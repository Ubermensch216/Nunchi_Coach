"""조각 규칙이 바뀌었을 때 색인을 바꾸는 길 — 준비 → 검증 → 전환, 그리고 되돌리기.

예전에는 규칙이 바뀌면 조각을 전부 지우고 다시 만들었다. 그 사이에는 질문에 답할
수 없었고, 업무노트의 답변·메모·검토 지식이 인용한 조각(위치와 원문)이 사라져 원문
파일이 그대로인데도 '근거가 달라졌다'가 됐다. 여기서는

  1. 옛 조각으로 계속 답하면서 새 조각을 chunk_staging에 따로 만든다(stage).
     끊겨도 만든 문서는 남고, 다음에 남은 문서부터 잇는다.
  2. 새 조각의 임베딩을 채운다(embed). 글이 같은 옛 조각의 벡터는 그대로 옮긴다.
  3. 빠진 문서·임베딩·차원·본문 누락이 없는지 본다(verify).
  4. 한 번의 트랜잭션으로 바꾼다(switch). 조각이 달라지지 않은 문서는 건드리지
     않는다 — 조각 번호와 자식 색인이 그대로다. 밀려난 옛 조각은 chunk_archive에
     문서 판본 해시·벡터와 함께 남는다.
  5. 문제가 있으면 마지막 전환을 되돌린다(rollback). 옛 조각이 제 번호로 돌아온다.

전환을 되돌린 프로젝트는 같은 규칙으로 다시 자동 전환하지 않는다(reindex_hold).
보관한 옛 조각을 지우는 것(prune)은 사람이 따로 정한다 — 지우면 그 조각을 인용한
메모는 다시 검토해야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..search.vector import pack
from .chunking import CHUNKING_VERSION, build_chunks_with_context

EMBED_BATCH = 16
HOLD = "reindex_hold"


@dataclass(slots=True)
class Status:
    current: str            # 지금 쓰는 조각의 규칙 버전
    target: str             # 코드의 규칙 버전
    needed: bool            # 전환이 필요한가
    held: bool              # 되돌린 뒤 자동 전환을 멈춰 두었는가
    documents: int = 0      # 조각이 있는 문서 수
    staged: int = 0         # 새 조각을 준비한 문서 수
    staged_chunks: int = 0
    unembedded: int = 0     # 준비한 조각 가운데 임베딩이 빠진 수
    revision: int = 0       # 지금까지의 전환 횟수
    archived: int = 0       # 보관 중인 옛 조각 수


@dataclass(slots=True)
class Switched:
    revision: int = 0
    changed_documents: int = 0
    unchanged_documents: int = 0
    archived: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _documents(db) -> list:
    """조각이 있고 지금도 읽을 수 있는 문서."""
    return db.con.execute(
        "SELECT d.id, d.hash, d.filename FROM documents d "
        "WHERE d.missing_since IS NULL AND d.parse_status IN ('ok','partial') "
        "AND EXISTS (SELECT 1 FROM chunks c WHERE c.doc_id=d.id) ORDER BY d.id").fetchall()


def needed(db) -> bool:
    current = db.get_meta("chunking_version")
    if current == CHUNKING_VERSION or db.get_meta(HOLD) == CHUNKING_VERSION:
        return False
    return db.con.execute("SELECT 1 FROM chunks LIMIT 1").fetchone() is not None


def status(db, model: str = "") -> Status:
    current = db.get_meta("chunking_version") or ""
    one = lambda sql, *args: db.con.execute(sql, args).fetchone()[0]   # noqa: E731
    return Status(
        current=current, target=CHUNKING_VERSION, needed=needed(db),
        held=db.get_meta(HOLD) == CHUNKING_VERSION and current != CHUNKING_VERSION,
        documents=len(_documents(db)),
        staged=one("SELECT COUNT(DISTINCT doc_id) FROM chunk_staging WHERE version=?", CHUNKING_VERSION),
        staged_chunks=one("SELECT COUNT(*) FROM chunk_staging WHERE version=?", CHUNKING_VERSION),
        unembedded=(one("SELECT COUNT(*) FROM chunk_staging WHERE version=? AND (vector IS NULL OR model!=?)",
                        CHUNKING_VERSION, model) if model else 0),
        revision=int(db.get_meta("index_revision") or 0),
        archived=one("SELECT COUNT(*) FROM chunk_archive"),
    )


def stage(db, should_stop: Callable[[], bool] = lambda: False,
          on_progress: Callable[[int, int, str], None] = lambda done, total, name: None) -> int:
    """새 규칙의 조각을 문서마다 준비한다. 이미 준비한 문서는 건너뛴다. 준비한 문서 수를 돌려준다."""
    # 다른 규칙으로, 또는 그 뒤 바뀐 판본으로 준비해 둔 것은 쓸 수 없다.
    with db.transaction():
        db.con.execute(
            "DELETE FROM chunk_staging WHERE version!=? OR doc_id NOT IN "
            "(SELECT id FROM documents d WHERE COALESCE(d.hash,'')=chunk_staging.doc_hash AND d.missing_since IS NULL)",
            (CHUNKING_VERSION,))
    documents = _documents(db)
    ready = {row[0] for row in db.con.execute("SELECT DISTINCT doc_id FROM chunk_staging")}
    made = 0
    for index, document in enumerate(documents, start=1):
        if should_stop():
            break
        if document["id"] in ready:
            continue
        sections = db.con.execute(
            "SELECT kind, locator, text, meta FROM document_sections WHERE doc_id=? ORDER BY ordinal",
            (document["id"],)).fetchall()
        pieces = build_chunks_with_context([(s["kind"], s["locator"], s["text"], s["meta"]) for s in sections])
        if not pieces:
            # 본문 표가 비었다(오래된 프로젝트·중단된 읽기). 이 문서는 옛 조각을 그대로 둔다.
            continue
        with db.transaction():
            db.con.executemany(
                "INSERT INTO chunk_staging(doc_id,ordinal,locator,text,context,doc_hash,version) "
                "VALUES (?,?,?,?,?,?,?)",
                [(document["id"], *piece, document["hash"] or "", CHUNKING_VERSION) for piece in pieces])
            # 글이 같은 옛 조각의 벡터는 다시 계산하지 않는다.
            db.con.execute(
                "UPDATE chunk_staging SET (model,dim,vector)=("
                " SELECT e.model,e.dim,e.vector FROM chunks c JOIN embeddings e ON e.chunk_id=c.id"
                " WHERE c.doc_id=chunk_staging.doc_id AND c.text=chunk_staging.text LIMIT 1) "
                "WHERE doc_id=?", (document["id"],))
        made += 1
        on_progress(index, len(documents), document["filename"])
    return made


def embed(db, client, should_stop: Callable[[], bool] = lambda: False) -> str:
    """준비한 조각의 빠진 임베딩을 채운다. 배치마다 저장한다. 오류 문구(없으면 빈 글)를 돌려준다."""
    pending = db.con.execute(
        "SELECT id, text FROM chunk_staging WHERE version=? AND (vector IS NULL OR model!=?) ORDER BY id",
        (CHUNKING_VERSION, client.embed_model)).fetchall()
    for start in range(0, len(pending), EMBED_BATCH):
        if should_stop():
            return "cancelled"
        batch = pending[start:start + EMBED_BATCH]
        vectors, error = client.embed([row["text"] for row in batch])
        if error:
            return error
        if not vectors or len(vectors) != len(batch) or any(len(v) != len(vectors[0]) for v in vectors):
            return "임베딩 개수·차원이 요청과 다릅니다."
        with db.transaction():
            for row, vector in zip(batch, vectors):
                db.con.execute("UPDATE chunk_staging SET model=?,dim=?,vector=? WHERE id=?",
                               (client.embed_model, len(vector), pack(vector), row["id"]))
    return ""


def verify(db, model: str = "") -> list[str]:
    """전환해도 되는지 본다. 문제를 사람이 읽을 말로 돌려준다(없으면 빈 목록).

    model을 주면 그 모델의 임베딩이 전부 준비됐는지도 본다. 옛 색인에 임베딩이 없던
    프로젝트는 model 없이 부른다 — 글자 검색만 하던 것을 그대로 잇는다.
    """
    problems: list[str] = []
    documents = _documents(db)
    staged = {row["doc_id"]: row for row in db.con.execute(
        "SELECT doc_id, COUNT(*) AS n, SUM(LENGTH(text)) AS chars, MIN(doc_hash) AS doc_hash "
        "FROM chunk_staging WHERE version=? GROUP BY doc_id", (CHUNKING_VERSION,))}
    for document in documents:
        row = staged.get(document["id"])
        sections = db.con.execute(
            "SELECT COALESCE(SUM(LENGTH(TRIM(text))),0) FROM document_sections WHERE doc_id=? AND TRIM(text)!=''",
            (document["id"],)).fetchone()[0]
        if row is None:
            if sections:
                problems.append(f"{document['filename']}: 새 조각을 준비하지 못했습니다.")
            continue
        if (document["hash"] or "") != row["doc_hash"]:
            problems.append(f"{document['filename']}: 준비한 뒤 원문 판본이 바뀌었습니다.")
        # 조각은 섹션의 글을 빼지 않는다(합칠 때 줄바꿈이 더해질 뿐이다).
        if row["chars"] < sections:
            problems.append(f"{document['filename']}: 새 조각의 본문이 원래보다 짧습니다"
                            f"({row['chars']:,}자 / {sections:,}자).")
    if model:
        missing = db.con.execute(
            "SELECT COUNT(*) FROM chunk_staging WHERE version=? AND (vector IS NULL OR model!=?)",
            (CHUNKING_VERSION, model)).fetchone()[0]
        if missing:
            problems.append(f"임베딩이 빠진 새 조각이 {missing:,}개 있습니다.")
        dims = [row[0] for row in db.con.execute(
            "SELECT DISTINCT dim FROM chunk_staging WHERE version=? AND model=?", (CHUNKING_VERSION, model))]
        if len(dims) > 1:
            problems.append(f"새 조각의 임베딩 차원이 섞여 있습니다({', '.join(map(str, sorted(dims)))}).")
    return problems


def switch(db, model: str = "") -> Switched:
    """검증을 지난 새 조각으로 바꾼다. 전부 바뀌거나 하나도 바뀌지 않는다."""
    result = Switched(problems=verify(db, model))
    if not result.ok:
        return result
    previous = db.get_meta("chunking_version") or ""
    with db.transaction():
        result.revision = int(db.get_meta("index_revision") or 0) + 1
        for (doc_id,) in db.con.execute(
                "SELECT DISTINCT doc_id FROM chunk_staging WHERE version=? ORDER BY doc_id",
                (CHUNKING_VERSION,)).fetchall():
            new = db.con.execute(
                "SELECT ordinal,locator,text,context,doc_hash,model,dim,vector FROM chunk_staging "
                "WHERE doc_id=? ORDER BY ordinal", (doc_id,)).fetchall()
            old = db.con.execute(
                "SELECT id,ordinal,locator,text,context FROM chunks WHERE doc_id=? ORDER BY ordinal,id",
                (doc_id,)).fetchall()
            if [tuple(row)[1:] for row in old] == [tuple(row)[:4] for row in new]:
                result.unchanged_documents += 1    # 번호·자식·임베딩을 그대로 둔다
                continue
            db.con.execute(
                "INSERT INTO chunk_archive(id,doc_id,ordinal,locator,text,context,doc_hash,revision,version,"
                "model,dim,vector) SELECT c.id,c.doc_id,c.ordinal,c.locator,c.text,c.context,?,?,?,"
                "e.model,e.dim,e.vector FROM chunks c LEFT JOIN embeddings e ON e.chunk_id=c.id WHERE c.doc_id=?",
                (new[0]["doc_hash"], result.revision, previous, doc_id))
            result.archived += len(old)
            # 새 조각을 먼저 넣는다 — 옛 번호보다 큰 번호를 받아, 보관한 번호와 겹치지 않는다.
            for row in new:
                chunk_id = db.insert_chunk(doc_id, row["ordinal"], row["locator"], row["text"], row["context"])
                if row["vector"] is not None:
                    db.save_chunk_embedding(chunk_id, row["model"], row["dim"], row["vector"])
            db.con.executemany("DELETE FROM chunks WHERE id=?", [(row["id"],) for row in old])
            result.changed_documents += 1
        db.con.execute("DELETE FROM chunk_staging")
        db.set_meta("chunking_version", CHUNKING_VERSION)
        db.set_meta("index_revision", str(result.revision))
        db.con.execute("DELETE FROM meta WHERE key IN ('chunks_checked', ?)", (HOLD,))
        db.audit("chunking.switch", detail=f"{previous or '?'} → {CHUNKING_VERSION} · 전환 {result.revision} · "
                 f"바뀐 문서 {result.changed_documents} · 보관 {result.archived}조각")
    return result


def cancel(db) -> int:
    """준비하던 새 조각을 버린다. 쓰던 색인은 그대로다."""
    with db.transaction():
        removed = db.con.execute("DELETE FROM chunk_staging").rowcount
        if removed:
            db.audit("chunking.cancel", detail=f"{removed}조각")
    return removed


def rollback(db) -> Switched:
    """마지막 전환을 되돌린다. 옛 조각이 제 번호와 벡터로 돌아온다.

    전환 뒤 원문 파일이 바뀐 문서는 되돌리지 않는다 — 옛 조각은 옛 판본의 글이다.
    되돌린 뒤에는 같은 규칙으로 다시 자동 전환하지 않는다(resume으로 푼다).
    """
    result = Switched(revision=int(db.get_meta("index_revision") or 0))
    rows = db.con.execute(
        "SELECT a.*, d.hash, d.filename FROM chunk_archive a JOIN documents d ON d.id=a.doc_id "
        "WHERE a.revision=? ORDER BY a.doc_id, a.ordinal, a.id", (result.revision,)).fetchall()
    if not result.revision or not rows:
        result.problems.append("되돌릴 전환 기록이 없습니다.")
        return result
    by_doc: dict[int, list] = {}
    for row in rows:
        by_doc.setdefault(row["doc_id"], []).append(row)
    with db.transaction():
        version = rows[0]["version"]
        for doc_id, old in by_doc.items():
            if (old[0]["hash"] or "") != old[0]["doc_hash"]:
                result.problems.append(f"{old[0]['filename']}: 전환 뒤 원문이 바뀌어 되돌리지 않았습니다.")
                continue
            db.con.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            for row in old:
                db.con.execute(
                    "INSERT INTO chunks(id,doc_id,ordinal,locator,text,context) VALUES (?,?,?,?,?,?)",
                    (row["id"], doc_id, row["ordinal"], row["locator"], row["text"], row["context"]))
                if row["vector"] is not None:
                    db.save_chunk_embedding(row["id"], row["model"], row["dim"], row["vector"])
            db.con.execute("DELETE FROM chunk_archive WHERE doc_id=? AND revision=?", (doc_id, result.revision))
            result.changed_documents += 1
        db.con.execute("DELETE FROM chunk_staging")
        db.set_meta("chunking_version", version or CHUNKING_VERSION)
        db.set_meta("index_revision", str(result.revision - 1))
        db.set_meta(HOLD, CHUNKING_VERSION)
        db.con.execute("DELETE FROM meta WHERE key='chunks_checked'")
        db.audit("chunking.rollback", detail=f"전환 {result.revision} · 문서 {result.changed_documents}")
    return result


def resume(db) -> None:
    """되돌린 뒤 멈춰 둔 자동 전환을 다시 허용한다."""
    db.con.execute("DELETE FROM meta WHERE key IN (?, 'chunks_checked')", (HOLD,))


def prune(db) -> int:
    """보관한 옛 조각을 지운다. 그 조각을 인용한 메모·지식은 다시 검토해야 한다."""
    with db.transaction():
        removed = db.con.execute("DELETE FROM chunk_archive").rowcount
        if removed:
            db.audit("chunking.prune", detail=f"{removed}조각")
    return removed


def run(db, client=None, should_stop: Callable[[], bool] = lambda: False,
        on_progress: Callable[[int, int, str], None] = lambda done, total, name: None) -> tuple[Switched | None, str]:
    """필요하면 준비부터 전환까지 한다. (전환 결과 또는 None, 사람이 읽을 상태 문구)를 돌려준다.

    임베딩이 있던 색인은 새 조각의 임베딩이 다 준비된 뒤에만 바꾼다. 모델이 없으면
    준비한 데까지 남기고 옛 색인을 계속 쓴다.
    """
    if not needed(db):
        return None, ""
    stage(db, should_stop, on_progress)
    if should_stop():
        return None, "색인 전환 준비를 멈췄습니다. 다음 분석에서 이어집니다."
    had_vectors = db.con.execute("SELECT model FROM embeddings LIMIT 1").fetchone()
    model = ""
    if had_vectors:
        if client is None or not client.health().embedding_ready:
            return None, "새 조각의 임베딩을 기다리는 중입니다. 로컬 AI가 준비되면 색인을 바꿉니다."
        error = embed(db, client, should_stop)
        if error:
            return None, ("색인 전환 준비를 멈췄습니다. 다음 분석에서 이어집니다." if error == "cancelled"
                          else f"새 조각의 임베딩이 중단됐습니다: {error}")
        model = client.embed_model
    result = switch(db, model)
    if not result.ok:
        return result, "색인을 바꾸지 않았습니다 — " + result.problems[0]
    return result, (f"조각 규칙 {CHUNKING_VERSION}로 색인 전환 · 바뀐 문서 {result.changed_documents}건"
                    f"(그대로 {result.unchanged_documents}건)")
