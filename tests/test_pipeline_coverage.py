"""Coverage and failure-state counterexamples, using only disposable source files."""
import hashlib
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import coverage
from app.db import Database
from app.ingest.scanner import scan_source
from app.jobs.pipeline import Pipeline, StageReport, STAGE_CHUNKS
from app.ingest.parsers.base import ParseResult, FAILED


@pytest.fixture
def db(tmp_path):
    value = Database(tmp_path / "project" / "project.db")
    value.init()
    yield value
    value.close()


def source(db, tmp_path):
    root = tmp_path / "source"
    root.mkdir(exist_ok=True)
    return root, db.add_source(root)


def test_scan_explains_files_and_counts_unknown_directory_contents_separately(db, tmp_path):
    root, sid = source(db, tmp_path)
    for name in ("read.txt", "photo.jpg", "~$locked.docx", "omit.txt"):
        (root / name).write_text("원본", encoding="utf-8")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "unknown.txt").write_text("unknown", encoding="utf-8")
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    scan_source(db, sid, root, excludes="omit*")
    view = coverage.snapshot(db)["scans"][0]
    assert view["observed_files"] == 4
    assert (view["included_files"], view["unsupported_files"], view["excluded_files"]) == (1, 1, 2)
    assert view["excluded_directories"] == 1
    assert view["uninspected_directory_contents"]
    assert {p: p.read_bytes() for p in before} == before
    db.close()
    assert coverage.snapshot(db)["scans"][0] == view


def test_excluded_previously_seen_file_is_not_called_deleted(db, tmp_path):
    root, sid = source(db, tmp_path)
    (root / "omit.txt").write_text("body", encoding="utf-8")
    scan_source(db, sid, root)
    scan_source(db, sid, root, excludes="omit*")
    assert db.con.execute("SELECT missing_since FROM documents").fetchone()[0] is None


def test_missing_directory_is_an_error_not_a_successful_empty_scan(db, tmp_path):
    root, sid = source(db, tmp_path)
    (root / "keep.txt").write_text("body", encoding="utf-8")
    scan_source(db, sid, root)
    scan_source(db, sid, root / "missing")
    view = coverage.snapshot(db)["scans"][0]
    assert view["state"] == "partial" and view["unreadable_directories"] == 1
    assert db.con.execute("SELECT missing_since FROM documents").fetchone()[0] is None


def test_cancelled_scan_does_not_mark_unvisited_documents_missing(db, tmp_path):
    root, sid = source(db, tmp_path)
    for index in range(3):
        (root / f"{index}.txt").write_text("body", encoding="utf-8")
    scan_source(db, sid, root)
    calls = iter([False, True, False])
    stats = scan_source(db, sid, root, should_stop=lambda: next(calls, False))
    assert stats.cancelled
    assert coverage.snapshot(db)["scans"][0]["state"] == "cancelled"
    assert not db.con.execute("SELECT 1 FROM documents WHERE missing_since IS NOT NULL").fetchone()


def test_hash_recheck_detects_same_size_and_timestamp_change(db, tmp_path):
    root, sid = source(db, tmp_path)
    path = root / "same.txt"
    path.write_bytes(b"old")
    stamp = path.stat()
    scan_source(db, sid, root)
    doc = db.con.execute("SELECT id FROM documents").fetchone()[0]
    db.update_document(doc, hash=hashlib.sha256(b"old").hexdigest())
    path.write_bytes(b"new")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert scan_source(db, sid, root).unchanged == 1
    assert scan_source(db, sid, root, verify_hash=True).changed == 1
    assert db.con.execute("SELECT hash FROM documents").fetchone()[0] is None


def test_counts_use_unique_documents_and_selected_model(db, tmp_path):
    from app.search.vector import pack
    root, sid = source(db, tmp_path)
    doc = db.upsert_document(sid, {"path": str(root / "one.txt"), "filename": "one.txt",
                                  "ext": ".txt", "parse_status": "ok", "hash": "one"})
    db.replace_sections(doc, [("paragraph", 1, "1문단", "업무 본문")])
    chunk = db.replace_document_chunks(doc, [(1, "1문단", "업무 본문")])[0][0]
    db.save_chunk_embedding(chunk, "old", 2, pack([1., 0.]))
    for name in ("one", "two"):
        task = db.con.execute("INSERT INTO tasks(name) VALUES(?)", (name,)).lastrowid
        db.con.execute("INSERT INTO task_docs(task_id,doc_id) VALUES(?,?)", (task, doc))
    view = coverage.snapshot(db, "new", include_documents=True)
    assert view["classifiable_documents"] == view["assigned_documents"] == 1
    assert view["embedded_chunks"] == 0 and view["unassigned_documents"] == 0
    assert view["extraction_complete_documents"] is None
    assert view["documents"][0]["extraction_completeness"] == "unverified"


def test_reparse_failure_removes_previous_searchable_content(db, tmp_path, monkeypatch):
    import app.jobs.pipeline as module
    root, sid = source(db, tmp_path)
    doc = db.upsert_document(sid, {"path": str(root / "one.txt"), "filename": "one.txt",
        "ext": ".txt", "parse_status": "ok", "hash": "one", "parser_version": "1"})
    db.replace_sections(doc, [("paragraph", 1, "1문단", "obsolete body")])
    db.index_document(doc, "one.txt", str(root / "one.txt"), "", "obsolete body")
    db.replace_document_chunks(doc, [(1, "1문단", "obsolete body")])
    monkeypatch.setattr(module, "_parse_with_retry", lambda _path: ParseResult(FAILED, "text", error="failed"))
    Pipeline(db.path)._parse(db)
    for table in ("document_sections", "document_index", "chunks"):
        assert db.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert db.con.execute("SELECT reason FROM processing_issues WHERE stage='parse'").fetchone()[0] == "failed"


def test_partial_stage_is_persistent_and_never_shown_as_success(db):
    from app.ui.status_dialog import _stage_line
    report = StageReport("내용 읽기", 1, 1, "읽지 못함", ["failed"])
    Pipeline(db.path)._report(db, report)
    assert "✓" not in _stage_line(report.stage, report)
    db.close()
    assert db.con.execute("SELECT state FROM pipeline_stage_results").fetchone()[0] == "partial"


def test_parent_embedding_failure_does_not_set_chunks_checked(db, tmp_path, monkeypatch):
    import app.jobs.pipeline as module
    root, sid = source(db, tmp_path)
    doc = db.upsert_document(sid, {"path": str(root / "one.txt"), "filename": "one.txt",
                                  "ext": ".txt", "parse_status": "ok"})
    db.replace_sections(doc, [("paragraph", 1, "1문단", "업무 본문")])
    class Client:
        embed_model = "fake"
        def health(self):
            return SimpleNamespace(embedding_ready=True)
        def embed(self, texts):
            return [], "failed"
    monkeypatch.setattr(module, "project_client", lambda *args: Client())
    monkeypatch.setattr("app.core.child_index.embed_children", lambda *args: None)
    monkeypatch.setattr(db, "unembedded_child_count", lambda model: 0)
    db.set_meta("chunks_checked", "1")
    Pipeline(db.path)._chunks(db)
    assert db.get_meta("chunks_checked") is None
    assert db.con.execute("SELECT state FROM pipeline_stage_results WHERE stage=?", (STAGE_CHUNKS,)).fetchone()[0] == "partial"


def test_v8_upgrade_backs_up_and_preserves_reviewed_data(db):
    task = db.con.execute("INSERT INTO tasks(name,origin,status) VALUES('사람 업무','user','edited')").lastrowid
    db.set_meta("schema_version", "8")
    db.close()
    db.init()
    assert db.get_meta("schema_version") == "9"
    assert db.task(task)["name"] == "사람 업무"
    backup = Path(str(db.path) + ".bak-v8")
    assert backup.exists()
    with sqlite3.connect(backup) as old:
        assert old.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "8"


@pytest.mark.parametrize("vectors", [[], [[1., 0.]], [[float('nan'), 1.], [1., 0.]], [[0., 0.], [0., 0.]]])
def test_invalid_embedding_batches_cannot_be_reported_complete(vectors):
    from app.jobs.pipeline import _valid_vectors
    assert not _valid_vectors(vectors, 2)
