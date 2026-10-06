"""Transport-independent collection contract without authentication secrets."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol


class CollectionError(RuntimeError):
    pass


class AuthenticationRequired(CollectionError):
    pass


class AccessDenied(CollectionError):
    pass


class InvalidDownload(CollectionError):
    pass


@dataclass(frozen=True)
class Capabilities:
    adapter: str
    institution: str
    system_id: str
    live_verified: bool = False
    simulation: bool = False
    authoritative_total: bool = False


@dataclass(frozen=True)
class SearchScope:
    author_ids: tuple[str, ...]
    start_date: str
    end_date: str
    departments: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.author_ids or any(not value.strip() for value in self.author_ids):
            raise ValueError("작성자 ID를 하나 이상 지정해 주세요.")
        if date.fromisoformat(self.start_date) > date.fromisoformat(self.end_date):
            raise ValueError("기간의 시작은 종료일보다 늦을 수 없습니다.")


@dataclass(frozen=True)
class DocumentRecord:
    document_id: str
    title: str
    author_id: str
    authored_at: str
    author_name: str = ""
    department: str = ""
    status: str = "unknown"
    document_number: str = ""
    author_role: str = "author"
    registered_at: str | None = None
    approved_at: str | None = None


@dataclass(frozen=True)
class Asset:
    asset_id: str
    filename: str
    role: str
    media_type: str
    size: int | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class SearchPage:
    records: tuple[DocumentRecord, ...]
    next_cursor: str | None
    total: int | None = None


class Connector(Protocol):
    @property
    def capabilities(self) -> Capabilities: ...

    @property
    def protected_roots(self) -> tuple[Path, ...]: ...

    def list_documents(self, scope: SearchScope, cursor: str | None) -> SearchPage: ...

    def get_metadata(self, document_id: str) -> DocumentRecord: ...

    def list_assets(self, document_id: str) -> tuple[Asset, ...]: ...

    def download_asset(self, document_id: str, asset_id: str, destination: Path) -> None: ...
