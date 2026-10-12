import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_knowledge_projection import db, source_snapshot
from app.core.knowledge import lifecycle, projection
from app.db import Database, migrations
from app.db.knowledge import KnowledgeStore


def drain(db):
    result = lifecycle.run_jobs(db, seconds=30, max_jobs=1000)
    assert not result.failed, result.diagnostics
    assert not lifecycle.pending(db)
    return result


def job(db, key='task:1'):
    return db.con.execute('SELECT * FROM kg_jobs WHERE partition_key=?', (key,)).fetchone()


def claim(db):
    return db.con.execute("SELECT * FROM kg_claims WHERE predicate='due_date'").fetchone()


@pytest.mark.parametrize('change', ["UPDATE documents SET hash='hash-b' WHERE id=1",
    "UPDATE documents SET missing_since='2026-10-11' WHERE id=1",
    "UPDATE document_sections SET text='바뀐 본문' WHERE doc_id=1",
    'DELETE FROM documents WHERE id=1'])
def test_document_change_immediately_excludes_dependent_claims_and_preserves_history(db, change):
    drain(db)
    previous = claim(db)
    store = KnowledgeStore(db)
    decision = store.record_decision('claim', previous['claim_key'], 'confirm', '사람')
    drain(db)
    evidence = [tuple(r) for r in db.con.execute('SELECT uuid,anchor_key,doc_uuid,doc_hash,exact_quote FROM kg_evidence')]
    revision = int(db.get_meta('kg_revision'))
    other = Database(db.path)
    try:
        other.con.execute(change)
    finally:
        other.close()
    assert job(db)['state'] == 'dirty'
    assert claim(db)['lifecycle'] == 'stale'
    assert int(db.get_meta('kg_revision')) > revision
    assert evidence == [tuple(r) for r in db.con.execute('SELECT uuid,anchor_key,doc_uuid,doc_hash,exact_quote FROM kg_evidence')]
    drain(db)
    assert claim(db)['lifecycle'] == 'stale'
    assert store.decisions_for(previous['claim_key'])[0]['uuid'] == decision


def test_missing_return_requires_worker_revalidation_and_scan_touch_is_noop(db):
    drain(db)
    before = job(db)['wanted_revision']
    db.con.execute("UPDATE documents SET last_seen='tomorrow' WHERE id=1")
    assert job(db)['wanted_revision'] == before
    db.con.execute("UPDATE documents SET missing_since='today' WHERE id=1")
    db.mark_seen(1)
    assert claim(db)['lifecycle'] == 'stale'
    drain(db)
    assert claim(db)['lifecycle'] == 'active'


def test_reindex_rollback_reconnects_only_valid_chunks(db):
    drain(db)
    quote = db.con.execute("SELECT uuid FROM kg_evidence WHERE kind='document_quote' AND locator='본문' LIMIT 1").fetchone()[0]
    db.set_meta('index_revision', '7')
    drain(db)
    db.con.execute('DELETE FROM chunks')
    db.con.execute("INSERT INTO chunks(id,doc_id,ordinal,locator,text) SELECT 99,1,0,locator,text FROM document_sections LIMIT 1")
    db.set_meta('index_revision', '2')
    assert claim(db)['lifecycle'] == 'stale'
    drain(db)
    assert db.con.execute('SELECT chunk_id FROM kg_evidence WHERE uuid=?', (quote,)).fetchone()[0] == 99
    db.con.execute("UPDATE chunks SET text='다른 인용' WHERE id=99")
    drain(db)
    assert db.con.execute('SELECT chunk_id FROM kg_evidence WHERE uuid=?', (quote,)).fetchone()[0] is None


def test_lease_is_exclusive_and_expired_job_can_be_reclaimed(db):
    first = lifecycle.acquire(db)
    other = Database(db.path)
    try:
        second = lifecycle.acquire(other)
        assert first['partition_key'] != second['partition_key']
        assert lifecycle.recover(other) == 0
        db.con.execute('UPDATE kg_jobs SET lease_until=0 WHERE lease=?', (first['lease'],))
        assert lifecycle.recover(other) == 1
    finally:
        other.close()
    drain(db)


def test_stale_lease_cannot_publish_and_preserves_verified_generation(db):
    drain(db)
    store = KnowledgeStore(db)
    store.schedule('task:1', 'retry')
    leased = lifecycle.acquire(db)
    assert leased['partition_key'] == 'task:1'
    db._kg_lease = (leased['lease'], leased['leased_revision'])
    try:
        with db.transaction():
            generation = store.begin_generation('task:1', 'staged')
            store.schedule('task:1', 'newer input')
            with pytest.raises(lifecycle.ObsoleteJob):
                store.finish_generation(generation, [])
            db.con.execute('UPDATE kg_generations SET status=\'obsolete\' WHERE id=?', (generation,))
    finally:
        db._kg_lease = None
    assert db.con.execute("SELECT COUNT(*) FROM kg_generations WHERE partition_key='task:1' AND active=1").fetchone()[0] == 1
    db.con.execute("UPDATE kg_jobs SET lease_until=0 WHERE partition_key='task:1'")
    drain(db)


def test_forced_process_exit_recovers_lease_without_partial_generation(db, tmp_path):
    # lease를 커밋한 실제 프로세스를 세대 생성의 미커밋 트랜잭션에서 종료한다.
    drain(db)
    KnowledgeStore(db).schedule('task:1', 'crash')
    script = tmp_path / 'crash.py'
    script.write_text('''import os, sys
from app.db import Database
from app.core.knowledge.lifecycle import acquire
from app.db.knowledge import KnowledgeStore
db=Database(sys.argv[1])
job=acquire(db)
db._kg_lease=(job['lease'],job['leased_revision'])
db.con.execute('BEGIN')
KnowledgeStore(db).begin_generation('task:1','uncommitted')
os._exit(17)
''', encoding='utf-8')
    result = subprocess.run([sys.executable, str(script), str(db.path)], cwd=Path(__file__).parents[1],
                            env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1])}, timeout=15)
    assert result.returncode == 17
    assert job(db)['state'] == 'running'
    assert not db.con.execute("SELECT 1 FROM kg_generations WHERE input_fingerprint='uncommitted'").fetchone()
    db.con.execute("UPDATE kg_jobs SET lease_until=0 WHERE state='running'")
    drain(db)
    assert db.con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_failure_limit_does_not_block_other_partitions_and_resume(db, monkeypatch):
    original = projection.project_document
    monkeypatch.setattr(projection, 'project_document', lambda *args: (_ for _ in ()).throw(ValueError('injected')))
    result = lifecycle.run_jobs(db, seconds=30)
    assert result.failed == 3
    assert job(db, 'document:1')['state'] == 'failed'
    assert job(db)['state'] == 'done'
    assert 'injected' in job(db, 'document:1')['error']
    monkeypatch.setattr(projection, 'project_document', original)
    assert lifecycle.resume(db, 'document:1') == 1
    drain(db)


def test_explicit_cancel_and_cooperative_stop_resume(db, monkeypatch):
    lifecycle.cancel(db, 'task:1')
    drain(db)
    assert job(db)['state'] == 'cancelled'
    assert lifecycle.resume(db, 'task:1') == 1
    original = projection._task_input
    stopped = False
    def stop_after_snapshot(*args, **kwargs):
        nonlocal stopped
        data = original(*args, **kwargs)
        stopped = True
        return data
    monkeypatch.setattr(projection, '_task_input', stop_after_snapshot)
    report = lifecycle.run_jobs(db, should_stop=lambda: stopped)
    assert report.cancelled and job(db)['state'] == 'dirty'
    assert job(db)['lease'] is None
    monkeypatch.setattr(projection, '_task_input', original)
    drain(db)


def test_snapshot_changed_by_second_connection_is_not_published(db, monkeypatch):
    drain(db)
    KnowledgeStore(db).schedule('task:1', 'snapshot race')
    original = projection._task_input
    changed = False
    def read_then_change(*args, **kwargs):
        nonlocal changed
        data = original(*args, **kwargs)
        if not changed:
            changed = True
            with Database(db.path) as other:
                other.con.execute("UPDATE tasks SET name='새 이름' WHERE id=1")
        return data
    monkeypatch.setattr(projection, '_task_input', read_then_change)
    result = lifecycle.run_jobs(db, max_jobs=1)
    assert result.obsolete == 1 and job(db)['state'] == 'dirty'
    assert not db.con.execute("SELECT 1 FROM kg_generations WHERE active=1 AND partition_key='task:1'").fetchone()
    drain(db)


def test_previous_step_order_does_not_create_cycle_in_new_generation(db):
    db.sync_work_steps(1, [{'slot':'a','ordinal':1,'label':'A'}, {'slot':'b','ordinal':2,'label':'B'}])
    a,b = [r['id'] for r in db.work_steps(1)]
    db.con.execute('UPDATE work_steps SET after_step_id=? WHERE id=?', (a,b))
    drain(db)
    with db.transaction():
        db.con.execute('UPDATE work_steps SET after_step_id=NULL WHERE id=?', (b,))
        db.con.execute('UPDATE work_steps SET after_step_id=? WHERE id=?', (b,a))
    drain(db)
    assert db.con.execute("SELECT COUNT(*) FROM kg_claims WHERE predicate='PRECEDES' AND lifecycle='active'").fetchone()[0] == 1
    assert db.con.execute("SELECT COUNT(*) FROM kg_claims WHERE predicate='PRECEDES' AND lifecycle='stale'").fetchone()[0] == 1


def test_identity_merge_separate_and_nested_undo_preserve_case_endpoints(db):
    db.con.execute("INSERT INTO tasks(id,name) VALUES (2,'같은 이름')")
    db.con.execute("INSERT INTO work_cases(task_id,period_year,label) VALUES (1,2026,'수행')")
    drain(db)
    store = KnowledgeStore(db)
    a,b = [r[0] for r in db.con.execute("SELECT entity_id FROM kg_bindings WHERE source_table='tasks' ORDER BY source_key")]
    before = source_snapshot(db)
    store.record_decision('identity', a, 'merge', '사람', after={'entity_id': b})
    assert store.canonical_entity(a) == b
    with pytest.raises(ValueError):
        store.record_decision('identity', b, 'merge', '사람', after={'entity_id': a})
    separate = store.record_decision('identity', a, 'separate', '사람')
    assert store.canonical_entity(a) == a
    undo = store.record_decision('identity', a, 'undo', '사람', supersedes_decision=separate)
    assert store.canonical_entity(a) == b
    store.record_decision('identity', a, 'undo', '사람', supersedes_decision=undo)
    assert store.canonical_entity(a) == a
    drain(db)
    assert source_snapshot(db) == before


def test_deleted_integer_id_does_not_reuse_previous_identity(db):
    drain(db)
    old_task = db.con.execute("SELECT entity_id FROM kg_bindings WHERE source_table='tasks'").fetchone()[0]
    old_doc = db.get_meta('kg_document_uuid:1')
    db.con.execute('DELETE FROM tasks WHERE id=1')
    db.con.execute('DELETE FROM documents WHERE id=1')
    drain(db)
    db.con.execute("INSERT INTO tasks(id,name) VALUES (1,'새 업무')")
    db.upsert_document(1, {'path':'/new.txt','filename':'new.txt','ext':'.txt','hash':'hash-a','parse_status':'ok'})
    drain(db)
    assert db.get_meta('kg_document_uuid:1') != old_doc
    assert db.con.execute("SELECT entity_id FROM kg_bindings WHERE source_table='tasks' AND source_key='1'").fetchone()[0] != old_task
    assert db.con.execute('SELECT tombstone FROM kg_bindings WHERE entity_id=?', (old_task,)).fetchone()[0] == 1


def test_v12_migration_adds_lease_columns_and_preserves_people_and_history(db):
    drain(db)
    previous = claim(db)
    KnowledgeStore(db).record_decision('claim', previous['claim_key'], 'reject', '사람')
    # v12 큐에는 새 열을 참조하는 P3 트리거가 없다.
    for row in list(db.con.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'kg_dirty_%'")):
        db.con.execute(f'DROP TRIGGER "{row[0]}"')
    for column in ('lease_until', 'leased_revision', 'error'):
        db.con.execute(f'ALTER TABLE kg_jobs DROP COLUMN {column}')
    db.set_meta('schema_version', '12')
    db.init()
    assert db.get_meta('schema_version') == migrations.SCHEMA_VERSION
    assert db.con.execute('SELECT COUNT(*) FROM kg_decisions').fetchone()[0] == 1
    assert claim(db)['uuid'] == previous['uuid']
    assert migrations.backup_path(db.path, '12').is_file()
    drain(db)
    assert claim(db)['lifecycle'] == 'retracted'


def test_failure_keeps_verified_generation_when_source_is_unchanged(db, monkeypatch):
    drain(db)
    before = db.con.execute("SELECT id FROM kg_generations WHERE partition_key='task:1' AND active=1").fetchone()[0]
    KnowledgeStore(db).schedule('task:1', 'retry without source change')
    monkeypatch.setattr(projection, 'project_task', lambda *args: (_ for _ in ()).throw(ValueError('invalid')))
    result = lifecycle.run_jobs(db, seconds=30)
    assert result.failed == 3 and job(db)['state'] == 'failed'
    assert db.con.execute('SELECT active FROM kg_generations WHERE id=?', (before,)).fetchone()[0] == 1
    assert claim(db)['lifecycle'] == 'active'


def test_another_independent_partition_support_survives_last_source_deletion(db):
    drain(db)
    store = KnowledgeStore(db)
    previous = claim(db)
    with db.transaction():
        generation = store.begin_generation('task:99', 'explicit human record')
        evidence = store.put_evidence('human_statement', source_record_snapshot={'actor':'담당자','statement':'같은 기한'})
        store.support(previous['uuid'], evidence, generation)
        store.finish_generation(generation, [])
    db.con.execute('DELETE FROM documents WHERE id=1')
    assert claim(db)['lifecycle'] == 'active'
    assert store.independent_quote_sources(previous['uuid']) == 0
    # 두 번째 파티션도 삭제되면 마지막 유효 근거가 없어진다.
    projection.project_task(db, 99)
    assert claim(db)['lifecycle'] == 'stale'


def test_reject_during_snapshot_is_applied_before_republication(db, monkeypatch):
    drain(db)
    previous = claim(db)
    store = KnowledgeStore(db)
    store.schedule('task:1', 'race')
    original = projection._task_input
    changed = False
    def reject(*args, **kwargs):
        nonlocal changed
        data = original(*args, **kwargs)
        if not changed:
            changed = True
            with Database(db.path) as other:
                KnowledgeStore(other).record_decision('claim', previous['claim_key'], 'reject', '후임자')
        return data
    monkeypatch.setattr(projection, '_task_input', reject)
    assert lifecycle.run_jobs(db, max_jobs=1).obsolete == 1
    drain(db)
    assert claim(db)['lifecycle'] == 'retracted'


def test_same_hash_move_keeps_identity_and_derived_brief_cannot_support_original_claim(db):
    drain(db)
    old = db.get_meta('kg_document_uuid:1')
    db.con.execute("UPDATE documents SET path='/moved.txt',filename='moved.txt' WHERE id=1")
    drain(db)
    assert db.get_meta('kg_document_uuid:1') == old
    from app.core import notebook
    book = notebook.create(db, '정리본')
    notebook.add_sources(db, book, [1], origin='task')
    drain(db)
    assert claim(db)['lifecycle'] == 'stale'
    assert KnowledgeStore(db).independent_quote_sources(claim(db)['uuid']) == 0


def test_extracted_step_quote_is_revalidated_after_document_disappears(db):
    db.sync_work_steps(1, [{'slot':'step','ordinal':1,'label':'제출','evidence':[(1,'본문','서식: 신청서')]}])
    drain(db)
    assert db.con.execute("SELECT 1 FROM kg_current_claims WHERE predicate='HAS_STEP'").fetchone()
    db.con.execute("UPDATE documents SET missing_since='today' WHERE id=1")
    drain(db)
    assert not db.con.execute("SELECT 1 FROM kg_current_claims WHERE predicate='HAS_STEP'").fetchone()


def test_task_merge_and_split_only_rebuild_views_of_original_cases_and_runs(db):
    db.con.execute("INSERT INTO tasks(id,name) VALUES (2,'대상 업무')")
    db.con.execute("INSERT INTO work_cases(id,task_id,period_year) VALUES (1,1,2026)")
    db.con.execute("INSERT INTO action_runs(id,case_id,task_id,plan_key,plan_version,idem_key,state) VALUES (1,1,1,'p','1','i','completed')")
    drain(db)
    original = {t:[tuple(r) for r in db.con.execute(f'SELECT * FROM {t}')] for t in ('work_cases','action_runs')}
    a,b = [r[0] for r in db.con.execute("SELECT entity_id FROM kg_bindings WHERE source_table='tasks' ORDER BY source_key")]
    assert db.merge_tasks(1,2)
    assert job(db)['state'] == job(db,'task:2')['state'] == 'dirty'
    drain(db)
    assert KnowledgeStore(db).canonical_entity(a) == b
    assert original == {t:[tuple(r) for r in db.con.execute(f'SELECT * FROM {t}')] for t in original}
    # 새 문서를 보태 실제 split을 할 수 있는 정본을 만든다.
    second = db.upsert_document(1, {'path':'/second.txt','filename':'second.txt','ext':'.txt','hash':'b'})
    db.assign_document(2,second)
    new = db.split_task(2,[second],'나눈 업무')
    assert new
    drain(db)
    assert original == {t:[tuple(r) for r in db.con.execute(f'SELECT * FROM {t}')] for t in original}


def test_failed_v13_migration_restores_old_version_and_lease_columns(db, monkeypatch):
    for row in list(db.con.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'kg_dirty_%'")):
        db.con.execute(f'DROP TRIGGER "{row[0]}"')
    for column in ('lease_until', 'leased_revision', 'error'):
        db.con.execute(f'ALTER TABLE kg_jobs DROP COLUMN {column}')
    db.set_meta('schema_version','12')
    original = migrations.STEPS['13']
    def fail(con):
        original(con)
        raise RuntimeError('injected')
    monkeypatch.setitem(migrations.STEPS,'13',fail)
    with pytest.raises(migrations.MigrationError):
        db.init()
    assert db.get_meta('schema_version') == '12'
    assert 'lease_until' not in {r[1] for r in db.con.execute('PRAGMA table_info(kg_jobs)')}
    assert db.task(1)['name'] == '신청 업무'
    monkeypatch.setitem(migrations.STEPS,'13',original)
    db.init()
    drain(db)


def test_deleted_item_id_does_not_merge_its_new_value_entity_with_history(db):
    drain(db)
    item = db.con.execute("SELECT * FROM task_items WHERE kind='form'").fetchone()
    before = db.con.execute("SELECT entity_id FROM kg_bindings WHERE source_table='task_item_values' AND source_key LIKE ?",
                            (f"{item['id']}:%",)).fetchone()[0]
    db.con.execute('DELETE FROM task_items WHERE id=?', (item['id'],))
    db.con.execute("INSERT INTO task_items(id,task_id,kind,slot,content,origin) VALUES (?,1,'form','new','신청서','user')", (item['id'],))
    drain(db)
    after = db.con.execute("SELECT entity_id FROM kg_bindings WHERE source_table='task_item_values' AND tombstone=0 AND source_key LIKE ?",
                           (f"{item['id']}:%",)).fetchone()[0]
    assert before != after


def test_cycle_in_reused_claims_is_rejected_before_publication(db):
    drain(db)
    store = KnowledgeStore(db)
    a = store.bind('test_steps','a','Step','A')
    b = store.bind('test_steps','b','Step','B')
    # 서로 다른 과거 세대에서는 각각 합법인 주장. 같은 후보 세대에서 재사용하면 순환한다.
    g1 = store.begin_generation('task:10','a')
    first = store.put_claim(a,'PRECEDES',object_id=b,generation_id=g1)
    g2 = store.begin_generation('task:11','b')
    second = store.put_claim(b,'PRECEDES',object_id=a,generation_id=g2)
    with pytest.raises(ValueError, match='cycle'):
        with db.transaction():
            candidate = store.begin_generation('task:12','cycle')
            evidence = store.put_evidence('human_statement',source_record_snapshot={'statement':'양쪽 순서'})
            store.support(first,evidence,candidate)
            store.support(second,evidence,candidate)
            store.finish_generation(candidate,[])
    assert not db.con.execute("SELECT 1 FROM kg_generations WHERE partition_key='task:12' AND active=1").fetchone()


def test_decision_only_schedules_partitions_containing_its_target(db):
    db.con.execute("INSERT INTO tasks(id,name) VALUES (2,'관련 없는 업무')")
    drain(db)
    unrelated = job(db,'task:2')['wanted_revision']
    document = job(db,'document:1')['wanted_revision']
    previous = claim(db)
    KnowledgeStore(db).record_decision('claim',previous['claim_key'],'reject','사람')
    assert job(db)['state'] == 'dirty'
    assert job(db,'task:2')['wanted_revision'] == unrelated
    assert job(db,'document:1')['wanted_revision'] == document


def test_index_revision_first_insert_and_delete_invalidate_current_generation(db):
    drain(db)
    db.set_meta('index_revision','7')
    assert claim(db)['lifecycle'] == 'stale'
    drain(db)
    db.con.execute("DELETE FROM meta WHERE key='index_revision'")
    assert claim(db)['lifecycle'] == 'stale'
    drain(db)


def test_a_change_only_stales_claims_of_the_changed_partition(db):
    """무효화 트리거가 프로젝트 전체 주장을 훑지 않고, 바뀐 파티션의 주장만 stale로 만든다."""
    source = db.con.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
    db.upsert_document(source, {"path": str(db.path.parent / "b.txt"), "filename": "b.txt", "ext": ".txt",
                                "hash": "hash-b", "parse_status": "ok", "parser": "text", "parser_version": "1"})
    text = "서식: 별지서식"
    db.replace_sections(2, [("paragraph", 0, "본문", text)])
    db.replace_document_chunks(2, [(0, "본문", text)])
    db.con.execute("INSERT INTO tasks(id,name) VALUES (2,'다른 업무')")
    db.con.execute("INSERT INTO task_docs(task_id,doc_id,origin) VALUES (2,2,'user')")
    db.sync_task_items(2, [{"kind": "form", "slot": "form", "content": "별지서식", "basis_state": "stated",
                            "applicability": "past", "evidence": [(2, "본문", "서식: 별지서식")]}])
    drain(db)
    forms = db.con.execute("SELECT uuid FROM kg_claims WHERE predicate='REQUIRES_FORM' AND lifecycle='active'").fetchall()
    assert len(forms) == 2
    other = Database(db.path)
    try:
        other.sync_task_items(2, [])                      # 업무 2의 항목만 지운다
    finally:
        other.close()
    states = {r["predicate"] + ":" + r["lifecycle"] for r in db.con.execute("SELECT predicate,lifecycle FROM kg_claims")}
    assert "due_date:active" in states and "amount:active" in states      # 업무 1의 주장은 그대로 현행
    assert db.con.execute("SELECT COUNT(*) FROM kg_claims WHERE predicate='REQUIRES_FORM' AND lifecycle='stale'").fetchone()[0] == 1
    assert db.con.execute("SELECT COUNT(*) FROM kg_claims WHERE predicate='REQUIRES_FORM' AND lifecycle='active'").fetchone()[0] == 1
