"""실제 모델로 긴 DOCX 부모 검색·업무노트 인용 저장까지 확인한다."""
import json
import os

import docx
import pytest

from app.ai.client import OllamaClient
from app.core import notebook, notebook_ingest
from app.db import Database


@pytest.mark.skipif(os.environ.get('NUNCHICOACH_LIVE_AI') != '1',
                    reason='NUNCHICOACH_LIVE_AI=1 과 로컬 생성·임베딩 모델이 필요합니다')
def test_long_docx_child_search_restores_parent_and_saves_quote(tmp_path):
    path = tmp_path / '공공기록교육_결과.docx'
    document = docx.Document()
    text = ('공공기록교육 운영 절차는 자료 점검, 안내 발송, 기록 정리 순으로 진행한다. ' * 20
            + '공공기록교육 수료인원은 37명이다. 공공기록교육 담당인원은 2명이다.')
    document.add_paragraph(text)
    document.save(path)
    client = OllamaClient()
    client.seed = 0
    health = client.health()
    assert health.embedding_ready and health.generation_ready, health.message
    with Database(tmp_path / 'project.db') as db:
        db.init()
        db.set_meta('notebook_query_expansion', '0')
        book = notebook.create(db, '긴 문서')
        result = notebook_ingest.ingest_file(db, book, path, client)
        assert result.doc_id is not None, result
        parents = db.con.execute('SELECT id,text,locator FROM chunks WHERE doc_id=?', (result.doc_id,)).fetchall()
        assert any(len(row['text']) > 500 for row in parents)
        assert db.con.execute('SELECT COUNT(*) FROM chunk_children').fetchone()[0] > len(parents)
        assert db.unembedded_child_count(client.embed_model) == 0
        message_id, answer = notebook.query(db, book, '공공기록교육 수료인원은 몇 명인가요?', client, mode='rag')
        assert not answer.error and not answer.withheld, answer
        assert '37' in answer.text, answer.text
        saved = json.loads(db.con.execute('SELECT citations FROM notebook_messages WHERE id=?', (message_id,)).fetchone()[0])
        assert saved and notebook.evidence_current(db, book, saved)
        by_id = {row['id']: row for row in parents}
        for citation in saved:
            parent = by_id[citation['chunk_id']]
            assert citation['exact_quote'] == parent['text']
            assert citation['locator'] == parent['locator']
