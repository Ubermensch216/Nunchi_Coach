"""Keep exports outside registered inputs and publish complete files atomically."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterator, TextIO, TYPE_CHECKING

if TYPE_CHECKING:
    from ..db import Database


class UnsafeOutputError(ValueError):
    pass


def validate_output(path: str | Path, db: Database) -> Path:
    target = Path(path).resolve()
    if target.is_dir():
        raise UnsafeOutputError("파일을 저장할 경로를 선택해 주세요.")
    if os.name == "nt" and any(":" in part for part in target.parts[1:]):
        raise UnsafeOutputError("파일 경로에 대체 데이터 스트림을 사용할 수 없습니다.")
    for source in db.sources():
        root = Path(source["path"]).resolve()
        if target == root or target.is_relative_to(root):
            raise UnsafeOutputError("자료 폴더에는 저장할 수 없습니다. 별도 출력 폴더를 선택해 주세요.")
    protected = [db.path, Path(str(db.path) + "-wal"), Path(str(db.path) + "-shm")]
    protected.extend(Path(row["path"]) for row in db.con.execute("SELECT path FROM documents"))
    for original in protected:
        if target == original.resolve():
            raise UnsafeOutputError("원본 자료나 프로젝트 DB를 덮어쓸 수 없습니다.")
        if target.exists() and original.exists() and os.path.samefile(target, original):
            raise UnsafeOutputError("원본 자료와 연결된 파일에는 저장할 수 없습니다.")
    return target


@contextmanager
def safe_text_writer(path: str | Path, db: Database) -> Iterator[TextIO]:
    target = validate_output(path, db)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="",
                                dir=target.parent, prefix=".export-", delete=False) as handle:
            temporary = Path(handle.name)
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        # Sources can have changed while the export was being assembled.
        validate_output(target, db)
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
