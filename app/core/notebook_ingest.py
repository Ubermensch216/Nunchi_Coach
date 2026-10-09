"""노트북에 올린 파일을 받아 읽고 질문할 수 있게 준비한다.

전임자 자료 폴더는 읽기만 한다. 올린 파일은 다르다 — 사용자가 이 노트북에서
쓰라고 건넨 것이므로 프로젝트 저장소 안 보관소에 **사본**을 둔다. 원래 자리의
파일은 건드리지 않는다. 사본을 두는 이유는 근거의 재현성이다: 바탕 화면의
파일은 내일 지워지거나 바뀌고, 그러면 어제의 인용을 다시 확인할 수 없다.

    보관소  <프로젝트>/uploads/<내용 해시 앞 16자>/<파일 이름>

내용이 같은 파일은 한 번만 보관하고, 이미 분석한 문서와 내용이 같으면 그
문서를 연결한다. 같은 내용의 문서가 둘이면 검색은 대표본 하나만 근거로
삼는다(db.representative_predicate) — 사본만 연결한 노트북에서는 아무것도
찾지 못하게 된다.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..ai import analyze
from ..ingest import vision
from ..ingest.hasher import sha256
from ..ingest.parsers import parse, supported_extensions
from ..ingest.parsers.base import EMPTY, MAX_BYTES, PARSER_VERSION, PARTIAL, ParseResult
from ..search import pack
from . import dating, notebook
from .chunking import build_chunks

UPLOAD_DIR = "uploads"
EMBED_BATCH = 32
DATE_SECTIONS = 6
# 구형 형식은 파서가 '지원 안 함'을 돌려준다. 올리는 자리에서 먼저 거절한다.
_LEGACY = {".doc", ".xls", ".ppt"}

PHASE_STORE = "보관하는 중"
PHASE_PARSE = "내용을 읽는 중"
PHASE_VISION = "그림의 글자를 읽는 중"
PHASE_INDEX = "질문할 수 있게 준비하는 중"
PHASE_SUMMARY = "요약하는 중"


def accepted_extensions() -> tuple[str, ...]:
    return tuple(sorted((set(supported_extensions()) - _LEGACY) | set(vision.IMAGE_EXTENSIONS)))


def file_filter() -> str:
    """파일 고르기 대화상자용 필터."""
    marks = " ".join(f"*{ext}" for ext in accepted_extensions())
    return f"문서와 그림 ({marks});;모든 파일 (*)"


def upload_root(db) -> Path:
    return db.path.parent / UPLOAD_DIR


def upload_source(db) -> int:
    root = upload_root(db)
    root.mkdir(parents=True, exist_ok=True)
    return db.add_source(root, kind="upload")


@dataclass(slots=True)
class Ingested:
    path: str
    filename: str
    doc_id: int | None = None
    status: str = ""            # ok | partial | reused | 그 밖은 읽지 못한 사유
    note: str = ""
    error: str = ""

    @property
    def usable(self) -> bool:
        return self.status in ("ok", "partial", "reused")


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned[-140:] or "document"


def store(db, path: Path, digest: str) -> Path:
    """보관소에 사본을 둔다. 쓰다 만 파일이 남지 않게 임시 파일로 쓰고 바꿔 끼운다."""
    folder = upload_root(db) / digest[:16]
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / _safe_name(path.name)
    if target.exists():
        existing, _ = sha256(target)
        if existing == digest:
            return target
    temporary = None
    try:
        with NamedTemporaryFile(dir=folder, prefix=".upload-", delete=False) as handle:
            temporary = Path(handle.name)
        shutil.copyfile(path, temporary)
        copied, error = sha256(temporary)
        if copied != digest:
            raise OSError(error or "복사한 내용이 원본과 다릅니다. 다시 올려 주세요.")
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return target


def _existing(db, digest: str):
    """내용이 같은, 이미 읽어 둔 문서(대표본)."""
    return db.con.execute(
        "SELECT * FROM documents WHERE hash=? AND missing_since IS NULL "
        "AND parse_status IN ('ok','partial') ORDER BY id LIMIT 1", (digest,)).fetchone()


def read_document(client, path: Path, on_phase=lambda phase: None,
                  should_stop: Callable[[], bool] = lambda: False) -> ParseResult:
    """형식에 맞는 방법으로 읽는다. 글자 없는 PDF는 그림으로 다시 읽어 본다."""
    if vision.is_image(path):
        on_phase(PHASE_VISION)
        return vision.read(client, path, should_stop)
    on_phase(PHASE_PARSE)
    result = parse(path)
    scanned = path.suffix.lower() == ".pdf" and (
        result.status == EMPTY or (result.status == PARTIAL and "스캔본" in (result.note or "")))
    if scanned and client is not None:
        on_phase(PHASE_VISION)
        seen = vision.read(client, path, should_stop)
        if seen.ok:
            seen.meta = result.meta
            return seen
    return result


def write_parse(db, doc_id: int, filename: str, path: str, result: ParseResult) -> None:
    """읽은 결과를 한 묶음으로 남긴다(jobs/pipeline.py _parse와 같은 계약)."""
    with db.transaction():
        if result.ok:
            db.invalidate_document_analysis(doc_id)
        db.update_document(
            doc_id, parse_status=result.status, parse_error=result.error, parse_note=result.note,
            parser=result.parser, parser_version=PARSER_VERSION, char_count=result.char_count,
            author=result.meta.author, doc_title=result.meta.title,
            meta_created=result.meta.created, meta_modified=result.meta.modified)
        if result.ok:
            db.replace_sections(doc_id, [(s.kind, s.ordinal, s.locator, s.text) for s in result.sections])
            db.index_document(doc_id, filename, path, result.meta.author or "", result.text)


def write_date(db, doc_id: int) -> None:
    row = db.document(doc_id)
    if row["date_decided_by"] == "user":
        return
    sections = db.con.execute(
        "SELECT locator, text FROM document_sections WHERE doc_id=? ORDER BY ordinal LIMIT ?",
        (doc_id, DATE_SECTIONS)).fetchall()
    resolution = dating.collect(
        sections=[(s["locator"], s["text"]) for s in sections], filename=row["filename"],
        # 보관소 경로에는 날짜 단서가 없다. 해시 폴더 이름의 숫자를 연도로 읽으면 안 된다.
        path=row["filename"], meta_created=row["meta_created"],
        meta_modified=row["meta_modified"], fs_mtime=row["fs_mtime"])
    with db.transaction():
        db.replace_dates(doc_id, [c.as_row() for c in resolution.candidates])
        db.update_document(doc_id, eff_date=resolution.value, eff_date_kind=resolution.kind,
                           eff_precision=resolution.precision, eff_year=resolution.year,
                           eff_month=resolution.month)


def write_chunks(db, doc_id: int, client, should_stop: Callable[[], bool] = lambda: False) -> str:
    """조각을 만들고 임베딩한다. 모델이 없으면 조각까지만 — 글자 검색은 그대로 된다."""
    if not db.con.execute("SELECT 1 FROM chunks WHERE doc_id=? LIMIT 1", (doc_id,)).fetchone():
        sections = db.con.execute(
            "SELECT kind, locator, text FROM document_sections WHERE doc_id=? ORDER BY ordinal",
            (doc_id,)).fetchall()
        pieces = build_chunks([(s["kind"], s["locator"], s["text"]) for s in sections])
        if pieces:
            with db.transaction():
                db.replace_document_chunks(doc_id, pieces)
    if client is None or not client.health().embedding_ready:
        return "의미 검색은 로컬 AI가 준비되면 이어서 준비합니다."
    pending = db.chunks_needing_embedding(doc_id, client.embed_model)
    for start in range(0, len(pending), EMBED_BATCH):
        if should_stop():
            break
        batch = pending[start:start + EMBED_BATCH]
        vectors, error = client.embed([text for _, text in batch])
        if error:
            return f"의미 검색 준비가 중단됐습니다: {error}"
        with db.transaction():
            for (chunk_id, _text), vector in zip(batch, vectors):
                db.save_chunk_embedding(chunk_id, client.embed_model, len(vector), pack(vector))
    return ""


def summarize_source(db, notebook_id: int, doc_id: int, client) -> bool:
    """자료 한 건의 요약과 주제어. 실패해도 자료는 그대로 쓴다."""
    if client is None or not client.health().generation_ready:
        return False
    row = db.con.execute("SELECT body FROM document_index WHERE doc_id=?", (doc_id,)).fetchone()
    if row is None or not (row["body"] or "").strip():
        return False
    summary = analyze.summarize(client, doc_id, row["body"])
    topics = analyze.keywords(client, doc_id, row["body"])
    if not summary.ok and not topics.ok:
        return False
    db.con.execute("UPDATE notebook_sources SET summary=?, topics=? WHERE notebook_id=? AND doc_id=?",
                   (summary.summary or "", notebook.dumps(topics.keywords), notebook_id, doc_id))
    return True


def ingest_file(db, notebook_id: int, path: str | Path, client=None,
                on_phase: Callable[[str], None] = lambda phase: None,
                should_stop: Callable[[], bool] = lambda: False, origin: str = "upload") -> Ingested:
    """파일 하나를 노트북의 자료로 만든다. 실패는 값으로 돌려준다.

    origin은 출처 목록에 적는 갈래다. 눈치코치가 만든 업무 정리본도 이 길로
    들어온다(core/notebook_tasks.py).
    """
    path = Path(path)
    outcome = Ingested(str(path), path.name)
    notebook.require(db, notebook_id)
    try:
        if not path.is_file():
            raise ValueError("파일을 찾을 수 없습니다.")
        if path.suffix.lower() not in accepted_extensions():
            raise ValueError(f"지원하지 않는 형식입니다: {path.suffix or '(확장자 없음)'}")
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise ValueError(f"파일이 너무 큽니다({size:,}바이트). 200MB까지 올릴 수 있습니다.")
        if size == 0:
            raise ValueError("빈 파일입니다.")
        on_phase(PHASE_STORE)
        digest, error = sha256(path)
        if digest is None:
            raise OSError(error or "파일을 읽을 수 없습니다.")

        known = _existing(db, digest)
        if known is not None:
            if origin == "upload" and not Path(known["path"]).is_relative_to(upload_root(db)):
                origin = "project"
            notebook.add_sources(db, notebook_id, [known["id"]], origin=origin)
            outcome.doc_id, outcome.status = known["id"], "reused"
            outcome.note = "같은 내용의 문서를 이미 읽어 두어 그 문서를 연결했습니다."
            write_chunks(db, known["id"], client, should_stop)
            return outcome

        stored = store(db, path, digest)
        stat = stored.stat()
        doc_id = db.upsert_document(upload_source(db), {
            "path": str(stored), "filename": path.name, "ext": path.suffix.lower(), "size": stat.st_size,
            "fs_mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            "hash": digest, "parse_status": "pending"})
        outcome.doc_id = doc_id
        result = read_document(client, stored, on_phase, should_stop)
        write_parse(db, doc_id, path.name, str(stored), result)
        outcome.status, outcome.note, outcome.error = result.status, result.note or "", result.error or ""
        # 읽지 못한 파일도 목록에 남긴다. 사라지면 올렸는지조차 알 수 없다.
        notebook.add_sources(db, notebook_id, [doc_id], origin=origin)
        if not result.ok:
            if not outcome.error:
                outcome.error = outcome.note or "본문을 읽지 못했습니다."
            return outcome
        on_phase(PHASE_INDEX)
        write_date(db, doc_id)
        pending = write_chunks(db, doc_id, client, should_stop)
        if pending:
            outcome.note = (outcome.note + " " + pending).strip()
        db.audit("notebook.upload", f"{notebook_id}/{doc_id}", result=result.status)
    except (ValueError, OSError) as exc:
        outcome.status, outcome.error = "rejected", str(exc)
    return outcome


def outdated_uploads(db) -> list[int]:
    """다시 읽어야 하는 올린 파일 — 읽다 멈췄거나 파서가 바뀐 것."""
    return [row[0] for row in db.con.execute(
        "SELECT d.id FROM documents d JOIN sources s ON s.id=d.source_id AND s.kind='upload' "
        "WHERE d.missing_since IS NULL AND (d.parse_status='pending' "
        "OR (d.parse_status IN ('ok','partial') AND COALESCE(d.parser_version,'') != ?))",
        (PARSER_VERSION,))]


def reprocess(db, doc_id: int, client=None, on_phase=lambda phase: None,
              should_stop: Callable[[], bool] = lambda: False) -> Ingested:
    row = db.document(doc_id)
    outcome = Ingested(row["path"], row["filename"], doc_id)
    result = read_document(client, Path(row["path"]), on_phase, should_stop)
    write_parse(db, doc_id, row["filename"], row["path"], result)
    outcome.status, outcome.note, outcome.error = result.status, result.note or "", result.error or ""
    if result.ok:
        on_phase(PHASE_INDEX)
        write_date(db, doc_id)
        write_chunks(db, doc_id, client, should_stop)
    return outcome
