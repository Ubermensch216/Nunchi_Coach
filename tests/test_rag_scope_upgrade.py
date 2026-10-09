"""범위·후보 누락·대상 역할의 실제 회귀 사례와 양성 대조."""
import hashlib
from types import SimpleNamespace

import pytest

from app.core import notebook
from app.db import Database
from app.search.query import QueryScope
from app.search.rag import _distinctive_terms, _select_context, _subject_unknown, ask
from app.search.vector import pack


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
