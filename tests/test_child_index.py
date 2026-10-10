"""부모 ID 보존, 자식 검색과 원자적 전환 계약."""
from unittest.mock import Mock

import numpy as np
import pytest

from app.core.child_chunking import build_children
from app.core.child_index import embed_children
from app.db import Database
from app.search import fts, rag
from app.search.vector import pack


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / 'project.db') as value:
        value.init()
        yield value


def document(db, number=1, text=None):
    source = db.add_source('/source')
    doc = db.upsert_document(source, {'path': f'/source/{number}.txt', 'filename': f'{number}.txt',
                                     'ext': '.txt', 'parse_status': 'ok', 'hash': str(number)})
    text = text or ('계약 절차를 검토합니다.\n' * 70 + '마지막 지급기한은 12월 3일입니다.')
    parent = db.replace_document_chunks(doc, [(1, '1문단', text)])[0][0]
    db.save_chunk_embedding(parent, 'test', 2, pack([1, 0]))
    return doc, parent, text


def test_sentence_children_preserve_offsets_and_cover_parent():
    text = '절차를 확인합니다. 다음으로 서류를 검토합니다.\n' * 80
    children = build_children(text)
    assert len(children) > 1
    assert ''.join(c.text for c in children) == text
    assert all(c.text == text[c.start:c.end] and len(c.text) <= 500 for c in children)


def test_table_children_repeat_header_without_splitting_rows():
    header = '[예산]\n사업명\t인원(명)\t예산(천원)\n'
    rows = [f'사업{i}\t{i}\t{i * 100}\n' for i in range(80)]
    text = header + ''.join(rows)
    children = build_children(text)
    assert len(children) > 1
    assert all(c.text.startswith(header) for c in children)
    assert ''.join(text[c.start:c.end] for c in children) == text
    for row in rows:
        assert any(row in child.text for child in children)


def test_long_table_row_is_not_cut_and_empty_has_no_children():
    row = '대상\t' + '가' * 1000 + '\t42명\n'
    assert any(row in c.text for c in build_children('사업\t인원\n' + row))
    assert build_children('   ') == []


def test_backfill_preserves_citations_and_vectors_and_is_idempotent(db):
    from app.core import notebook
    doc, parent, text = document(db)
    book = notebook.create(db, '검증')
    notebook.add_sources(db, book, [doc])
    proof = [{'doc_id': doc, 'chunk_id': parent, 'locator': '1문단', 'exact_quote': text, 'version_hash': '1'}]
    assert notebook.evidence_current(db, book, proof)
    before = tuple(db.con.execute('SELECT * FROM chunks WHERE id=?', (parent,)).fetchone())
    assert db.ensure_child_chunks(doc) > 1
    ids = [r[0] for r in db.con.execute('SELECT id FROM chunk_children')]
    assert db.ensure_child_chunks(doc) == 0
    assert ids == [r[0] for r in db.con.execute('SELECT id FROM chunk_children')]
    assert tuple(db.con.execute('SELECT * FROM chunks WHERE id=?', (parent,)).fetchone()) == before
    assert db.con.execute('SELECT COUNT(*) FROM embeddings').fetchone()[0] == 1
    context = rag._load_chunk_context(db, [parent])[0]
    assert context['text'] == text and context['locator'] == '1문단'
    assert notebook.evidence_current(db, book, proof)
    db.con.execute("UPDATE documents SET hash='changed' WHERE id=?", (doc,))
    assert not notebook.evidence_current(db, book, proof)


def test_child_fts_is_scoped_deduplicated_and_restores_parent(db):
    first, parent, _ = document(db)
    second, excluded, _ = document(db, 2)
    db.ensure_child_chunks(first)
    db.ensure_child_chunks(second)
    assert fts.search_chunk_ids(db, '계약 절차', doc_ids=[first]) == [parent]
    assert excluded not in fts.search_chunk_ids(db, '계약 절차', doc_ids=[first])
    assert fts.search_chunk_ids(db, '지급기한', doc_ids=[first]) == [parent]
    assert fts.search_chunk_ids(db, '계약', doc_ids=[]) == []


def test_unprepared_parent_still_has_lexical_search(db):
    _, parent, _ = document(db)
    assert fts.search_chunk_ids(db, '지급기한') == [parent]


def test_mixed_prepared_and_legacy_parents_share_search_without_duplicates(db):
    doc, parent, _ = document(db)
    _, legacy, _ = document(db, 2)
    db.ensure_child_chunks(doc)
    for child, _ in db.children_needing_embedding(doc, 'test'):
        db.save_child_embedding(child, 'test', 2, pack([0, 1]))
    assert set(fts.search_chunk_ids(db, '지급기한')) == {parent, legacy}
    ids, matrix = rag._load_chunk_matrix(db, 'test')
    assert set(cid for cid, _ in rag._parent_vector_rank(ids, matrix @ np.array([1, 1]))) == {parent, legacy}


def test_inconsistent_child_dimensions_keep_parent_search(db):
    doc, parent, _ = document(db)
    db.ensure_child_chunks(doc)
    pending = db.children_needing_embedding(doc, 'test')
    for index, (child, _) in enumerate(pending):
        vector = [0, 1, 0] if index else [0, 1]
        db.save_child_embedding(child, 'test', len(vector), pack(vector))
    ids, matrix = rag._load_chunk_matrix(db, 'test', [doc])
    assert ids.tolist() == [parent]
    np.testing.assert_array_equal(matrix, [[1, 0]])


def test_vectors_switch_only_after_every_child_is_ready(db):
    doc, parent, _ = document(db)
    db.ensure_child_chunks(doc)
    pending = db.children_needing_embedding(doc, 'test')
    db.save_child_embedding(pending[0][0], 'test', 2, pack([0, 1]))
    ids, matrix = rag._load_chunk_matrix(db, 'test', [doc])
    assert ids.tolist() == [parent]
    np.testing.assert_array_equal(matrix, [[1, 0]])
    for child, _ in pending[1:]:
        db.save_child_embedding(child, 'test', 2, pack([0, 1]))
    ids, matrix = rag._load_chunk_matrix(db, 'test', [doc])
    assert ids.tolist() == [parent] * len(pending)
    assert rag._parent_vector_rank(ids, matrix @ np.array([0, 1])) == [(parent, 1.0)]
    assert rag._load_chunk_matrix(db, 'test', [])[0].size == 0


def test_failure_and_cancel_resume_without_dropping_parent(db):
    doc, parent, _ = document(db)
    db.ensure_child_chunks(doc)
    pending = len(db.children_needing_embedding(doc, 'test'))
    client = Mock(embed_model='test')
    client.embed.side_effect = [([[0, 1]], ''), (None, 'offline')]
    assert embed_children(db, doc, client, batch_size=1) == 'offline'
    assert len(db.children_needing_embedding(doc, 'test')) == pending - 1
    assert embed_children(db, doc, client, lambda: True) == 'cancelled'
    ids, _ = rag._load_chunk_matrix(db, 'test', [doc])
    assert ids.tolist() == [parent]
    client.embed.side_effect = lambda texts: ([[0, 1] for _ in texts], '')
    assert embed_children(db, doc, client) == ''
    assert not db.children_needing_embedding(doc, 'test')


def test_bad_embedding_batch_is_not_saved(db):
    doc, _, _ = document(db)
    db.ensure_child_chunks(doc)
    client = Mock(embed_model='test')
    client.embed.return_value = ([[1]], '')
    assert embed_children(db, doc, client)
    assert db.con.execute('SELECT COUNT(*) FROM child_embeddings').fetchone()[0] == 0


def test_child_creation_rolls_back_on_failure(db, monkeypatch):
    from app.core import child_chunking
    doc, _, _ = document(db)
    db.replace_document_chunks(doc, [(1, '1문단', '첫 원문'), (2, '2문단', '둘째 원문')])
    original = child_chunking.build_children
    def fail(text, context=''):
        if text == '둘째 원문':
            raise RuntimeError('injected')
        return original(text, context)
    monkeypatch.setattr(child_chunking, 'build_children', fail)
    with pytest.raises(RuntimeError, match='injected'):
        db.ensure_child_chunks(doc)
    assert db.con.execute('SELECT COUNT(*) FROM chunk_children').fetchone()[0] == 0
    assert db.con.execute('SELECT COUNT(*) FROM chunks').fetchone()[0] == 2


def test_upgrade_v7_preserves_chunk_id_and_embedding(tmp_path):
    path = tmp_path / 'old.db'
    with Database(path) as db:
        db.init()
        _, parent, text = document(db)
        db.con.execute('DROP TABLE child_fts')
        db.con.execute('DROP TABLE child_embeddings')
        db.con.execute('DROP TABLE chunk_children')
        db.set_meta('schema_version', '7')
        db.set_meta('chunks_checked', '1')
    with Database(path) as db:
        db.init()
        assert db.get_meta('schema_version') == '10'
        assert db.get_meta('chunks_checked') is None
        assert db.con.execute('SELECT text FROM chunks WHERE id=?', (parent,)).fetchone()[0] == text
        assert db.con.execute('SELECT COUNT(*) FROM embeddings').fetchone()[0] == 1
        assert path.with_name(path.name + '.bak-v7').exists()


def test_parent_deletion_cascades_child_fts_and_embeddings(db):
    doc, parent, _ = document(db)
    db.ensure_child_chunks(doc)
    child = db.children_needing_embedding(doc, 'test')[0][0]
    db.save_child_embedding(child, 'test', 2, pack([1, 0]))
    db.con.execute('DELETE FROM chunks WHERE id=?', (parent,))
    assert db.con.execute('SELECT COUNT(*) FROM chunk_children').fetchone()[0] == 0
    assert db.con.execute('SELECT COUNT(*) FROM child_embeddings').fetchone()[0] == 0
    assert db.con.execute("SELECT COUNT(*) FROM child_fts WHERE child_fts MATCH '계약'").fetchone()[0] == 0


def test_heading_context_reaches_the_search_text_but_not_the_quoted_range(db):
    """'집행 현황'이라는 절 제목이 본문에 다시 나오지 않아도 그 절의 대목을 찾는다."""
    from app.core import child_chunking
    doc, _, _ = document(db)
    body = '새봄 센터의 교부액은 31,500천원이다.'
    parent = db.replace_document_chunks(doc, [(1, '3문단', body, '제2장 예산 > 1. 집행 현황')])[0][0]
    db.ensure_child_chunks(doc)
    child = db.con.execute('SELECT * FROM chunk_children WHERE parent_id=?', (parent,)).fetchone()
    assert child['text'] == '제2장 예산 > 1. 집행 현황\n' + body
    assert child['version'] == child_chunking.child_version('문맥')
    assert body[child['start_offset']:child['end_offset']] == body
    assert parent in fts.search_chunk_ids(db, '집행 현황')
    assert db.con.execute('SELECT text FROM chunks WHERE id=?', (parent,)).fetchone()[0] == body
    # 문맥이 없는 부모의 자식은 예전 판 그대로다 — 다시 만들지 않는다.
    assert [c.text for c in build_children(body)] == [body]
    assert child_chunking.child_version() == child_chunking.CHILD_VERSION
