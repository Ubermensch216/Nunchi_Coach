"""산출물 원장 — 도구가 만든 파일을 저장하고 판을 잇는다.

도구는 파일을 직접 쓰지 않는다. 바이트를 넘기면 여기서 쓴다.

    어디에      프로젝트 폴더의 outputs/actions 아래. 그 밖으로 나가는 이름은 거절한다
    어떻게      임시 이름으로 다 쓴 뒤 바꿔 끼운다. 도중에 죽어도 반쪽 파일이 남지 않는다
    원본 보호   자료 폴더·원본 파일·프로젝트 DB 위에는 쓰지 않는다(core/output_policy)
    같은 내용   다시 저장해도 행이 늘지 않는다
    다른 내용   새 판을 만들고 옛 판은 '대체됨'으로 남긴다. 옛 파일을 덮어쓰지 않는다
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..core.output_policy import UnsafeOutputError, validate_output
from ..core.paths import relative_to_source
from ..core.workcard import default_output_dir

ACTIONS_DIR = "actions"
_FORBIDDEN = '<>:"/\\|?*'


def actions_root(db) -> Path:
    return default_output_dir(db) / ACTIONS_DIR


def run_dir(db, case_id: int | None, run_id: int) -> Path:
    return actions_root(db) / (f"case-{case_id}" if case_id else "no-case") / f"run-{run_id}"


def safe_name(name: str) -> str:
    """파일 이름 하나로 만든다. 경로 구분자와 Windows가 받지 않는 글자를 바꾼다."""
    cleaned = "".join("_" if ch in _FORBIDDEN or ord(ch) < 32 else ch for ch in name).strip(" .")
    if not cleaned:
        raise UnsafeOutputError("산출물의 파일 이름이 비어 있습니다.")
    return cleaned


class ArtifactWriter:
    """한 단계가 쓰는 작성기. 실행기가 만들어 도구에 건넨다."""

    def __init__(self, db, run_id: int, step_id: int | None, case_id: int | None) -> None:
        self._db = db
        self._run_id, self._step_id, self._case_id = run_id, step_id, case_id

    def save(self, kind: str, title: str, filename: str, data: bytes, *,
             mode: str = "preliminary", basis_year: int | None = None,
             sources=(), evidence=()) -> int:
        """산출물 id를 돌려준다.

        sources는 만들 때 본 문서의 판 — [{doc_id, hash}]. evidence는 이 산출물의 결론이
        기댄 대목 — [{doc_id, locator, quote}]. 같은 내용을 다시 저장하면 새로 적지 않는다.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("산출물은 바이트로 넘겨야 합니다.")
        db = self._db
        digest = hashlib.sha256(data).hexdigest()
        previous = db.latest_artifact(self._case_id, kind, title)
        if previous is not None and previous["sha256"] == digest:
            _restore_if_missing(db, Path(previous["path"]), data)
            return previous["id"]

        version = previous["version"] + 1 if previous is not None else 1
        target = self._target(filename, version, digest)
        _write(db, target, data)
        with db.transaction():
            if previous is not None:
                db.set_artifact_state(previous["id"], "superseded")
            artifact_id = db.add_artifact(
                kind, title, target.suffix.lower().lstrip("."), str(target), digest,
                run_id=self._run_id, step_id=self._step_id, case_id=self._case_id,
                mode=mode, basis_year=basis_year, sources=sources, version=version,
                supersedes_id=previous["id"] if previous is not None else None)
            _add_evidence(db, artifact_id, evidence)
            return artifact_id

    def _target(self, filename: str, version: int, digest: str) -> Path:
        folder = run_dir(self._db, self._case_id, self._run_id)
        name = safe_name(filename)
        target = folder / name
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            # 같은 실행 안에서 내용이 달라졌다. 앞 판의 파일은 그대로 둔다.
            stem, suffix = Path(name).stem, Path(name).suffix
            target = folder / f"{stem}_v{version}{suffix}"
        return target


def _add_evidence(db, artifact_id: int, evidence) -> None:
    seen: set[tuple] = set()
    for item in evidence:
        key = (item["doc_id"], item["locator"], item["quote"])
        if key in seen:
            continue
        seen.add(key)
        document = db.con.execute(
            "SELECT d.hash, d.filename, d.path, s.path AS root FROM documents d "
            "JOIN sources s ON s.id = d.source_id WHERE d.id = ?", (item["doc_id"],)).fetchone()
        db.add_evidence(
            "artifact", artifact_id, item["doc_id"], document["hash"] if document else None,
            item["locator"], item["quote"], document["filename"] if document else None,
            relative_to_source(document["path"], document["root"] or "") if document else None)


def _write(db, target: Path, data: bytes) -> None:
    root = actions_root(db).resolve()
    if not target.resolve().is_relative_to(root):
        raise UnsafeOutputError("산출물은 프로젝트의 출력 폴더 안에만 저장합니다.")
    validate_output(target, db)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(dir=target.parent, prefix=".artifact-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        validate_output(target, db)     # 쓰는 동안 자료 폴더가 바뀌었을 수 있다
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _restore_if_missing(db, path: Path, data: bytes) -> None:
    """원장에는 있는데 파일이 지워졌으면 같은 내용으로 다시 쓴다."""
    if not path.exists():
        _write(db, path, data)


def current(db, row) -> bool:
    """파일이 원장에 적힌 그대로인가. 밖에서 고쳤거나 지웠으면 거짓이다."""
    path = Path(row["path"])
    return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
