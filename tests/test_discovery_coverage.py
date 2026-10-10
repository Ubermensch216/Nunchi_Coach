"""Controlled-vector counterexamples; these are not real-model accuracy scores."""
from types import SimpleNamespace

import pytest

from app.ai.discover import discover
from app.ai.analyze import TaskNaming
from app.core import discovery_index
from app.core.clustering import cluster
from app.db import Database
from app.search.vector import VectorStore, pack


@pytest.fixture
def db(tmp_path):
    value = Database(tmp_path / "project.db")
    value.init()
    yield value
    value.close()


def add(db, tmp_path, name, topic, tail_vector, prefix_vector=(1., 0., 0.), body=True):
    sid = db.add_source(tmp_path / "source")
    prefix = "공통 서식과 기관 안내입니다. " * 100
    text = prefix + "\n" + topic
    doc = db.upsert_document(sid, {"path": str(tmp_path / "source" / name), "filename": name,
        "ext": ".txt", "parse_status": "ok", "hash": name, "char_count": len(text)})
    db.save_doc_embedding(doc, "fake", 3, pack(prefix_vector), 1200)
    if body:
        db.replace_sections(doc, [("paragraph", 1, "1문단", prefix), ("paragraph", 2, "2문단", topic)])
        chunks = db.replace_document_chunks(doc, [(1, "1문단", prefix), (2, "2문단", topic)])
        db.save_chunk_embedding(chunks[0][0], "fake", 3, pack(prefix_vector))
        db.save_chunk_embedding(chunks[1][0], "fake", 3, pack(tail_vector))
    db.set_meta("embed_model", "fake")
    return doc


def test_identical_first_1200_chars_no_longer_merge_different_late_businesses(db, tmp_path):
    expected = []
    for name, axis, topic in (("예산", (0., 1., 0.), "사업 A의 예산 정산"),
                               ("감사", (0., 0., 1.), "사업 B의 감사 대응")):
        expected.append({add(db, tmp_path, f"{name}_{i}.txt", topic + str(i), axis) for i in range(3)})
    baseline = cluster(VectorStore.load(db.con, "fake"))
    assert len(baseline.clusters) == 1
    result = discover(db)
    actual = [{r["id"] for r in db.task_documents(t["id"])} for t in db.tasks()]
    assert result.tasks == 2 and all(group in actual for group in expected)
    rows = list(db.con.execute("SELECT * FROM discovery_evidence"))
    assert len(rows) == 12 and all(r["basis"] == "full_chunks" for r in rows)
    assert all(r["locator"] and r["index_version"] and r["doc_hash"] for r in rows)
    assert any(r["locator"] == "2문단" for r in rows)
    assert not any(r["reviewed_by_model"] for r in rows)


def test_single_body_document_is_weak_candidate_not_silently_discarded(db, tmp_path):
    doc = add(db, tmp_path, "유일한업무.txt", "필수 업무 접수", (0., 1., 0.))
    result = discover(db)
    task = db.tasks()[0]
    assert result.singleton_candidates == 1
    assert task["confidence"] == "low" and task["review_state"] == "weak"
    assert task["status"] == "proposed"
    assert db.task_documents(task["id"])[0]["id"] == doc


def test_same_name_is_not_a_merge_key(db, tmp_path, monkeypatch):
    for axis, topic in (((0., 1., 0.), "사업 A"), ((0., 0., 1.), "사업 B")):
        for i in range(3):
            add(db, tmp_path, f"{topic}_{i}.txt", topic + str(i), axis)
    client = SimpleNamespace(embed_model="fake", health=lambda: SimpleNamespace(generation_ready=True))
    monkeypatch.setattr("app.ai.discover.name_task", lambda *a: TaskNaming("같은이름", "본문 확인", True))
    discover(db, client)
    assert len(db.tasks()) == 2
    assert all(t["doc_count"] == 3 for t in db.tasks())


def test_coherence_rejection_is_held_without_naming_fallback_assignment(db, tmp_path, monkeypatch):
    for i in range(3):
        add(db, tmp_path, f"같은양식_{i}.txt", "같은 벡터여도 다른 업무일 수 있음", (0., 1., 0.))
    client = SimpleNamespace(embed_model="fake", health=lambda: SimpleNamespace(generation_ready=True))
    monkeypatch.setattr("app.ai.discover.name_task", lambda *a: TaskNaming(None, "서로 다른 업무", False))
    result = discover(db, client)
    assert result.tasks == 0 and result.unassigned == 3 and result.held_groups == 1
    assert db.con.execute("SELECT reason FROM discovery_holds").fetchone()[0] == "coherence_rejected"


def test_cancelled_rediscovery_keeps_previous_tasks_and_assignments(db, tmp_path, monkeypatch):
    for i in range(3):
        add(db, tmp_path, f"업무_{i}.txt", "같은 업무" + str(i), (0., 1., 0.))
    discover(db)
    before = [tuple(r) for r in db.con.execute("SELECT * FROM tasks")]
    links = [tuple(r) for r in db.con.execute("SELECT * FROM task_docs")]
    client = SimpleNamespace(embed_model="fake", cancelled=False,
                             health=lambda: SimpleNamespace(generation_ready=True))
    def cancel(*args):
        client.cancelled = True
        return TaskNaming("바뀐이름", "", True)
    monkeypatch.setattr("app.ai.discover.name_task", cancel)
    assert discover(db, client).cancelled
    assert [tuple(r) for r in db.con.execute("SELECT * FROM tasks")] == before
    assert [tuple(r) for r in db.con.execute("SELECT * FROM task_docs")] == links


def test_human_detachment_does_not_return_as_a_new_singleton(db, tmp_path):
    doc = add(db, tmp_path, "유일업무.txt", "업무", (0., 1., 0.))
    discover(db)
    db.detach_document(db.tasks()[0]["id"], doc)
    discover(db)
    assert not db.con.execute("SELECT 1 FROM task_docs WHERE doc_id=?", (doc,)).fetchone()


def test_incomplete_chunk_index_uses_explicit_prefix_fallback(db, tmp_path):
    doc = add(db, tmp_path, "업무.txt", "업무", (0., 1., 0.))
    db.con.execute("DELETE FROM embeddings WHERE chunk_id=(SELECT MAX(id) FROM chunks)")
    store, evidence = discovery_index.load(db, "fake")
    assert store.size == 1
    assert evidence[doc][0]["basis"] == "document_prefix"
    assert evidence[doc][0]["chunk_id"] is None


def test_naming_receives_body_evidence_and_records_only_supplied_chunks(db, tmp_path, monkeypatch):
    for i in range(3):
        add(db, tmp_path, f"업무_{i}.txt", "후반부 제출처는 예산팀" + str(i), (0., 1., 0.))
    received = []
    def naming(_client, _titles, evidence):
        received.extend(evidence)
        return TaskNaming("예산업무", "본문 발췌에 근거한 후보", True)
    monkeypatch.setattr("app.ai.discover.name_task", naming)
    client = SimpleNamespace(embed_model="fake", health=lambda: SimpleNamespace(generation_ready=True))
    discover(db, client)
    assert any("예산팀" in r["text"] for r in received)
    reviewed = {r[0] for r in db.con.execute("SELECT chunk_id FROM discovery_evidence WHERE reviewed_by_model=1")}
    assert reviewed == {r["chunk_id"] for r in received}


def test_uploads_are_not_discovered_as_predecessor_businesses(db, tmp_path):
    add(db, tmp_path, "노트.txt", "본문", (0., 1., 0.))
    db.con.execute("UPDATE sources SET kind='upload'")
    store, _ = discovery_index.load(db, "fake")
    assert store.size == 0
