"""onNara.sAIde가 내려받아 둔 꾸러미 폴더를 읽는 연결부.

꾸러미는 사용자가 자기 온나라 화면에서 고른 문서를 확장이 내려받고
package.json으로 묶은 폴더다(docs/온나라_연동_구현계획서_2026-10-12.md §3).
이 연결부는 그 폴더를 **읽기만 한다.** 온나라에 접속하지 않고 인증 정보를
다루지 않는다.

모르는 것은 모른다고 남긴다.
  · 작성자 ID가 없으면 이름을 식별자로 쓰고 `author_identity="name"`으로 밝힌다.
    동명이인을 가를 수 없으므로 전체 건수를 믿을 수 있다고 하지 않는다.
  · 받지 못한 파일은 목록에 남기고 내려받기에서 실패로 돌려준다 — 지우면
    '붙임이 없는 문서'와 '붙임을 못 받은 문서'를 구분할 수 없다.
  · package.json이 가리키는 경로는 꾸러미 폴더 안의 상대 경로만 받는다.
"""
from __future__ import annotations

from collections import Counter
import json
import mimetypes
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
from urllib.parse import urlsplit

from .contract import (Asset, Capabilities, DocumentRecord, InvalidDownload,
                       SearchPage, SearchScope)

PACKAGE = "package.json"
FORMAT = "saide-onnara-package"
VERSION = 1
MAX_PACKAGE_BYTES = 20 * 1024 * 1024
ID_BASES = ("source-document-id", "management-number-and-date", "list-row")
FIDELITIES = ("original", "rendered-text", "extracted-text")
_DATE = re.compile(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
_MEDIA = {".hwp": "application/x-hwp", ".hwpx": "application/hwp+zip", ".md": "text/markdown",
          ".html": "text/html", ".htm": "text/html"}


def is_package(folder: str | Path) -> bool:
    return (Path(folder) / PACKAGE).is_file()


def iso_date(value) -> str:
    """목록의 일자 표기를 YYYY-MM-DD로. 읽지 못하면 빈 값 — 추정해 채우지 않는다."""
    text = str(value or "").strip()
    match = _DATE.search(text)
    if match:
        parts = match.groups()
    else:
        digits = re.sub(r"\D", "", text)
        if len(digits) != 8:
            return ""
        parts = (digits[:4], digits[4:6], digits[6:])
    year, month, day = (int(part) for part in parts)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def _text(value, limit=500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _strings(value) -> list[str]:
    return [text for text in (_text(item, 200) for item in value)
            if text] if isinstance(value, list) else []


def _author_key(author_id: str, author_name: str) -> str:
    return author_id or f"name:{author_name}"


def _media_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return _MEDIA.get(suffix) or mimetypes.guess_type(filename)[0] or "application/octet-stream"


class SaidePackageConnector:
    def __init__(self, folder: str | Path, page_size: int = 50):
        self.root = Path(folder).resolve()
        path = self.root / PACKAGE
        if not path.is_file():
            raise ValueError("sAIde 꾸러미 폴더가 아닙니다. package.json이 있는 폴더를 고르세요.")
        if path.stat().st_size > MAX_PACKAGE_BYTES:
            raise ValueError("package.json이 너무 큽니다.")
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"package.json을 읽을 수 없습니다: {exc}") from exc
        if (not isinstance(data, dict) or data.get("format") != FORMAT
                or data.get("version") != VERSION or not isinstance(data.get("documents"), list)):
            raise ValueError("이 프로그램이 아는 꾸러미 형식이 아닙니다(형식 또는 판이 다릅니다).")

        system = data.get("system") if isinstance(data.get("system"), dict) else {}
        host = urlsplit(_text(system.get("origin"))).netloc or "unknown"
        declared = data.get("scope") if isinstance(data.get("scope"), dict) else {}
        listing = data.get("listing") if isinstance(data.get("listing"), dict) else {}
        self.page_size = max(1, page_size)
        self.run_id = re.sub(r"[^A-Za-z0-9_-]", "", _text(data.get("run_id"), 80))
        self.records: dict[str, DocumentRecord] = {}
        self.assets: dict[str, list[Asset]] = {}
        self._files: dict[tuple[str, str], dict] = {}
        bases, fidelities, truncated = Counter(), Counter(), 0
        for item in data["documents"]:
            if not isinstance(item, dict):
                raise ValueError("꾸러미의 문서 항목이 올바르지 않습니다.")
            document_id = _text(item.get("document_id"), 200)
            if not document_id or document_id in self.records:
                raise ValueError("문서 ID가 비어 있거나 중복되었습니다.")
            basis = item.get("id_basis") if item.get("id_basis") in ID_BASES else "list-row"
            bases[basis] += 1
            author_id, author_name = _text(item.get("author_id"), 200), _text(item.get("author_name"), 200)
            self.records[document_id] = DocumentRecord(
                document_id=document_id, title=_text(item.get("title")),
                author_id=_author_key(author_id, author_name),
                authored_at=iso_date(item.get("authored_at")), author_name=author_name,
                department=_text(item.get("department"), 200),
                status=_text(item.get("status"), 80) or "unknown",
                document_number=_text(item.get("document_number"), 200), id_basis=basis,
                task_card=_text(item.get("task_card"), 200))
            self.assets[document_id] = []
            for raw in item.get("assets") if isinstance(item.get("assets"), list) else []:
                if not isinstance(raw, dict):
                    raise ValueError("꾸러미의 파일 항목이 올바르지 않습니다.")
                asset_id, filename = _text(raw.get("asset_id"), 200), _text(raw.get("filename"), 300)
                if not asset_id or (document_id, asset_id) in self._files:
                    raise ValueError("파일 ID가 비어 있거나 중복되었습니다.")
                fidelity = raw.get("fidelity") if raw.get("fidelity") in FIDELITIES else ""
                if raw.get("role") == "body":
                    fidelities[fidelity or "unstated"] += 1
                    truncated += raw.get("truncated") is True and raw.get("status") == "complete"
                self.assets[document_id].append(Asset(
                    asset_id=asset_id, filename=filename or "document.bin", role=_text(raw.get("role"), 20),
                    media_type=_media_type(filename), fidelity=fidelity))
                self._files[(document_id, asset_id)] = raw

        records = self.records.values()
        by_id = all(not record.author_id.startswith("name:") for record in records) and bool(self.records)
        author_ids = tuple(_strings(declared.get("author_ids"))) or tuple(
            f"name:{name}" for name in _strings(declared.get("author_names")))
        if not author_ids:
            author_ids = tuple(sorted({record.author_id for record in records}))
        dates = sorted(record.authored_at for record in records if record.authored_at)
        start = iso_date(declared.get("start_date")) or (dates[0] if dates else "")
        end = iso_date(declared.get("end_date")) or (dates[-1] if dates else "")
        if not author_ids or not start or not end:
            raise ValueError("꾸러미에서 수집 범위(작성자·기간)를 알 수 없습니다. 일자가 있는 문서가 한 건도 없습니다.")
        self._scope = SearchScope(author_ids, start, end, tuple(_strings(declared.get("departments"))))
        total = listing.get("total")
        self._total = total if isinstance(total, int) and not isinstance(total, bool) else None
        whole = declared.get("kind") == "listing" and listing.get("finished") is True and self._total is not None
        self._capabilities = Capabilities(
            "saide-package-v1", _text(system.get("institution"), 200) or host, f"onnara@{host}",
            live_verified=False, simulation=False, authoritative_total=bool(whole and by_id))
        exporter = data.get("exporter") if isinstance(data.get("exporter"), dict) else {}
        self.details = {
            "format": FORMAT, "version": VERSION, "run_id": self.run_id,
            "created_at": _text(data.get("created_at"), 40),
            "exporter": f"{_text(exporter.get('name'), 60)} {_text(exporter.get('version'), 40)}".strip(),
            "scope_kind": "listing" if declared.get("kind") == "listing" else "selection",
            "list_name": _text(declared.get("list_name"), 120),
            "author_identity": "id" if by_id else "name",
            "id_basis": dict(bases), "body_fidelity": dict(fidelities),
            # 확장이 본문을 끝까지 읽지 못했다고 밝힌 건수. 받은 파일이지만 본문 전체가 아니다.
            "body_truncated": truncated,
        }

    @property
    def capabilities(self):
        return self._capabilities

    @property
    def protected_roots(self):
        return (self.root,)

    def scope(self) -> SearchScope:
        """꾸러미가 밝힌 범위. 밝히지 않은 기간은 문서 일자의 최소·최대다."""
        return self._scope

    def resolve_author(self, name: str):
        return sorted({(r.author_id, r.author_name, r.department)
                       for r in self.records.values() if r.author_name == name})

    def list_documents(self, scope, cursor=None):
        # 범위 판정은 수집 엔진이 한다. 여기서 거르면 '범위 밖이라 뺀 문서'가 기록에 남지 않는다.
        records = list(self.records.values())
        offset = int(cursor or "0")
        page = records[offset:offset + self.page_size]
        end = offset + len(page)
        return SearchPage(tuple(page), str(end) if end < len(records) else None, self._total)

    def get_metadata(self, document_id):
        return self.records[document_id]

    def list_assets(self, document_id):
        return tuple(self.assets[document_id])

    def download_asset(self, document_id, asset_id, destination):
        raw = self._files[(document_id, asset_id)]
        if raw.get("status") != "complete":
            raise InvalidDownload(_text(raw.get("message")) or "확장이 이 파일을 받지 못했습니다.")
        relative = str(raw.get("path") or "")
        if (not relative or "\\" in relative or PurePosixPath(relative).is_absolute()
                or PureWindowsPath(relative).is_absolute() or PureWindowsPath(relative).drive
                or ".." in PurePosixPath(relative).parts):
            raise InvalidDownload("꾸러미 폴더 기준 상대 경로가 아닙니다.")
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise InvalidDownload("꾸러미 폴더에 파일이 없습니다.")
        shutil.copyfile(path, destination)
