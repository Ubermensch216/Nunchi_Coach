"""Graph candidates must carry scoped, current original quotes."""
import json
import sqlite3

import pytest

from app.core.knowledge import projection
from app.db import Database
from app.db.knowledge import KnowledgeStore
from app.search import graph, rag
from app.search.query import QueryScope


@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path / 'project.db')
    db.init()
    source = db.add_source(str(tmp_path))
    for i, year in [(1, 2025), (2, 2026)]:
        text = f'업무명: 신청 업무\n서식: 신청서\n제출처: 복지{i}과\n기준연도: {year-1}'
        path = tmp_path / f'{i}.txt'
        path.write_text(text, encoding='utf-8')
        import hashlib
        db.upsert_document(source, {'path': str(path), 'filename': path.name, 'ext': '.txt',
                                    'hash': hashlib.sha256(path.read_bytes()).hexdigest(),
                                    'parse_status': 'ok', 'parser': 'text', 'parser_version': '1', 'eff_year': year})
        db.replace_sections(i, [('paragraph', 0, '본문', text)])
        db.replace_document_chunks(i, [(0, '본문', text)])
        db.con.execute('INSERT INTO tasks(id,name) VALUES (?,?)', (i, f'신청 업무 {i}'))
        db.con.execute("INSERT INTO task_docs(task_id,doc_id,origin) VALUES (?,?,'user')", (i, i))
        db.sync_task_items(i, [{'kind': 'form', 'slot': 'form', 'content': '신청서', 'basis_state': 'stated',
                               'evidence': [(i, '본문', '서식: 신청서')]}])
        db.con.execute("INSERT INTO doc_years(doc_id,kind,year) VALUES (?,'performance',?)", (i, year-1))
        projection.project_task(db, i)
    yield db
    db.close()


def test_namesakes_scoped_to_selected_quote_and_no_labels_returned(db):
    result = graph.search(db, '신청서가 필요해?', QueryScope(doc_ids=[2]), budget_ms=2000)
    assert len(result.hits) == 1 and not result.fallback
    hit = result.hits[0]
    assert db.con.execute('SELECT doc_id FROM chunks WHERE id=?', (hit.chunk_id,)).fetchone()[0] == 2
    assert hit.scope_fingerprint == QueryScope(doc_ids=[2]).fingerprint()
    assert hit.path == (hit.claim_id,)
    assert hit.validation_version and hit.generation
    assert graph.search(db, '신청서', QueryScope(doc_ids=[])).hits == []


def test_outside_seed_cannot_bridge_scope(db):
    outside = db.con.execute('SELECT id FROM chunks WHERE doc_id=1').fetchone()[0]
    assert not graph.search(db, '알 수 없는 대상', QueryScope(doc_ids=[2]), seed_chunks=[outside]).hits


def test_rejected_and_stale_quotes_never_return(db):
    claim = db.con.execute("SELECT * FROM kg_claims WHERE predicate='REQUIRES_FORM' ORDER BY rowid").fetchone()
    KnowledgeStore(db).record_decision('claim', claim['claim_key'], 'reject', '검토자')
    assert not graph.search(db, '신청서', QueryScope(doc_ids=[1])).hits
    db.con.execute("UPDATE documents SET hash='new-hash' WHERE id=2")
    assert not graph.search(db, '신청서', QueryScope(doc_ids=[2])).hits


def test_quote_chunk_binding_is_rechecked(db):
    db.con.execute("UPDATE chunks SET text='근거가 없는 본문' WHERE doc_id=1")
    assert not graph.search(db, '신청서', QueryScope(doc_ids=[1])).hits


def test_timeout_falls_back_and_clears_progress_handler(db):
    result = graph.search(db, '신청서', budget_ms=0)
    assert result.fallback == 'timeout' and not result.hits
    assert db.con.execute('SELECT COUNT(*) FROM chunks').fetchone()[0] == 2


def test_shadow_and_off_preserve_candidates(db):
    base, strong = [(1, .03)], {1}
    assert graph.augment(db, '신청서', QueryScope(), base, strong)[:2] == (base, strong)
    db.set_meta('knowledge_search_mode', 'shadow')
    combined, qualified, diagnostic = graph.augment(db, '신청서', QueryScope(doc_ids=[2]), base, strong)
    assert combined == base and qualified == strong
    assert diagnostic['candidates'] == 1 and 'paths' not in diagnostic


def test_on_adds_separate_rrf_contribution_once(db):
    db.set_meta('knowledge_search_mode', 'on')
    cid = db.con.execute('SELECT id FROM chunks WHERE doc_id=2').fetchone()[0]
    combined, strong, diagnostic = graph.augment(db, '신청서', QueryScope(doc_ids=[2]), [(cid, .1)], {cid})
    assert combined == [(cid, .1 + graph.WEIGHT / 61)] and strong == {cid}
    assert diagnostic['paths'][0]['chunk_id'] == cid


@pytest.mark.parametrize('basis,expected', [('document_date', [1]), ('performance', [2]),
                                          ('evaluation', []), ('submitted', [])])
def test_year_basis_is_common_to_search_and_selection(db, basis, expected):
    scope = QueryScope(years=[2025], year_basis=basis)
    assert scope.search_documents(db) == expected
    answer = rag.search_only(db, '신청서', scope)
    assert {citation.doc_id for citation in answer.citations} == set(expected)


def test_case_year_uses_explicit_basis_document_and_intersection(db):
    db.con.execute('INSERT INTO work_cases(id,task_id,period_year,basis_doc_id) VALUES (1,1,2024,2)')
    scope = QueryScope(years=[2024], year_basis='case', case_id=1)
    assert scope.search_documents(db) == [2]
    assert QueryScope(years=[2025], year_basis='case', case_id=1).search_documents(db) == []
    assert QueryScope(doc_ids=[1], year_basis='case', case_id=1).search_documents(db) == []
    assert {c.doc_id for c in rag.search_only(db, '신청서', scope).citations} == {2}


def test_scope_contract_and_fingerprint():
    with pytest.raises(ValueError):
        QueryScope(year_basis='unknown')
    with pytest.raises(ValueError):
        QueryScope(year_basis='case')
    assert QueryScope(doc_ids=None).fingerprint() != QueryScope(doc_ids=[]).fingerprint()
    assert QueryScope(doc_ids=[1, 2]).fingerprint() == QueryScope(doc_ids=[2, 1, 1]).fingerprint()
    merged = QueryScope(years=[2026]).merged_with(QueryScope(years=[2024], year_basis='performance'))
    assert merged.years == [2024] and merged.year_basis == 'performance'


def test_search_only_returns_and_saves_paths_for_selected_original(db):
    db.set_meta('knowledge_search_mode', 'on')
    answer = rag.search_only(db, '신청서', QueryScope(doc_ids=[2]))
    assert answer.citations and answer.citations[0].graph_paths
    assert answer.citations[0].doc_id == 2


def test_index_rollback_during_question_discards_answer(db, monkeypatch):
    db.set_meta('index_revision', '5')
    def changing(*args, **kwargs):
        db.set_meta('index_revision', '4')
        return rag.Answer('q', '낡은 답', False, citations=[rag.Citation(1, 1, 'a', '본문', 'a')])
    monkeypatch.setattr(rag, '_ask', changing)
    answer = rag.ask(db, 'q')
    assert answer.error and not answer.citations and not answer.text


def test_notebook_rechecks_graph_after_law_lookup_before_save(db, monkeypatch):
    from app.core import notebook
    nid = notebook.create(db, '범위 시험')
    notebook.add_sources(db, nid, [1])
    monkeypatch.setattr(notebook, 'ask', lambda *a, **k: rag.Answer('q', 'answer', False))
    def changing(*args):
        db.set_meta('kg_revision', str(int(db.get_meta('kg_revision') or 0)+1))
        return {}
    monkeypatch.setattr(notebook, '_law_lookup', changing)
    with pytest.raises(ValueError, match='바뀌었습니다'):
        notebook.query(db, nid, 'q', law=True)
    assert not notebook.messages(db, nid)


def test_readonly_cli_returns_existing_evidence_without_changing_project(db, capsys):
    from app.tools.knowledge_search import main
    before = [tuple(r) for r in db.con.execute('SELECT * FROM kg_jobs ORDER BY partition_key')]
    assert main(['--project', str(db.path.parent), '--query', '신청서', '--docs', '2']) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result['hits']) == 1
    assert before == [tuple(r) for r in db.con.execute('SELECT * FROM kg_jobs ORDER BY partition_key')]
    with Database(db.path, readonly=True) as readonly:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            readonly.set_meta('should_not_exist', '1')


def test_notebook_explicit_scope_cannot_expand_selected_sources(db, monkeypatch):
    from app.core import notebook
    nid = notebook.create(db, '선택 교집합')
    notebook.add_sources(db, nid, [1])
    seen = []
    def inspect(*args, **kwargs):
        seen.append(kwargs['scope'])
        return rag.Answer('q', '', True)
    monkeypatch.setattr(notebook, 'ask', inspect)
    notebook.query(db, nid, 'q', chosen_scope=QueryScope(doc_ids=[2], years=[2025], year_basis='performance'))
    assert seen[0].doc_ids == [] and seen[0].year_basis == 'performance' and seen[0].years == [2025]


def test_graph_failure_uses_existing_fts_results(db, monkeypatch):
    db.set_meta('knowledge_search_mode', 'on')
    monkeypatch.setattr(graph, 'search', lambda *a, **k: graph.Result(fallback='timeout'))
    answer = rag.search_only(db, '신청서', QueryScope(doc_ids=[2]))
    assert [c.doc_id for c in answer.citations] == [2]
    assert answer.generation['graph']['fallback'] == 'timeout'


def test_question_scope_change_during_generation_discards_answer(db, monkeypatch):
    scope = QueryScope(doc_ids=[1])
    def changing(*args, **kwargs):
        scope.doc_ids[:] = [2]
        return rag.Answer('q', 'old', False)
    monkeypatch.setattr(rag, '_ask', changing)
    answer = rag.ask(db, 'q', scope=scope)
    assert answer.error and not answer.text


def test_budget_bounds_high_degree_traversal(db, monkeypatch):
    db.sync_task_items(1, [{'kind': 'form', 'slot': f'form-{i}', 'content': '신청서',
                           'basis_state': 'stated', 'evidence': [(1, '본문', '서식: 신청서')]}
                          for i in range(130)])
    projection.project_task(db, 1)
    # Every candidate is visited but fails the last anchor recheck; this exposes
    # unbounded traversals that would otherwise stop after five accepted hits.
    monkeypatch.setattr(graph, 'match_quote', lambda *a, **k: None)
    cid = db.con.execute('SELECT id FROM chunks WHERE doc_id=1').fetchone()[0]
    result = graph.search(db, '순위로 찾은 자료', QueryScope(doc_ids=[1]), seed_chunks=[cid], budget_ms=2000)
    assert result.visited == 100 and not result.hits and not result.fallback
