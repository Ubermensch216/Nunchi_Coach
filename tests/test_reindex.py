"""조각 규칙이 바뀐 프로젝트의 색인 전환 — 준비·검증·전환·되돌리기 (노트북 RAG 2단계).

지키는 것: 전환하는 동안에도 옛 조각으로 답한다. 조각이 달라지지 않은 문서는 번호까지
그대로다. 달라진 문서의 옛 조각은 보관되어, 원문 파일이 그대로인 한 저장된 인용을
계속 대조할 수 있다. 전환은 전부 되거나 하나도 되지 않고, 되돌릴 수 있다.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from app.core import notebook, reindex
from app.core.chunking import CHUNKING_VERSION, MAX_CHUNK_CHARS
from app.db import Database
from app.search.vector import pack

MODEL = "bge-m3"
ROWS = [f"항목{i}\t{i * 1000:,}원\t담당 {i}명" for i in range(1, 200)]
LONG = "\n".join(ROWS)                       # 예전 규칙은 이 표를 1,200자마다 잘랐다
SHORT = "제출기한: 9월 30일까지 총무과로 제출한다."


class Embedder:
    embed_model = MODEL

    def __init__(self, ready=True, fail_after=None):
        self.ready, self.fail_after, self.texts = ready, fail_after, []

    def health(self):
        return SimpleNamespace(embedding_ready=self.ready, generation_ready=False, message="")

    def embed(self, texts):
        if self.fail_after is not None and len(self.texts) >= self.fail_after:
            return None, "연결 끊김"
        self.texts += texts
        return [[1.0, 0.0, 0.0] for _ in texts], None


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "project.db")
    database.init()
    yield database
    database.close()


def add(db, tmp_path, name, kind, text, embedded=True):
    """예전 규칙(글자 수로 자르기)으로 색인해 둔 문서 하나."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    doc = db.upsert_document(db.add_source(tmp_path), {
        "path": str(path), "filename": name, "ext": ".txt", "parse_status": "ok",
        "hash": hashlib.sha256(path.read_bytes()).hexdigest()})
    db.replace_sections(doc, [(kind, 1, "표1" if kind == "table" else "1문단", text)])
    old = [text[i:i + MAX_CHUNK_CHARS] for i in range(0, len(text), MAX_CHUNK_CHARS)]
    locator = "표1" if kind == "table" else "1문단"
    ids = [cid for cid, _ in db.replace_document_chunks(doc, [(n, locator, piece) for n, piece in enumerate(old, 1)])]
    if embedded:
        for cid in ids:
            db.save_chunk_embedding(cid, MODEL, 3, pack([0.0, 1.0, 0.0]))
    return doc, ids


@pytest.fixture
def project(db, tmp_path):
    table, table_ids = add(db, tmp_path, "표.txt", "table", LONG)
    plain, plain_ids = add(db, tmp_path, "요청.txt", "paragraph", SHORT)
    db.set_meta("chunking_version", "2")
    return SimpleNamespace(table=table, table_ids=table_ids, plain=plain, plain_ids=plain_ids)


def chunk_ids(db, doc):
    return [row[0] for row in db.con.execute("SELECT id FROM chunks WHERE doc_id=? ORDER BY ordinal", (doc,))]


def test_reopening_an_old_project_keeps_its_chunks_until_the_new_index_is_ready(db, project):
    """예전에는 프로젝트를 여는 순간 조각을 전부 지웠다 — 그동안 질문할 수 없었다."""
    db.set_meta("chunks_checked", "1")
    assert db.sync_chunking_version() is False
    assert chunk_ids(db, project.table) == project.table_ids
    assert db.get_meta("chunking_version") == "2" and db.get_meta("chunks_checked") is None
    assert reindex.needed(db)


def test_switch_replaces_only_documents_whose_chunks_differ_and_archives_the_old_ones(db, project):
    client = Embedder()
    result, note = reindex.run(db, client)
    assert result.ok and result.changed_documents == 1 and result.unchanged_documents == 1
    assert db.get_meta("chunking_version") == CHUNKING_VERSION and "색인 전환" in note
    assert chunk_ids(db, project.plain) == project.plain_ids            # 번호까지 그대로
    new = db.con.execute("SELECT id, text FROM chunks WHERE doc_id=? ORDER BY ordinal", (project.table,)).fetchall()
    assert min(row["id"] for row in new) > max(project.table_ids)       # 옛 번호를 다시 쓰지 않는다
    assert "".join(row["text"] for row in new) == LONG
    assert all(line in ROWS for row in new for line in row["text"].splitlines())   # 행이 잘리지 않는다
    archived = db.con.execute("SELECT id, revision, version, vector FROM chunk_archive ORDER BY id").fetchall()
    assert [row["id"] for row in archived] == project.table_ids
    assert {(row["revision"], row["version"]) for row in archived} == {(1, "2")}
    assert all(row["vector"] is not None for row in archived)
    # 새 조각은 전부 임베딩이 있고, 글이 달라진 조각만 새로 계산했다.
    assert db.unembedded_chunk_count(MODEL) == 0 and 0 < len(client.texts) <= len(new)
    assert not reindex.needed(db) and db.con.execute("SELECT COUNT(*) FROM chunk_staging").fetchone()[0] == 0


def test_saved_citation_of_a_replaced_chunk_stays_valid_while_the_file_is_unchanged(db, project, tmp_path):
    book = notebook.create(db, "정산")
    notebook.add_sources(db, book, [project.table])
    document = db.document(project.table)
    old = db.con.execute("SELECT locator, text FROM chunks WHERE id=?", (project.table_ids[1],)).fetchone()
    proof = [{"doc_id": project.table, "locator": old["locator"], "exact_quote": old["text"],
              "version_hash": document["hash"]}]
    assert notebook.evidence_current(db, book, proof, check_file=True)
    assert reindex.run(db, Embedder())[0].ok
    assert not db.con.execute("SELECT 1 FROM chunks WHERE text=?", (old["text"],)).fetchone()
    assert notebook.evidence_current(db, book, proof, check_file=True)
    # 보관한 조각은 그 판본의 것이다. 다른 판본의 인용까지 살려 주지 않는다.
    assert not notebook.evidence_current(db, book, [{**proof[0], "version_hash": "다른판본"}])
    reindex.prune(db)
    assert not notebook.evidence_current(db, book, proof)


def test_old_index_keeps_answering_while_embeddings_are_not_ready(db, project):
    result, note = reindex.run(db, Embedder(ready=False))
    assert result is None and "임베딩" in note
    assert chunk_ids(db, project.table) == project.table_ids and db.get_meta("chunking_version") == "2"
    assert reindex.status(db, MODEL).staged == 2 and reindex.needed(db)


def test_interrupted_preparation_resumes_from_what_was_saved(db, project, monkeypatch):
    monkeypatch.setattr(reindex, "EMBED_BATCH", 1)
    broken = Embedder(fail_after=1)
    result, note = reindex.run(db, broken)
    assert result is None and "중단" in note and chunk_ids(db, project.table) == project.table_ids
    left = reindex.status(db, MODEL).unembedded
    assert left > 0
    client = Embedder()
    stops = iter([False] * 50)
    result, _ = reindex.run(db, client, should_stop=lambda: next(stops, False))
    assert result.ok and len(client.texts) == left          # 이미 저장한 배치는 다시 하지 않는다


def test_cancel_before_the_switch_leaves_the_old_index_untouched(db, project):
    reindex.stage(db)
    assert reindex.cancel(db) > 0
    assert chunk_ids(db, project.table) == project.table_ids
    assert db.con.execute("SELECT COUNT(*) FROM chunk_archive").fetchone()[0] == 0


def test_nothing_is_switched_when_verification_finds_a_problem(db, project):
    reindex.stage(db)
    db.con.execute("DELETE FROM chunk_staging WHERE doc_id=? AND ordinal>1", (project.table,))
    result = reindex.switch(db)
    assert not result.ok and "짧습니다" in result.problems[0]
    assert chunk_ids(db, project.table) == project.table_ids and db.get_meta("chunking_version") == "2"


def test_material_that_changed_after_staging_is_prepared_again(db, project):
    reindex.stage(db)
    db.update_document(project.table, hash="새판본")
    assert any("판본" in problem for problem in reindex.verify(db))
    assert reindex.stage(db) == 1                            # 그 문서만 다시 준비한다
    assert reindex.verify(db) == []


def test_rollback_restores_the_old_chunks_with_their_ids_and_holds_further_switching(db, project):
    assert reindex.run(db, Embedder())[0].ok
    result = reindex.rollback(db)
    assert result.changed_documents == 1 and not result.problems
    assert chunk_ids(db, project.table) == project.table_ids
    assert db.unembedded_chunk_count(MODEL) == 0
    assert db.get_meta("chunking_version") == "2" and db.get_meta("index_revision") == "0"
    assert db.con.execute("SELECT COUNT(*) FROM chunk_archive").fetchone()[0] == 0
    # 되돌린 까닭이 있을 것이다 — 같은 규칙으로 저절로 다시 바꾸지 않는다.
    assert not reindex.needed(db) and reindex.status(db).held
    assert reindex.run(db, Embedder()) == (None, "")
    reindex.resume(db)
    assert reindex.needed(db) and reindex.run(db, Embedder())[0].ok


def test_rollback_skips_a_document_whose_file_changed_after_the_switch(db, project):
    assert reindex.run(db, Embedder())[0].ok
    db.update_document(project.table, hash="새판본")
    result = reindex.rollback(db)
    assert result.changed_documents == 0 and "원문이 바뀌어" in result.problems[0]
    assert chunk_ids(db, project.table) != project.table_ids


def test_project_without_embeddings_switches_on_text_search_alone(db, tmp_path):
    doc, ids = add(db, tmp_path, "표.txt", "table", LONG, embedded=False)
    db.set_meta("chunking_version", "2")
    result, _ = reindex.run(db, None)
    assert result.ok and result.changed_documents == 1 and chunk_ids(db, doc) != ids


def test_new_chunk_never_reuses_the_number_of_an_archived_one(db, project):
    assert reindex.run(db, Embedder())[0].ok
    db.con.execute("DELETE FROM chunks")
    fresh = db.insert_chunk(project.plain, 1, "1문단", SHORT)
    assert fresh > max(project.table_ids)


def test_pipeline_switches_the_index_and_reports_it(db, project, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import app.jobs.pipeline as pipeline_module
    from app.ai.client import Health

    class Fake(Embedder):
        def __init__(self, gen_model="gen", embed_model=MODEL, **_kw):
            super().__init__()

        def health(self):
            return Health(ok=True, message="", embedding_ready=True)

    monkeypatch.setattr(pipeline_module, "OllamaClient", Fake)
    reports = []
    pipeline = pipeline_module.Pipeline(db.path)
    pipeline.stage_done.connect(reports.append)
    pipeline._chunks(db)
    assert "색인 전환" in reports[-1].note
    assert db.get_meta("chunking_version") == CHUNKING_VERSION and db.get_meta("chunks_checked") == "1"
    assert db.unembedded_child_count(MODEL) == 0
