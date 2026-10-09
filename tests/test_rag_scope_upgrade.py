"""범위·후보 누락·대상 역할의 실제 회귀 사례와 양성 대조."""
import hashlib
from types import SimpleNamespace

import pytest

from app.core import notebook
from app.db import Database
from app.search.query import QueryScope
from app.search.rag import _distinctive_terms, _select_context, _subject_unknown, ask
from app.search.vector import pack
from app.search import fts, intent, quantity


@pytest.fixture
def store(tmp_path):
    db = Database(tmp_path / 'project.db')
    db.init()
    source = db.add_source(tmp_path)

    def add(name, text, year=2026):
        path = tmp_path / name
        path.write_text(text, encoding='utf-8')
        doc = db.upsert_document(source, {
            'path': str(path), 'filename': name, 'ext': '.txt', 'parse_status': 'ok',
            'hash': hashlib.sha256(path.read_bytes()).hexdigest(), 'eff_year': year,
        })
        cid = db.replace_document_chunks(doc, [(1, '1문단', text)])[0][0]
        db.save_chunk_embedding(cid, 'fake', 3, pack([1., 0., 0.]))
        return doc, cid

    yield db, add
    db.close()


def test_scoped_relevant_candidate_is_not_lost_beyond_global_window(store):
    db, add = store
    old = [add(f'old{i}.txt', f'예산 신청 설명 {i}', 2025) for i in range(16)]
    doc, target = add('current.txt', '예산 신청 담당 인원은 7명이다')
    ranked = [(cid, .03 - i * .0001) for i, (_, cid) in enumerate([*old, (doc, target)])]
    result = _select_context(db, ranked, {cid for cid, _ in ranked},
                             QueryScope(years=[2026]), '2026년 예산 신청 담당 인원은 몇 명이야?')
    assert target in [cid for cid, _ in result]


def test_scope_before_dedup_does_not_lose_identical_text_in_requested_year(store):
    db, add = store
    _, old = add('old.txt', '예산 신청 담당 인원은 7명이다', 2025)
    _, current = add('current.txt', '예산 신청 담당 인원은 7명이다')
    assert _select_context(db, [(old, .03), (current, .02)], {old, current},
                           QueryScope(years=[2026]), '예산 신청 담당 인원은 몇 명이야?')[0][0] == current


def test_unselected_document_does_not_change_subject_guard(store):
    db, add = store
    selected, _ = add('selected.txt', '사무용품 단가는 100원이다')
    scope = QueryScope(doc_ids=[selected])
    before = _subject_unknown(db, '소독약품 단가는 얼마야?', '단가', scope)
    add('outside.txt', '소독약품 단가는 200원이다')
    assert before and _subject_unknown(db, '소독약품 단가는 얼마야?', '단가', scope)


def test_unselected_documents_do_not_change_distinctive_terms(store):
    db, add = store
    selected, _ = add('selected.txt', '업무 자료')
    other, _ = add('other.txt', '업무 예산')
    scope = QueryScope(doc_ids=[selected, other])
    before = _distinctive_terms(db, ['업무', '예산'], scope)
    for i in range(5):
        add(f'outside{i}.txt', '예산 설명')
    assert _distinctive_terms(db, ['업무', '예산'], scope) == before


class Model:
    embed_model = 'fake'
    gen_model = 'scripted'
    cancelled = False

    def __init__(self, sentence):
        self.sentence = sentence

    def health(self):
        return SimpleNamespace(embedding_ready=True, generation_ready=True)

    def embed(self, texts):
        return [[1., 0., 0.] for _ in texts], None

    def generate_json(self, *args, **kwargs):
        return {'answered': True, 'sentences': [{'text': self.sentence, 'sources': [1]}]}, None, ''


@pytest.mark.parametrize('source,answer,withheld', [
    ('시설방문 업무의 담당 인원은 미정이다. 방문상담 인원은 19명이다.', '방문상담 인원은 19명이다.', True),
    ('시설방문 업무의 담당 인원은 7명이다. 방문상담 인원은 19명이다.', '방문상담 인원은 19명이다.', True),
    ('시설방문 업무의 담당 인원은 7명이다. 방문상담 인원은 19명이다.', '시설방문 담당 인원은 7명이다.', False),
    ('시설방문 업무의 담당 인원은 0명이다.', '시설방문 담당 인원은 0명이다.', False),
    ('시설방문 업무의 담당 인원은 7명이다. 방문상담 인원도 7명이다.', '시설방문 담당 인원은 7명이다.', False),
])
def test_count_must_be_bound_to_requested_role(store, source, answer, withheld):
    db, add = store
    doc, _ = add('시설방문.txt', source)
    result = ask(db, '시설방문 담당 인원은 몇 명이야?', Model(answer), QueryScope(doc_ids=[doc]))
    assert result.withheld == withheld


def test_notebook_keeps_retrieval_diagnostics_without_raw_text(store):
    db, add = store
    doc, _ = add('시설방문.txt', '시설방문 담당 인원은 7명이다')
    book = notebook.create(db, '시설방문')
    notebook.add_sources(db, book, [doc])
    db.set_meta('notebook_query_expansion', '0')
    key, answer = notebook.query(db, book, '시설방문 담당 인원은 몇 명이야?', Model('시설방문 담당 인원은 7명이다'))
    record = answer.generation['retrieval']
    assert record['scope_documents'] == 1 and record['selected_chunks'] >= 1
    assert {'embedding_ms', 'vector_ms', 'lexical_ms', 'selection_ms', 'total_ms'} <= record['timing'].keys()
    assert not any(word in str(record) for word in ('시설방문', '몇 명', '7명'))
    row = db.con.execute('SELECT generation FROM notebook_messages WHERE id=?', (key,)).fetchone()
    assert 'retrieval' in row[0]


def test_two_character_lexical_search_is_scoped(store):
    db, add = store
    doc, cid = add('budget.txt', '예산 신청')
    add('outside.txt', '예산 확정')
    assert fts.search_chunk_ids(db, '예산', doc_ids=[doc]) == [cid]
    assert fts.search_chunk_ids(db, '예산은', doc_ids=[doc]) == [cid]
    assert fts.search_chunk_ids(db, '예산', doc_ids=[]) == []


def test_role_table_preserves_header_binding_without_using_other_columns():
    asked = intent.frame('시설방문 담당 인원은 몇 명이야?', '인원')
    original = '구분 | 담당 인원 | 방문 인원 / 시설방문 | 7 | 19'
    assert quantity.binding_verdict(asked, '담당 인원은 7명이다', original) == ''
    assert quantity.binding_verdict(asked, '담당 인원은 19명이다', original) == 'quantity_role_unbound'


def test_year_and_task_filter_apply_before_vector_pool(store):
    db, add = store
    other, _ = add('old.txt', '계약 담당 인원은 9명이다', 2025)
    doc, _ = add('current.txt', '시설방문 담당 인원은 7명이다')
    db.con.execute("INSERT INTO tasks(id,name) VALUES (1,'시설방문')")
    db.con.execute('INSERT INTO task_docs(task_id,doc_id) VALUES (1,?)', (doc,))
    result = ask(db, '시설방문 담당 인원은 몇 명이야?', Model('시설방문 담당 인원은 7명이다'),
                 QueryScope(years=[2026], task_id=1))
    assert not result.withheld and [c.doc_id for c in result.citations] == [doc]
    assert result.generation['retrieval']['indexed_chunks'] == 1


def test_scope_filters_missing_documents_in_auxiliary_checks(store):
    db, add = store
    doc, _ = add('missing.txt', '소독약품 단가는 7원이다')
    db.con.execute("UPDATE documents SET missing_since='2026-10-09' WHERE id=?", (doc,))
    assert _subject_unknown(db, '소독약품 단가는 얼마야?', '단가')


def test_wrong_embedding_dimension_is_a_search_fallback(store):
    db, add = store
    doc, cid = add('budget.txt', '예산 신청 담당 인원은 7명이다')
    db.save_chunk_embedding(cid, 'fake', 2, pack([1., 0.]))
    result = ask(db, '예산 신청 담당 인원은 몇 명이야?', Model('담당 인원은 7명이다'), QueryScope(doc_ids=[doc]))
    assert result.generation['mode'] == 'search_only'
    assert result.generation['retrieval']['index_dimension_mismatch']


def test_person_count_cannot_borrow_task_name_from_another_cited_document():
    from app.search.rag import _off_the_point
    from app.search.verify import VerifiedSentence
    question = '시설방문 담당 인원은 몇 명이야?'
    rows = [{'doc_id': 1, 'filename': '시설방문.txt'}, {'doc_id': 2, 'filename': '상담.txt'}]
    source = {1: '시설방문 담당 인원은 추후 결정한다.', 2: '지역상담 담당 인원은 19명이다.'}
    assert _off_the_point(question, [VerifiedSentence('담당 인원은 19명이다.', [1, 2])],
                         rows, source, intent.frame(question, '인원')) == 'quantity_role_unbound'


def test_valid_person_count_does_not_authorize_an_invalid_extra_sentence(store):
    db, add = store
    doc, _ = add('시설방문.txt', '시설방문 담당 인원은 7명이다. 방문상담 인원은 19명이다.')

    class Mixed(Model):
        def generate_json(self, *args, **kwargs):
            return {'answered': True, 'sentences': [
                {'text': '시설방문 담당 인원은 7명이다.', 'sources': [1]},
                {'text': '방문상담 인원은 19명이다.', 'sources': [1]},
            ]}, None, ''

    result = ask(db, '시설방문 담당 인원은 몇 명이야?', Mixed(''), QueryScope(doc_ids=[doc]))
    assert not result.withheld and '7명' in result.text and '19명' not in result.text
    assert result.generation['sentences_off_quantity'] == 1


def test_person_role_is_not_borrowed_across_clauses():
    asked = intent.frame('시설방문 담당 인원은 몇 명이야?', '인원')
    original = '시설방문 담당 인원은 7명이며 방문상담 인원은 19명이다.'
    assert quantity.binding_verdict(asked, '담당 인원은 19명이다', original, '시설방문.txt') == 'quantity_role_unbound'
