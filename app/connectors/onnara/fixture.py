"""Synthetic On-nara adapter. Never claims live-system verification."""
from __future__ import annotations

from dataclasses import fields
import json
from pathlib import Path
import shutil

from .contract import (AccessDenied, Asset, Capabilities, DocumentRecord,
                       SearchPage)


def _model(cls, value):
    allowed = {item.name for item in fields(cls)}
    return cls(**{key: val for key, val in value.items() if key in allowed})


class FixtureConnector:
    def __init__(self, catalog: str | Path, page_size: int = 10):
        self.catalog = Path(catalog).resolve()
        self.root = self.catalog.parent
        data = json.loads(self.catalog.read_text(encoding="utf-8"))
        if data.get("synthetic") is not True:
            raise ValueError("합성 자료로 명시된 시험 카탈로그만 사용할 수 있습니다.")
        self._capabilities = Capabilities("fixture-v1", data["institution"], data["system_id"],
                                         simulation=True, authoritative_total=True)
        self.page_size = max(1, page_size)
        self.records = {}
        self.assets = {}
        self.asset_paths = {}
        for item in data["documents"]:
            record = _model(DocumentRecord, item)
            if not record.document_id or record.document_id in self.records:
                raise ValueError("문서 ID가 비어 있거나 중복되었습니다.")
            self.records[record.document_id] = record
            self.assets[record.document_id] = []
            for raw in item.get("assets", []):
                asset = _model(Asset, raw)
                key = (record.document_id, asset.asset_id)
                if not asset.asset_id or key in self.asset_paths:
                    raise ValueError("붙임 ID가 비어 있거나 중복되었습니다.")
                path = (self.root / raw["path"]).resolve()
                if not path.is_relative_to(self.root):
                    raise ValueError("시험 파일 경로가 카탈로그 폴더를 벗어납니다.")
                self.assets[record.document_id].append(asset)
                self.asset_paths[key] = (path, raw.get("denied", False))

    @property
    def capabilities(self):
        return self._capabilities

    @property
    def protected_roots(self):
        return (self.root,)

    def resolve_author(self, name: str):
        return sorted({(r.author_id, r.author_name, r.department)
                       for r in self.records.values() if r.author_name == name})

    def list_documents(self, scope, cursor=None):
        records = [r for r in self.records.values()
                   if r.author_id in scope.author_ids
                   and scope.start_date <= r.authored_at[:10] <= scope.end_date
                   and (not scope.departments or r.department in scope.departments)
                   and (not scope.statuses or r.status in scope.statuses)
                   and r.author_role in {"author", "drafter", "coauthor"}]
        records.sort(key=lambda r: (r.authored_at, r.document_id))
        offset = int(cursor or "0")
        page = records[offset:offset + self.page_size]
        end = offset + len(page)
        return SearchPage(tuple(page), str(end) if end < len(records) else None, len(records))

    def get_metadata(self, document_id):
        return self.records[document_id]

    def list_assets(self, document_id):
        return tuple(self.assets[document_id])

    def download_asset(self, document_id, asset_id, destination):
        path, denied = self.asset_paths[(document_id, asset_id)]
        if denied:
            raise AccessDenied("해당 파일은 내려받을 수 없습니다.")
        # Resolve again in case a link changed after reading the catalog.
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("시험 파일 경로가 변경되었습니다.")
        shutil.copyfile(path, destination)
