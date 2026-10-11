import json

import pytest

from app.core import notebook, notebook_ingest
from app.core.knowledge import projection
from app.core.knowledge.evidence import match_quote
from app.db import Database
from app.db.knowledge import KnowledgeStore


@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path / "project.db")
    db.init()
    source = db.add_source(str(tmp_path / "source"))
    db.upsert_document(source, {"path": str(tmp_path / "source" / "a.txt"), "filename": "a.txt", "ext": ".txt", "hash": "hash-a", "parse_status": "ok", "parser": "text", "parser_version": "1"})
    text = "서식: 신청서\n제출처: 복지과\n제출기한: 2026-11-01\n금액: 1,000 원"
    db.replace_sections(1, [("paragraph", 0, "본문", text)])
    db.replace_document_chunks(1, [(0, "본문", text)])
    db.con.execute("INSERT INTO tasks(id,name) VALUES (1,'신청 업무')")
    db.con.execute("INSERT INTO task_docs(task_id,doc_id,origin) VALUES (1,1,'user')")
    db.sync_task_items(1, [{"kind": kind, "slot": kind, "content": content, "basis_state": "stated", "applicability": "past", "evidence": [(1,"본문",quote)]}
        for kind,content,quote in [("form","신청서","서식: 신청서"), ("recipient","복지과","제출처: 복지과"),
                                  ("deadline","2026-11-01","제출기한: 2026-11-01"), ("amount","1,000 원","금액: 1,000 원")]])
    yield db
    db.close()


def source_snapshot(db):
    return {table: [tuple(r) for r in db.con.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("tasks", "documents", "task_docs", "task_items", "work_steps", "work_cases", "step_runs", "action_runs", "action_steps", "action_approvals", "artifacts", "evidence")}


def test_projection_is_idempotent_does_not_mutate_sources_or_invent_cases(db):
    before = source_snapshot(db)
    first = projection.project_task(db, 1)
    counts = {table: db.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
              for table in ("kg_entities", "kg_claims", "kg_evidence", "kg_claim_events", "kg_generations")}
    second = projection.project_task(db, 1)
    assert second.reused and second.generation == first.generation and not first.diagnostics
    assert source_snapshot(db) == before
    assert counts == {table: db.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in counts}
    assert db.con.execute("SELECT COUNT(*) FROM kg_entities WHERE type IN ('Case','HistoricalCase')").fetchone()[0] == 0
    claims = db.con.execute("SELECT predicate,lifecycle,applicability FROM kg_claims ORDER BY predicate").fetchall()
    assert {r[0] for r in claims} == {"HAS_DOCUMENT", "REQUIRES_FORM", "SUBMITTED_TO", "due_date", "amount"}
    assert all(r[1] == "active" for r in claims)
    assert all(r[2] == "past" for r in claims if r[0] != "HAS_DOCUMENT")
    assert db.con.execute("SELECT COUNT(*) FROM kg_claim_evidence WHERE validation_result='semantic_supported'").fetchone()[0] == 0


def test_reject_and_undo_survive_projection(db):
    projection.project_task(db, 1)
    claim = db.con.execute("SELECT * FROM kg_claims WHERE predicate='due_date'").fetchone()
    store = KnowledgeStore(db)
    rejected = store.record_decision("claim", claim["claim_key"], "reject", "후임자")
    projection.project_task(db, 1)
    assert db.con.execute("SELECT lifecycle FROM kg_claims WHERE uuid=?", (claim["uuid"],)).fetchone()[0] == "retracted"
    store.record_decision("claim", claim["claim_key"], "undo", "후임자", supersedes_decision=rejected)
    projection.project_task(db, 1)
    assert db.con.execute("SELECT lifecycle FROM kg_claims WHERE uuid=?", (claim["uuid"],)).fetchone()[0] == "active"
    assert db.con.execute("SELECT COUNT(*) FROM kg_decisions").fetchone()[0] == 2


def test_case_and_step_connections_only_follow_explicit_source_records(db):
    db.con.execute("INSERT INTO work_cases(id,task_id,period_year,label,due_date,due_state) VALUES (1,1,2026,'올해','2026-12-01','user')")
    db.con.execute("INSERT INTO work_cases(id,task_id,period_year,label) VALUES (2,1,2025,'지난해')")
    db.sync_work_steps(1, [{"slot":"a","ordinal":1,"label":"준비"}, {"slot":"b","ordinal":2,"label":"제출"}])
    steps = db.work_steps(1)
    db.con.execute("UPDATE work_steps SET after_step_id=? WHERE id=?", (steps[0]["id"],steps[1]["id"]))
    db.set_step_run(steps[0]["id"], "done")
    db.con.execute("INSERT INTO checklist_items(case_id,work_step_id,slot,title,source_type) VALUES (1,?,'a','준비','stated')", (steps[0]["id"],))
    before = source_snapshot(db)
    projection.project_task(db, 1)
    assert source_snapshot(db) == before
    assert db.con.execute("SELECT COUNT(*) FROM kg_claims c JOIN kg_entities s ON s.uuid=c.subject_id WHERE c.predicate='HAS_STEP' AND s.type='Case'").fetchone()[0] == 1
    assert db.con.execute("SELECT COUNT(*) FROM kg_claims WHERE predicate='PRECEDES'").fetchone()[0] == 1
    assert db.con.execute("SELECT execution_state FROM kg_claims WHERE predicate='HAS_STEP' AND execution_state='done'").fetchone()[0] == "done"


def test_normalized_quotes_restore_exact_offsets_and_keep_table_headers(db):
    db.replace_sections(1, [("paragraph",0,"뒤쪽","앞말\n서식:    신청서\n뒷말"),
                            ("table",1,"표 1","첫 행: 서식 | 제출처\n신청서 | 복지과", {"headers":["서식","제출처"]})])
    matched = match_quote(db,1,"hash-a","뒤쪽","서식: 신청서")
    assert matched["exact_quote"] == "서식:    신청서"
    assert matched["offsets"]["start"] == 3
    table = match_quote(db,1,"hash-a","표 1","신청서 | 복지과")
    assert "첫 행:" in table["exact_quote"] and table["offsets"]["structure"]["headers"] == ["서식","제출처"]
    assert match_quote(db,1,"wrong-hash","뒤쪽","서식: 신청서") is None
    assert match_quote(db,1,"hash-a","없는 위치","서식: 신청서") is None


def test_invalid_quote_does_not_become_active_and_index_changes_keep_anchor(db):
    db.con.execute("UPDATE evidence SET quote='원문에 없는 말' WHERE owner_kind='item'")
    result = projection.project_task(db,1)
    assert result.diagnostics
    assert db.con.execute("SELECT COUNT(*) FROM kg_claim_evidence WHERE validation_result='invalid' AND active=1").fetchone()[0] == 0
    assert db.con.execute("SELECT COUNT(*) FROM kg_claims WHERE predicate!='HAS_DOCUMENT' AND lifecycle='active'").fetchone()[0] == 0


def test_human_confirm_cannot_turn_invalid_quote_into_current_source(db):
    projection.project_task(db,1)
    claim = db.con.execute("SELECT * FROM kg_claims WHERE predicate='due_date'").fetchone()
    KnowledgeStore(db).record_decision("claim",claim["claim_key"],"confirm","확인자")
    db.con.execute("UPDATE evidence SET quote='없는 기한' WHERE owner_id IN (SELECT id FROM task_items WHERE kind='deadline')")
    projection.project_task(db,1)
    assert db.con.execute("SELECT lifecycle FROM kg_claims WHERE uuid=?", (claim["uuid"],)).fetchone()[0] == "stale"


def test_human_alias_survives_automatic_projection(db):
    projection.project_task(db,1)
    store = KnowledgeStore(db)
    entity = db.con.execute("SELECT uuid FROM kg_entities WHERE type='Organization'").fetchone()[0]
    decision = store.record_decision("alias",entity,"alias","확인자",after={"label":"민원 창구"})
    store.add_alias(entity,"민원 창구",origin="human",source_key=decision,decision_id=decision)
    db.con.execute("UPDATE tasks SET description='재투영' WHERE id=1")
    projection.project_task(db,1)
    assert db.con.execute("SELECT decision_id FROM kg_aliases WHERE label='민원 창구'").fetchone()[0] == decision


def test_state_event_points_to_generation_that_observed_the_change(db):
    projection.project_task(db,1)
    db.con.execute("UPDATE task_items SET basis_state='inferred' WHERE kind='form'")
    latest = projection.project_task(db,1)
    event = db.con.execute("SELECT e.generation_id FROM kg_claim_events e JOIN kg_claims c ON c.uuid=e.claim_id "
                           "WHERE c.predicate='REQUIRES_FORM' AND e.event_kind='state' ORDER BY e.rowid DESC LIMIT 1").fetchone()
    assert event[0] == latest.generation


def test_document_version_separates_hash_from_parse_generation(db):
    first = projection.project_document(db,1)
    uid = db.con.execute("SELECT entity_id FROM kg_bindings WHERE source_table='document_versions'").fetchone()[0]
    db.update_document(1, parser_version="2")
    second = projection.project_document(db,1)
    assert first.generation != second.generation
    assert db.con.execute("SELECT entity_id FROM kg_bindings WHERE source_table='document_versions'").fetchone()[0] == uid
    db.update_document(1, hash="new-hash")
    projection.project_document(db,1)
    assert db.con.execute("SELECT COUNT(*) FROM kg_bindings WHERE source_table='document_versions'").fetchone()[0] == 2


def test_reindex_preserves_quote_anchor_and_refreshes_chunk_pointer(db):
    projection.project_task(db,1)
    before = {r["uuid"]: r["chunk_id"] for r in db.con.execute("SELECT uuid,chunk_id FROM kg_evidence WHERE kind='document_quote'")}
    text = db.con.execute("SELECT text FROM chunks WHERE doc_id=1").fetchone()[0]
    db.con.execute("INSERT INTO chunks(id,doc_id,ordinal,locator,text) VALUES (100,1,0,'본문',?)", (text,))
    db.con.execute("DELETE FROM chunks WHERE id!=100 AND doc_id=1")
    result = projection.project_task(db,1)
    assert not result.reused
    after = {r["uuid"]: r["chunk_id"] for r in db.con.execute("SELECT uuid,chunk_id FROM kg_evidence WHERE kind='document_quote'")}
    assert before.keys() == after.keys() and set(after.values()) == {100}


def test_same_hash_copies_do_not_count_as_independent_quotes(db):
    source = db.sources()[0]["id"]
    doc = db.upsert_document(source, {"path": "/copy.txt", "filename": "copy.txt", "ext": ".txt", "hash": "hash-a", "parse_status": "ok"})
    db.replace_sections(doc, [("paragraph",0,"본문","서식: 신청서")])
    item = db.con.execute("SELECT id FROM task_items WHERE kind='form'").fetchone()[0]
    db.add_evidence("item",item,doc,"hash-a","본문","서식: 신청서","copy.txt","copy.txt")
    projection.project_task(db,1)
    claim = db.con.execute("SELECT uuid FROM kg_claims WHERE predicate='REQUIRES_FORM'").fetchone()[0]
    assert KnowledgeStore(db).independent_quote_sources(claim) == 1


def test_artifact_run_and_supersedes_are_projected_without_changing_approval(db):
    db.con.execute("INSERT INTO work_cases(id,task_id,period_year) VALUES (1,1,2026)")
    db.con.execute("INSERT INTO action_runs(id,case_id,task_id,plan_key,plan_version,idem_key,state) VALUES (1,1,1,'plan','1','idem','waiting_for_approval')")
    db.con.execute("INSERT INTO action_steps(id,run_id,ordinal,tool,tool_version,risk,state) VALUES (1,1,0,'tool','1','draft','waiting_for_approval')")
    db.con.execute("INSERT INTO action_approvals(run_id,step_id,action,scope_hash,decision) VALUES (1,1,'draft','scope','rejected')")
    db.con.execute("INSERT INTO artifacts(id,run_id,step_id,case_id,kind,title,format,path,sha256) VALUES (1,1,1,1,'draft','이전 초안','md','old.md','a')")
    db.con.execute("INSERT INTO artifacts(id,run_id,step_id,case_id,kind,title,format,path,sha256,supersedes_id) VALUES (2,1,1,1,'draft','새 초안','md','new.md','b',1)")
    before = source_snapshot(db)
    result = projection.project_task(db,1)
    assert not result.diagnostics and source_snapshot(db) == before
    assert db.con.execute("SELECT COUNT(*) FROM kg_claims WHERE predicate='SUPERSEDES'").fetchone()[0] == 1
    assert db.con.execute("SELECT COUNT(*) FROM kg_claims WHERE predicate='PRODUCES'").fetchone()[0] == 4


def test_merged_task_keeps_original_case_endpoint(db):
    db.con.execute("INSERT INTO tasks(id,name) VALUES (2,'합쳐진 업무')")
    db.con.execute("INSERT INTO work_cases(id,task_id,period_year) VALUES (1,1,2026)")
    db.con.execute("UPDATE tasks SET merged_into=2 WHERE id=1")
    projection.project_task(db,1)
    projection.project_task(db,2)
    source = db.con.execute("SELECT b.source_key FROM kg_claims c JOIN kg_bindings b ON b.entity_id=c.subject_id "
                            "WHERE c.predicate='HAS_CASE' AND b.source_table='tasks'").fetchone()[0]
    assert source == "1" and db.case(1)["task_id"] == 1


def test_notebook_ingest_only_schedules_document_and_never_assigns_task(db,tmp_path):
    book = notebook.create(db,"선택 자료")
    path = tmp_path / "upload.txt"
    path.write_text("서식: 별도 참고서식", encoding="utf-8")
    outcome = notebook_ingest.ingest_file(db,book,path)
    assert outcome.usable
    assert db.con.execute("SELECT COUNT(*) FROM task_docs WHERE doc_id=?", (outcome.doc_id,)).fetchone()[0] == 0
    assert db.con.execute("SELECT state FROM kg_jobs WHERE partition_key=?", (f"document:{outcome.doc_id}",)).fetchone()[0] == "dirty"
    projection.project_document(db,outcome.doc_id)
    assert db.con.execute("SELECT COUNT(*) FROM kg_claims WHERE predicate='HAS_CASE'").fetchone()[0] == 0
