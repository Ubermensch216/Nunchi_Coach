from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.knowledge.ontology import canonical_json, claim_key, normalize_label
from app.core.knowledge.validation import validate_claim
from app.db import Database
from app.db.knowledge import KnowledgeStore
from app.db import migrations

FIXTURES = Path(__file__).parent / "fixtures" / "knowledge"
CANDIDATES = json.loads((FIXTURES / "constraints.json").read_text(encoding="utf-8"))


@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path / "project.db")
    db.init()
    yield db
    db.close()


def sql_candidate(db, candidate):
    subject, obj = str(uuid4()), str(uuid4())
    db.con.execute("INSERT INTO kg_entities(uuid,type,label) VALUES (?,?,'subject')", (subject, candidate["subject_type"]))
    if candidate.get("object_type"):
        db.con.execute("INSERT INTO kg_entities(uuid,type,label) VALUES (?,?,'object')", (obj, candidate["object_type"]))
    else:
        obj = None
    uid = str(uuid4())
    db.con.execute("INSERT INTO kg_claims(uuid,claim_key,subject_id,predicate,object_id,value_json,value_type,origin) "
                   "VALUES (?,?,?,?,?,?,?,'projection')",
                   (uid, uid, subject, candidate["predicate"], obj,
                    canonical_json(candidate["value"]) if candidate.get("value") is not None else None,
                    candidate.get("value_type")))


@pytest.mark.parametrize("candidate", CANDIDATES, ids=lambda c: c["id"])
def test_python_and_sql_contract(db, candidate):
    data = {k: v for k, v in candidate.items() if k not in ("id", "valid")}
    if candidate["valid"]:
        validate_claim(**data)
        sql_candidate(db, candidate)
    else:
        with pytest.raises(ValueError):
            validate_claim(**data)
        with pytest.raises(sqlite3.IntegrityError):
            sql_candidate(db, candidate)


def schema(con):
    return [tuple(row) for row in con.execute("SELECT type,name,sql FROM sqlite_master "
                                             "WHERE name LIKE 'kg_%' OR name LIKE 'idx_kg_%' ORDER BY name")]


def strip_graph(db):
    for row in list(db.con.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'kg_%'")):
        db.con.execute(f'DROP TRIGGER "{row[0]}"')
    for row in list(db.con.execute("SELECT name FROM sqlite_master WHERE type='view' AND name LIKE 'kg_%'")):
        db.con.execute(f'DROP VIEW "{row[0]}"')
    db.con.execute("PRAGMA foreign_keys=OFF")
    for row in list(db.con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'kg_%'")):
        db.con.execute(f'DROP TABLE "{row[0]}"')
    db.con.execute("PRAGMA foreign_keys=ON")
    db.con.execute("DELETE FROM meta WHERE key LIKE 'kg_%' OR key='project_uuid'")
    db.set_meta("schema_version", "11")


def test_v11_upgrade_matches_new_db_and_preserves_records(db, tmp_path):
    db.con.execute("INSERT INTO tasks(id,name) VALUES (1,'사람이 정정한 업무')")
    db.con.execute("INSERT INTO work_cases(task_id,period_year,label) VALUES (1,2026,'수행 건')")
    before = [tuple(r) for r in db.con.execute("SELECT * FROM work_cases")]
    strip_graph(db)
    db.init()
    fresh = Database(tmp_path / "fresh.db")
    try:
        fresh.init()
        assert schema(db.con) == schema(fresh.con)
        assert [tuple(r) for r in db.con.execute("SELECT * FROM work_cases")] == before
        assert db.con.execute("SELECT reason FROM kg_jobs WHERE partition_key='task:1'").fetchone()[0] == "bootstrap"
        assert db.con.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0] == 0
        with sqlite3.connect(migrations.backup_path(db.path, "11")) as backup:
            assert backup.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "11"
            assert backup.execute("SELECT name FROM tasks").fetchone()[0] == "사람이 정정한 업무"
        project_id = db.get_meta("project_uuid")
        db.init()
        assert db.get_meta("project_uuid") == project_id
        assert db.con.execute("SELECT COUNT(*) FROM kg_jobs").fetchone()[0] == 1
    finally:
        fresh.close()


def test_failed_v12_rolls_back_ddl_uuid_and_queue_then_retries(db, monkeypatch):
    strip_graph(db)
    original = migrations.STEPS["12"]

    def fail(con):
        original(con)
        raise RuntimeError("injected migration failure")

    monkeypatch.setitem(migrations.STEPS, "12", fail)
    with pytest.raises(migrations.MigrationError):
        db.init()
    assert db.get_meta("schema_version") == "11"
    assert db.get_meta("project_uuid") is None
    assert not db.con.execute("SELECT 1 FROM sqlite_master WHERE name='kg_entities'").fetchone()
    monkeypatch.setitem(migrations.STEPS, "12", original)
    db.init()
    assert db.get_meta("schema_version") == migrations.SCHEMA_VERSION


def test_older_reader_refuses_v12_without_modification(db, monkeypatch):
    original = schema(db.con)
    monkeypatch.setattr(migrations, "SCHEMA_VERSION", "11")
    with pytest.raises(migrations.MigrationError, match="더 새로운 버전"):
        migrations.upgrade(db.con, db.path, "12")
    assert schema(db.con) == original


def test_new_database_failure_does_not_publish_schema_version(tmp_path, monkeypatch):
    from app.db import knowledge

    fresh = Database(tmp_path / "new.db")
    original = knowledge.install

    def fail(con):
        original(con)
        raise RuntimeError("new DB failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(knowledge, "install", fail)
            with pytest.raises(RuntimeError):
                fresh.init()
        assert fresh.get_meta("schema_version") is None
        assert fresh.get_meta("project_uuid") is None
        assert not fresh.con.execute("SELECT 1 FROM sqlite_master WHERE name='kg_entities'").fetchone()
        fresh.init()
        assert fresh.get_meta("schema_version") == migrations.SCHEMA_VERSION
    finally:
        fresh.close()


def test_binding_claim_dedup_and_append_only_decisions(db):
    store = KnowledgeStore(db)
    task = store.bind("tasks", "1", "Task", "업무")
    assert store.bind("tasks", "1", "Task", "업무 수정") == task
    other = store.bind("tasks", "2", "Task", "업무 수정")
    assert other != task
    first = store.put_claim(task, "due_date", value="2026-11-01", value_type="date")
    decision = store.record_decision("claim", db.con.execute("SELECT claim_key FROM kg_claims").fetchone()[0], "reject", "후임자")
    assert store.put_claim(task, "due_date", value="2026-11-01", value_type="date", origin="model") == first
    undo = store.record_decision("claim", db.con.execute("SELECT claim_key FROM kg_claims").fetchone()[0], "undo", "후임자", supersedes_decision=decision)
    assert undo != decision
    for statement in ("UPDATE kg_decisions SET actor='changed'", "DELETE FROM kg_decisions",
                      "UPDATE kg_claims SET value_json='\"2026-12-01\"'", "DELETE FROM kg_claims"):
        with pytest.raises(sqlite3.IntegrityError):
            db.con.execute(statement)
    for state in ("active", "retracted", "active"):
        db.con.execute("UPDATE kg_claims SET lifecycle=?", (state,))
    assert db.con.execute("SELECT COUNT(*) FROM kg_claim_events").fetchone()[0] == 5


def test_quote_anchor_survives_chunk_and_document_deletion(db):
    source = db.add_source("/source")
    doc = db.upsert_document(source, {"path": "/source/a.txt", "filename": "a.txt", "ext": ".txt"})
    chunk = db.con.execute("INSERT INTO chunks(doc_id,ordinal,locator,text) VALUES (?,0,'본문','원문')", (doc,)).lastrowid
    store = KnowledgeStore(db)
    anchor = store.put_evidence("document_quote", doc_uuid="doc-stable", doc_id=doc, doc_hash="hash",
        parse_fingerprint="parse-v1", locator="본문", exact_quote="원문", chunk_id=chunk)
    db.con.execute("DELETE FROM chunks WHERE id=?", (chunk,))
    assert db.con.execute("SELECT chunk_id FROM kg_evidence").fetchone()[0] is None
    assert store.put_evidence("document_quote", doc_uuid="doc-stable", doc_id=doc, doc_hash="hash",
        parse_fingerprint="parse-v1", locator="본문", exact_quote="원문") == anchor
    db.con.execute("DELETE FROM documents WHERE id=?", (doc,))
    row = db.con.execute("SELECT * FROM kg_evidence").fetchone()
    assert row["doc_id"] is None and row["exact_quote"] == "원문" and row["doc_hash"] == "hash"
    with pytest.raises(sqlite3.IntegrityError):
        db.con.execute("UPDATE kg_evidence SET exact_quote='changed'")


def test_ambiguous_aliases_keep_both_entities_and_origins(db):
    store = KnowledgeStore(db)
    evidence = store.put_evidence("source_record", source_record_snapshot={"source": "explicit fixture"})
    for key in ("a", "b"):
        entity = store.bind("fixture", key, "Organization", "복지과", key)
        store.add_alias(entity, " 복지과 ", source_key=key, evidence_id=evidence)
        store.add_alias(entity, "복지과", source_key=key, evidence_id=evidence)
    assert db.con.execute("SELECT COUNT(*) FROM kg_aliases").fetchone()[0] == 2
    assert normalize_label("  1구청 / 복지과  ") == "1구청 / 복지과"
    assert normalize_label("서식-01") != normalize_label("서식01")


def test_invalid_alias_and_claim_link_are_rejected_instead_of_silently_ignored(db):
    store = KnowledgeStore(db)
    task = store.bind("tasks", "1", "Task", "업무")
    with pytest.raises(sqlite3.IntegrityError):
        store.add_alias(task, "별칭", source_key="invalid-no-provenance")
    claim = store.put_claim(task, "due_date", value="2026-11-01", value_type="date")
    evidence = store.put_evidence("source_record", source_record_snapshot={"source": "fixture"})
    with pytest.raises(sqlite3.IntegrityError):
        store.link_claims(claim, "conflicts", claim, evidence)
    assert db.con.execute("SELECT COUNT(*) FROM kg_aliases").fetchone()[0] == 0
    assert db.con.execute("SELECT COUNT(*) FROM kg_claim_links").fetchone()[0] == 0


def test_claim_key_scope_time_conditions_and_model_independence():
    args = {"subject_id": "task", "predicate": "amount", "value": {"amount": "100.00", "currency": "KRW"},
            "value_type": "decimal_amount", "conditions": {"b": 2, "a": 1}}
    key = claim_key(**args)
    assert claim_key(**(args | {"conditions": {"a": 1, "b": 2}})) == key
    for change in ({"scope_key": "2026"}, {"valid_from": "2026-01-01"}, {"conditions": {"a": 2}}):
        assert claim_key(**(args | change)) != key


def test_decimal_dedup_preserves_precision_and_unit(db):
    store = KnowledgeStore(db)
    task = store.bind("tasks", "1", "Task", "금액 업무")
    first = store.put_claim(task, "amount", value={"amount": "123456789012345678901234567890.5000", "currency": "KRW"}, value_type="decimal_amount")
    assert store.put_claim(task, "amount", value={"amount": "123456789012345678901234567890.50", "currency": "KRW"}, value_type="decimal_amount") == first
    amount = json.loads(db.con.execute("SELECT value_json FROM kg_claims WHERE uuid=?", (first,)).fetchone()[0])["amount"]
    assert amount == "123456789012345678901234567890.5"
    assert store.put_claim(task, "amount", value={"amount": "123456789012345678901234567890.50", "unit": "천원"}, value_type="decimal_amount") != first


def test_relation_and_claim_link_cycles_and_symmetric_conflicts(db):
    store = KnowledgeStore(db)
    steps = [store.bind("steps", str(i), "Step", str(i)) for i in range(3)]
    claims = [store.put_claim(steps[i], "PRECEDES", object_id=steps[i + 1], scope_key="task:1") for i in (0, 1)]
    with pytest.raises(sqlite3.IntegrityError, match="cycle"):
        store.put_claim(steps[2], "PRECEDES", object_id=steps[0], scope_key="task:1")
    evidence = store.put_evidence("source_record", source_record_snapshot={"change": "explicit"})
    store.link_claims(claims[0], "supersedes", claims[1], evidence)
    with pytest.raises(sqlite3.IntegrityError, match="cycle"):
        store.link_claims(claims[1], "supersedes", claims[0], evidence)
    store.link_claims(claims[0], "conflicts", claims[1], evidence)
    store.link_claims(claims[1], "conflicts", claims[0], evidence)
    assert len(store.claim_links(claims[0])) == len(store.claim_links(claims[1])) == 2


def test_indexes_are_used(db):
    queries = [("SELECT * FROM kg_claims WHERE subject_id=? AND predicate=?", ("task", "HAS_DOCUMENT"), "idx_kg_claim_subject"),
               ("SELECT * FROM kg_claims WHERE object_id=? AND predicate=?", ("doc", "HAS_DOCUMENT"), "idx_kg_claim_object"),
               ("SELECT * FROM kg_aliases WHERE normalized_label=? AND scope_key=?", ("복지과", "task:1"), "idx_kg_alias_lookup"),
               ("SELECT * FROM kg_evidence WHERE doc_id=? AND doc_hash=?", (1, "hash"), "idx_kg_evidence_doc"),
               ("SELECT * FROM kg_jobs WHERE state=?", ("dirty",), "idx_kg_jobs_state")]
    for sql, params, index in queries:
        plan = str([tuple(r) for r in db.con.execute("EXPLAIN QUERY PLAN " + sql, params)])
        assert index in plan


@pytest.mark.parametrize("start,end,valid", [
    (None, None, True), ("2026-01-01", None, True), (None, "2027-01-01", True),
    ("2026-01-01", "2027-01-01", True), ("2026-01-01", "2026-01-01", False),
    ("2027-01-01", "2026-01-01", False), ("2026-02-30", None, False),
])
def test_half_open_time_contract_in_python_and_sql(db, start, end, valid):
    store = KnowledgeStore(db)
    subject = store.bind("tasks", "1", "Task", "기간 업무")

    def check_python():
        validate_claim("Task", "due_date", value="2026-11-01", value_type="date", valid_from=start, valid_to=end)

    def check_sql():
        uid = str(uuid4())
        db.con.execute("INSERT INTO kg_claims(uuid,claim_key,subject_id,predicate,value_json,value_type,valid_from,valid_to,origin) "
                       "VALUES (?,?,?,'due_date','\"2026-11-01\"','date',?,?,'projection')", (uid, uid, subject, start, end))

    if valid:
        check_python()
        check_sql()
        row = db.con.execute("SELECT valid_from,valid_to FROM kg_claims").fetchone()
        assert tuple(row) == (start, end)
    else:
        with pytest.raises(ValueError):
            check_python()
        with pytest.raises(sqlite3.IntegrityError):
            check_sql()


def test_dirty_changes_merge_across_connections_and_rollback_with_source(db):
    db.con.execute("INSERT INTO tasks(id,name) VALUES (1,'업무')")
    assert db.con.execute("SELECT wanted_revision FROM kg_jobs WHERE partition_key='task:1'").fetchone()[0] == 1
    with sqlite3.connect(db.path) as other:
        other.execute("UPDATE tasks SET name='업무 수정' WHERE id=1")
        other.execute("UPDATE tasks SET name='다시 수정' WHERE id=1")
    assert db.con.execute("SELECT wanted_revision FROM kg_jobs WHERE partition_key='task:1'").fetchone()[0] == 3
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.con.execute("UPDATE tasks SET name='취소되는 수정' WHERE id=1")
            raise RuntimeError("rollback")
    assert db.con.execute("SELECT wanted_revision FROM kg_jobs WHERE partition_key='task:1'").fetchone()[0] == 3
    assert db.con.execute("SELECT name FROM tasks").fetchone()[0] == "다시 수정"
    db.con.execute("DELETE FROM tasks WHERE id=1")
    assert db.con.execute("SELECT COUNT(*) FROM kg_jobs WHERE partition_key='task:1'").fetchone()[0] == 1
    assert db.con.execute("SELECT wanted_revision FROM kg_jobs WHERE partition_key='task:1'").fetchone()[0] == 4
