import hashlib
from types import SimpleNamespace

import pytest

from app.db import Database
from app.search.rag import _focus, ask
from app.search.vector import pack


@pytest.mark.parametrize('word',['관련해서','대해서','관해서'])
def test_topic_connectors_are_not_document_field_names(word):
    assert _focus(f'행정사무감사 때 수질 {word} 뭘 제출했어?')==''


def test_real_field_beginning_with_related_is_still_checked():
    assert _focus('행정사무감사 관련서식은 무엇이야?')=='관련서식'
    assert _focus('행정사무감사 제출기한은 언제야?')=='제출기한'


@pytest.mark.parametrize('word',['관련해서','대해서','관해서'])
def test_prose_answer_is_not_withheld_for_a_topic_connector(tmp_path,word):
    db=Database(tmp_path/'project.db');db.init()
    text='2024년 행정사무감사 수질 관련 제출자료입니다. 원수 수질, 정수장별 검사결과, 수질민원 현황을 포함합니다.'
    path=tmp_path/'행정사무감사.txt';path.write_text(text,encoding='utf-8')
    doc=db.upsert_document(db.add_source(tmp_path),{'path':str(path),'filename':path.name,
        'hash':hashlib.sha256(path.read_bytes()).hexdigest(),'parse_status':'ok','ext':'.txt','eff_year':2024})
    chunk=db.replace_document_chunks(doc,[(1,'1문단',text)])[0][0]
    db.save_chunk_embedding(chunk,'fake',3,pack([1,0,0]))
    class Client:
        embed_model=gen_model='fake'
        def health(self):return SimpleNamespace(embedding_ready=True)
        def embed(self,_):return [[1,0,0]],None
        def generate_json(self,*_args,**_kwargs):
            return {'answered':True,'sentences':[{'text':'원수 수질, 정수장별 검사결과, 수질민원 현황을 제출했습니다.','sources':[1]}]},None,''
    try:
        answer=ask(db,f'행정사무감사 때 수질 {word} 뭘 제출했어?',Client())
        assert not answer.withheld and not answer.error
        assert {c.doc_id for c in answer.citations}=={doc}
        assert all(s in answer.text for s in ('원수','검사결과','수질민원'))
    finally:
        db.close()
