"""Resumable On-nara collection, independent of a specific institution transport."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from tempfile import NamedTemporaryFile

from ..connectors.onnara.contract import (AccessDenied, Asset, AuthenticationRequired,
                                         Connector, InvalidDownload, SearchScope)
from ..ingest.parsers.base import MAX_BYTES


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(path: Path):
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _replace(source: Path, target: Path, attempts: int = 8):
    """Windows에서 백신·색인기가 방금 쓴 파일을 잠시 잡고 있으면 PermissionError가 난다. 짧게 다시 시도한다."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def _write_json(path: Path, value):
    temporary = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                prefix=".checkpoint-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        _replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _in_scope(record, scope):
    return (record.author_id in scope.author_ids
            and scope.start_date <= record.authored_at[:10] <= scope.end_date
            and (not scope.departments or record.department in scope.departments)
            and (not scope.statuses or record.status in scope.statuses)
            and record.author_role in {"author", "drafter", "coauthor"})


class Collection:
    def __init__(self, connector: Connector, scope: SearchScope, directory: str | Path,
                 max_attempts: int = 3):
        self.connector = connector
        self.scope = scope
        self.root = Path(directory).resolve()
        for source in connector.protected_roots:
            source = source.resolve()
            if self.root == source or self.root.is_relative_to(source) or source.is_relative_to(self.root):
                raise ValueError("수집 저장소는 원본 자료 폴더와 분리해 주세요.")
        self.max_attempts = min(max(max_attempts, 1), 5)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = self.root / "manifest.json"
        # Fingerprint includes institution/system and the exact selected scope.
        self.identity = {"adapter": asdict(connector.capabilities), "scope": asdict(scope)}
        # Normalize tuples just as JSON persists them.
        self.identity = json.loads(json.dumps(self.identity))
        if self.manifest.exists():
            self.state = json.loads(self.manifest.read_text(encoding="utf-8"))
            if self.state.get("identity") != self.identity or self.state.get("schema_version") != 1:
                raise ValueError("다른 범위/연결부의 저장소입니다. 새 수집 폴더를 선택해 주세요.")
        else:
            self.state = {"schema_version": 1, "identity": self.identity,
                          "started_at": _now(), "documents": {}, "assets": {}}
        # 연결부가 밝힌 수집 근거(작성자 식별 방식·본문 충실도 등). 지문에는 넣지 않는다.
        details = getattr(connector, "details", None)
        if details:
            self.state["package"] = json.loads(json.dumps(details))

    def _save(self):
        self.state["updated_at"] = _now()
        _write_json(self.manifest, self.state)

    def _summarize(self):
        assets = list(self.state["assets"].values())
        downloaded = sum(item.get("status") == "downloaded" for item in assets)
        expected = self.state.get("expected_documents")
        list_complete = (self.state.get("listing_finished", False)
                         and self.connector.capabilities.authoritative_total
                         and not self.state.get("listing_errors")
                         and expected is not None
                         and expected == len(self.state["documents"]))
        self.state["summary"] = {
            "documents": len(self.state["documents"]), "expected_documents": expected,
            "assets": len(assets), "downloaded": downloaded,
            "failed": sum(item.get("status") in {"failed", "denied"} for item in assets),
            "pending": sum(item.get("status") == "pending" for item in assets),
            "listing_complete": list_complete,
            "completeness": ("partial" if self.state.get("metadata_errors") or downloaded != len(assets)
                             else "complete" if list_complete else "unknown"),
            "simulation": self.connector.capabilities.simulation,
            "live_verified": self.connector.capabilities.live_verified,
        }
        self._save()
        return self.state

    def run(self, should_stop: Callable[[], bool] = lambda: False,
            on_progress: Callable[[str, str], None] = lambda phase, label: None):
        self.state["status"] = "running"
        self.state["listing_errors"] = []
        self.state["metadata_errors"] = []
        self.state["listing_finished"] = False
        self.state["expected_documents"] = None
        # Restart listing, deduplicating stable IDs; do not redownload validated successes.
        records = {}
        cursor = None
        seen_cursors = set()
        try:
            while True:
                if should_stop():
                    self.state["status"] = "cancelled"
                    return self._summarize()
                page = self.connector.list_documents(self.scope, cursor)
                if page.total is not None:
                    previous = self.state["expected_documents"]
                    if previous is not None and previous != page.total:
                        self.state["listing_errors"].append("검색 중 전체 건수가 바뀌었습니다.")
                    self.state["expected_documents"] = page.total
                for item in page.records:
                    if not item.document_id or not _in_scope(item, self.scope):
                        self.state["listing_errors"].append("범위 밖 또는 ID 없는 검색 결과")
                        continue
                    records[item.document_id] = item
                on_progress("listing", str(len(records)))
                if page.next_cursor is None:
                    self.state["listing_finished"] = True
                    break
                if page.next_cursor in seen_cursors or len(seen_cursors) >= 10000:
                    self.state["listing_errors"].append("검색 페이지 반복/상한에 도달했습니다.")
                    break
                seen_cursors.add(page.next_cursor)
                cursor = page.next_cursor

            self.state["documents"] = {}
            current_assets = {}
            for document_id in records:
                if should_stop():
                    self.state["status"] = "cancelled"
                    return self._summarize()
                try:
                    record = self.connector.get_metadata(document_id)
                    if record.document_id != document_id or not _in_scope(record, self.scope):
                        raise InvalidDownload("문서 메타데이터가 검색 범위와 다릅니다.")
                    assets = self.connector.list_assets(document_id)
                    if not any(asset.role == "body" for asset in assets):
                        raise InvalidDownload("문서 본문 목록이 없습니다.")
                    self.state["documents"][document_id] = asdict(record)
                    for asset in assets:
                        if not asset.asset_id or asset.role not in {"body", "attachment"}:
                            raise InvalidDownload("잘못된 파일 식별자/역할")
                        key = hashlib.sha256(f"{document_id}\0{asset.asset_id}".encode()).hexdigest()
                        if key in current_assets:
                            raise InvalidDownload("중복 파일 ID")
                        old = self.state["assets"].get(key, {})
                        entry = {"document_id": document_id, "asset": asdict(asset), "status": "pending"}
                        if old.get("asset") == asdict(asset):
                            entry = old.copy()
                            entry["document_id"] = document_id
                        current_assets[key] = entry
                except AuthenticationRequired:
                    raise
                except Exception as exc:
                    self.state["metadata_errors"].append({"document_id": document_id,
                                                          "error": type(exc).__name__})
            self.state["assets"] = current_assets
            self._save()

            for entry in self.state["assets"].values():
                if should_stop():
                    self.state["status"] = "cancelled"
                    return self._summarize()
                if self._already_downloaded(entry):
                    continue
                asset = Asset(**entry["asset"])
                for attempt in range(self.max_attempts):
                    entry["attempts"] = attempt + 1
                    try:
                        self._download(entry, asset)
                        break
                    except AuthenticationRequired:
                        entry["status"] = "pending"
                        raise
                    except AccessDenied:
                        entry.update(status="denied", error="AccessDenied")
                        break
                    except Exception as exc:
                        entry.update(status="failed", error=type(exc).__name__)
                self._save()
                on_progress("download", entry["document_id"])
            self.state["status"] = "finished"
        except AuthenticationRequired:
            self.state["status"] = "authentication_required"
        except Exception as exc:
            self.state["status"] = "failed"
            self.state["listing_errors"].append(type(exc).__name__)
        return self._summarize()

    def _already_downloaded(self, entry):
        if entry.get("status") != "downloaded" or not entry.get("path"):
            return False
        path = (self.root / entry["path"]).resolve()
        if not path.is_relative_to(self.root / "files") or not path.is_file():
            return False
        size = path.stat().st_size
        asset = entry["asset"]
        if size <= 0 or size > MAX_BYTES or (asset.get("size") is not None and size != asset["size"]):
            return False
        digest = _digest(path)
        return (digest == entry.get("sha256")
                and (not asset.get("sha256") or digest == asset["sha256"]))

    def _download(self, entry, asset):
        directory = self.root / "files" / hashlib.sha256(entry["document_id"].encode()).hexdigest()[:20]
        if not directory.resolve().is_relative_to(self.root):
            raise InvalidDownload("저장 경로가 수집 폴더를 벗어납니다.")
        directory.mkdir(parents=True, exist_ok=True)
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", asset.filename).strip(" .")[-140:]
        if not filename:
            filename = "document.bin"
        prefix = hashlib.sha256(asset.asset_id.encode()).hexdigest()[:12]
        if asset.sha256:
            prefix += "-" + asset.sha256[:12]
        target = directory / f"{prefix}-{filename}"
        temporary = None
        try:
            with NamedTemporaryFile(dir=directory, prefix=".download-", delete=False) as handle:
                temporary = Path(handle.name)
            self.connector.download_asset(entry["document_id"], asset.asset_id, temporary)
            size = temporary.stat().st_size
            if not size or size > MAX_BYTES or (asset.size is not None and size != asset.size):
                raise InvalidDownload("파일 크기가 다운로드 정보와 다릅니다.")
            digest = _digest(temporary)
            if asset.sha256 and digest != asset.sha256:
                raise InvalidDownload("파일 해시가 다운로드 정보와 다릅니다.")
            with temporary.open("rb") as handle:
                head = handle.read(1024).lower().lstrip()
            if asset.media_type != "text/html" and (head.startswith(b"<!doctype html") or head.startswith(b"<html")):
                raise InvalidDownload("문서 대신 HTML 페이지를 받았습니다.")
            _replace(temporary, target)
            entry.update(status="downloaded", path=target.relative_to(self.root).as_posix(),
                         sha256=digest, size=size, downloaded_at=_now())
            entry.pop("error", None)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
