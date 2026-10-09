import json

from app.tools import notebook_eval, rag_quality
from app.search.rag import Answer
from test_rag_scope_upgrade import Model


def test_notebook_fixtures_are_frozen_and_workloads_are_disjoint():
    dev, manifest = rag_quality.frozen_suite('dev', notebook_eval.FIXTURES)
    holdout, _ = rag_quality.frozen_suite('holdout', notebook_eval.FIXTURES)
    assert len(dev['scenarios']) == len(holdout['scenarios']) == 60
    assert not ({d['filename'] for d in dev['documents']} & {d['filename'] for d in holdout['documents']})
    assert len({s['category'] for s in dev['scenarios']}) == 12
    assert 'app/core/notebook.py' in manifest['baseline_code']


def test_evaluator_executes_notebook_query_and_checks_stored_quotes(tmp_path):
    suite = {'documents': [{'filename': '예산.txt', 'year': 2026,
                           'text': '예산 담당 인원은 7명이다'}]}
    db = rag_quality.prepare(tmp_path, suite, 'test-only', Model('예산 담당 인원은 7명이다'))
    try:
        scenario = {'id': 'roundtrip', 'category': 'count', 'sources': ['예산.txt'],
                    'turns': [{'question': '예산 담당 인원은 몇 명이야?', 'expect': 'answer',
                               'must_contain': ['7'], 'must_cite_any': ['예산.txt']}]}
        result = notebook_eval.run_scenario(db, scenario, Model('예산 담당 인원은 7명이다'), False)
        assert result['passed']
        row = db.con.execute('SELECT citations FROM notebook_messages').fetchone()
        assert json.loads(row[0])[0]['exact_quote'] == '예산 담당 인원은 7명이다'
        assert result['turns'][0]['generation']['retrieval']['scope_documents'] == 1
    finally:
        db.close()


def test_search_fallback_is_not_scored_as_correct_abstention(tmp_path, monkeypatch):
    from app.core import notebook
    suite = {'documents': [{'filename': '예산.txt', 'year': 2026, 'text': '예산 담당 인원은 7명이다'}]}
    model = Model('예산 담당 인원은 7명이다')
    db = rag_quality.prepare(tmp_path, suite, 'search-only', model)
    try:
        def search(*args, **kwargs):
            return Answer('q', '검색 결과', True, generation={'mode': 'search_only'})

        monkeypatch.setattr(notebook, 'ask', search)
        spec = {'id': 'missing', 'category': 'fallback', 'sources': ['예산.txt'],
                'turns': [{'question': '질문', 'expect': 'withhold'}]}
        result = notebook_eval.run_scenario(db, spec, model, False)
        assert not result['passed'] and '평가' in result['turns'][0]['failures'][0]
    finally:
        db.close()
