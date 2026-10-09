import hashlib
from types import SimpleNamespace

import pytest

from app.db import Database
from app.search import intent,quantity
from app.search.rag import _focus,_off_the_point,ask
from app.search.vector import pack
from app.search.verify import VerifiedSentence


def frame(question='2026년 휴게공간 정수기 수량은 몇 대야?'):
    return intent.frame(question,_focus(question))


@pytest.mark.parametrize('reply',[
    '2026년 정수기 수량에 대한 기록이 있습니다.',
    '2026년 10월 2일에 정수기 수량을 조사했습니다.',
    '정수기 수량은 본관 4층에서 확인했습니다.',
    '정수기 수량 기록의 관리번호는 제3호입니다.',
    '정수기 수량 정보는 없습니다.',
])
def test_record_dates_addresses_and_missing_information_are_not_quantities(reply):
    assert quantity.verdict(frame(),reply,reply)!=''


@pytest.mark.parametrize('value',['12대','0대','2,026대','한 대','두 대','세 대'])
def test_explicit_count_and_zero_are_preserved(value):
    text='정수기 수량은 '+value+'입니다.'
    assert quantity.verdict(frame(),text,text)==''


def test_count_equal_to_question_year_is_not_removed_as_a_date():
    text='정수기 수량은 2026대입니다.'
    assert quantity.verdict(frame(),text,text)==''


def test_no_space_after_total_is_a_valid_arabic_quantity():
    text='정수기 수량은 총12대입니다.'
    assert quantity.verdict(frame(),text,text)==''


def test_number_from_a_floor_cannot_be_used_as_an_equipment_count():
    assert quantity.verdict(frame(),'정수기 수량은 4대입니다.',
                            '2026년 본관 4층에서 정수기 수량을 조사했습니다.')=='quantity_unsupported'


def test_people_count_is_not_an_equipment_count():
    assert quantity.verdict(frame(),'정수기 수량은 4대입니다.',
                            '정수기 수량: 4명')=='quantity_unsupported'


def test_percentage_is_not_a_case_count():
    asked=frame('분기접수 접수 건수는 몇 건이야?')
    assert quantity.verdict(asked,'접수 건수는 90%입니다.','접수 건수: 45건\n처리율: 90%')=='quantity_unit_mismatch'


@pytest.mark.parametrize('unit,other',[('명','사람'),('곳','개소')])
def test_equivalent_unit_names_are_preserved(unit,other):
    asked=frame(f'2026년 점검 인원은 몇 {unit}이야?')
    assert quantity.verdict(asked,f'점검 인원은 9{unit}입니다.',f'점검 인원: 9{other}')==''


@pytest.mark.parametrize('original',['접수 건수: -3건','접수 건수: -3'])
def test_sign_cannot_be_dropped_when_matching_quantities(original):
    asked=frame('접수 건수는 몇 건이야?')
    assert quantity.verdict(asked,'접수 건수는 3건입니다.',original)=='quantity_unsupported'


def test_negative_difference_is_preserved_when_explicitly_supported():
    asked=frame('증감 건수는 몇 건이야?')
    assert quantity.verdict(asked,'증감 건수는 -3건입니다.','증감 건수: -3건')==''


@pytest.mark.parametrize('reply,original',[
    ('정수기 수량은 12입니다.','정수기 수량: 12'),
    ('정수기 수량은 12입니다.','정수기 수량은 12대입니다.'),
    ('정수기 수량은 12대입니다.','정수기 수량: 12'),
])
def test_bare_scalar_is_allowed_only_when_bound_to_the_requested_field(reply,original):
    assert quantity.verdict(frame(),reply,original)==''


def test_unbound_management_number_cannot_authorize_a_bare_scalar():
    assert quantity.verdict(frame(),'정수기 수량은 12입니다.','정수기 수량을 조사했습니다. 관리번호: 12')=='quantity_unsupported'


def test_bare_table_value_retains_its_row_binding():
    asked=frame('분기접수 접수 건수는 몇 건이야?')
    table='구분 | 1분기 | 2분기 / 접수 건수 | 45 | 37 /'
    assert quantity.verdict(asked,'접수 건수는 45입니다.',table)==''


def test_explicit_zero_and_missing_information_are_different():
    asked=frame('2026년 민원 접수 건수는 몇 건이야?')
    assert quantity.verdict(asked,'접수 건수는 없습니다.','접수 건수: 없음')==''
    assert quantity.verdict(asked,'접수 건수 기록은 없습니다.','접수 건수 기록은 없습니다.')=='quantity_missing'
    assert quantity.verdict(asked,'접수 건수 원문은 없습니다.','접수 건수 원문은 없습니다.')=='quantity_missing'


def test_negated_one_case_is_zero_not_one():
    asked=frame('민원 접수 건수는 몇 건이야?')
    text='접수 건수는 한 건도 없었습니다.'
    assert quantity.explicit(text,'건')=={(0,'건')}
    assert quantity.verdict(asked,text,'접수 건수: 0건')==''


def test_only_cited_document_can_supply_quantity():
    q='2026년 휴게공간 정수기 수량은 몇 대야?'
    rows=[{'doc_id':1,'filename':'휴게공간.txt'}, {'doc_id':2,'filename':'다른시설.txt'}]
    texts={1:'휴게공간 정수기 수량에 대한 기록이 있습니다.',2:'다른시설 정수기 수량: 12대'}
    kept=[VerifiedSentence('정수기 수량은 12대입니다.',[1])]
    assert _off_the_point(q,kept,rows,texts,frame(q))=='quantity_unsupported'


def test_a_nonquantity_sentence_does_not_hide_a_correct_quantity_sentence():
    q='2026년 휴게공간 정수기 수량은 몇 대야?'
    rows=[{'doc_id':1,'filename':'휴게공간.txt'}]
    kept=[VerifiedSentence('정수기 수량을 조사했습니다.',[1]),VerifiedSentence('정수기 수량은 12대입니다.',[1])]
    assert _off_the_point(q,kept,rows,{1:'정수기 수량: 12대'},frame(q))==''


def test_date_and_prose_questions_do_not_require_a_count():
    for q in ('정수기 점검일은 언제야?','정수기 관련해서 뭘 조사했어?'):
        assert quantity.verdict(frame(q),'정수기 관련 자료입니다.','정수기 관련 자료입니다.')==''


@pytest.mark.parametrize('q,text',[
    ('운송 거리는 몇 km야?','운송 거리는 3km입니다.'),
    ('장비 무게는 몇 kg이야?','장비 무게는 12kg입니다.'),
])
def test_unrecognized_measurement_unit_retains_existing_validation(q,text):
    assert quantity.verdict(frame(q),text,text)==''


@pytest.mark.parametrize('text,reply,withheld',[
    ('2026년 휴게공간 설비 현황 조사입니다. 정수기 수량과 냉난방기 위치를 기록했습니다.',
     '2026년 휴게공간 정수기 수량에 대한 기록이 있습니다.',True),
    ('2026년 휴게공간 설비 조사입니다. 정수기 수량은 12대입니다.',
     '2026년 휴게공간 정수기 수량은 12대입니다.',False),
    ('2026년 휴게공간 설비 조사입니다. 정수기 수량은 0대입니다.',
     '2026년 휴게공간 정수기 수량은 0대입니다.',False),
])
def test_generated_answer_is_checked_without_extra_model_calls(tmp_path,text,reply,withheld):
    db=Database(tmp_path/'project.db');db.init()
    path=tmp_path/'휴게공간_조사.txt';path.write_text(text,encoding='utf-8')
    doc=db.upsert_document(db.add_source(tmp_path),{'path':str(path),'filename':path.name,'ext':'.txt',
        'hash':hashlib.sha256(path.read_bytes()).hexdigest(),'parse_status':'ok','eff_year':2026})
    chunk=db.replace_document_chunks(doc,[(1,'1문단',text)])[0][0]
    db.save_chunk_embedding(chunk,'fake',3,pack([1,0,0]))
    class Client:
        embed_model=gen_model='fake'
        calls=0
        def health(self):return SimpleNamespace(embedding_ready=True)
        def embed(self,_):return [[1,0,0]],None
        def generate_json(self,*_args,**_kwargs):
            self.calls+=1
            return {'answered':True,'sentences':[{'text':reply,'sources':[1]}]},None,''
    client=Client()
    try:
        answer=ask(db,'2026년 휴게공간 정수기 수량은 몇 대야?',client)
        assert answer.withheld==withheld and not answer.error
        assert client.calls==1
        if withheld:
            assert answer.generation['withheld']=='quantity_missing'
            assert '수량' in answer.guidance and answer.related_docs
        else:
            assert {c.doc_id for c in answer.citations}=={doc}
    finally:
        db.close()
