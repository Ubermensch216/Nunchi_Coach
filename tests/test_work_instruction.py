"""후임자가 범위·기간·전달할 결과물을 이해하고, 제안과 근거를 구별한다."""
from datetime import date

import pytest

from app.core import reconstruction as rc, task_manual, workcard, workstate as ws
from app.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'instruction.db')
    database.init()
    database.con.execute("INSERT INTO tasks(id,name,description,origin) VALUES "
                         "(1,'행정사무감사','문서 18건입니다. 회차: 2022 · 2023 · 2024 · 2025.','ai')")
    yield database
    database.close()


def sections(db, card=None):
    return {s.key: s for s in task_manual.build(db, 1, card).sections}


def test_audit_without_facts_teaches_concept_and_preparation_without_inventing_dates(db):
    result = sections(db)
    overview = '\n'.join(result['overview'].lines)
    assert '지방의회' in overview and '수감기관' in overview
    assert '감사 대상 기간' in overview and '담당 역할' in overview
    assert '18건' not in overview and '2022' not in overview
    assert '일반 설명' in overview
    timing = '\n'.join(result['timing'].lines)
    assert all(word in timing for word in ('감사 대상 기간', '감사 실시 기간', '준비·제출 기간'))
    procedure = '\n'.join(result['procedure'].lines)
    assert all(word in procedure for word in ('제안', '작성 부서', '증빙', '결재', '질의', '처리결과'))
    assert '아직 확인되지 않았습니다' in '\n'.join(result['output'].lines)


def test_scopes_are_extracted_with_evidence_and_distinct_periods(db, tmp_path):
    source = db.add_source(tmp_path / '자료')
    doc = db.upsert_document(source, dict(path=str(tmp_path / '감사계획.txt'), filename='감사계획.txt',
                                        ext='.txt', hash='audit-plan', parse_status='ok', eff_year=2026))
    db.con.execute('INSERT INTO task_docs(task_id,doc_id) VALUES(1,?)', (doc,))
    texts = ['감사 주체: 가람시의회', '감사 대상 기관: 가람시 환경국',
             '감사 대상 기간: 2025.11.1.~2026.10.31.', '감사 기간: 2026.11.10.~2026.11.20.',
             '담당 업무: 부서별 실적자료 취합 및 제출', '제출처: 기획조정실',
             '제출기한: 2026.11.3.', '최종 산출물: 행정사무감사 제출자료 및 증빙 목록']
    db.replace_sections(doc, [('paragraph', n, f'{n}문단', text) for n, text in enumerate(texts, 1)])
    rc.restore(db, basis=date(2026, 10, 20), statements=False)
    result = sections(db)
    overview = '\n'.join(result['overview'].lines)
    assert '가람시의회' in overview and '가람시 환경국' in overview
    assert '2025.11.1.~2026.10.31' in overview and '2026.11.10.~2026.11.20' in overview
    assert '부서별 실적자료 취합 및 제출' in overview
    assert any(e['doc_id'] == doc for e in result['overview'].evidence)
    assert '기획조정실' in '\n'.join(result['output'].lines)
    assert '2026-11-03' in '\n'.join(result['timing'].lines)


def test_conflicting_or_changed_facts_are_not_promoted_to_overview(db):
    for slot, value, state in [('감사주체', '충돌한 기관', ws.CONFLICT),
                                ('담당업무', '재확인할 역할', ws.STATED)]:
        item = db.add_task_item(1, rc.SCOPE, value, slot=slot)
        db.con.execute('UPDATE task_items SET basis_state=?, human_state=? WHERE id=?',
                       (state, ws.RECHECK if state == ws.STATED else ws.NONE, item))
    overview = '\n'.join(sections(db)['overview'].lines)
    assert '확인이 필요한 감사주체: 충돌한 기관' in overview
    assert '자료에서 확인한 감사주체' not in overview
    assert '자료에서 확인한 담당업무' not in overview


def test_output_names_and_user_supplements_reach_instruction_without_raw_filenames(db):
    db.add_task_item(1, rc.OUTPUT, '2025_행정사무감사_의원질의답변_송부.docx')
    db.add_task_item(1, rc.RECIPIENT, '의회사무국')
    result = sections(db)
    text = '\n'.join(result['output'].lines)
    assert '의원질의답변' in text and '의회사무국' in text and '.docx' not in text
    assert '후임자 확인' in text


def test_explicit_procedure_is_kept_instead_of_a_suggested_replacement(db):
    card = workcard.Card(1, '행정사무감사', stated_steps=[dict(
        label='요청 항목별 검토 후 회신', procedure_kind=ws.PROCEDURE_STATED,
        basis_state=ws.STATED, applicability=ws.CURRENT, human_state=ws.NONE,
        inputs='부서별 실적', output='검토 완료본', done_criteria='회신 접수', evidence=[])])
    procedure = '\n'.join(sections(db, card)['procedure'].lines)
    assert all(word in procedure for word in ('요청 항목별 검토 후 회신', '부서별 실적', '검토 완료본', '회신 접수'))
    assert '준비 순서 제안' not in procedure


def test_council_responsibility_uses_council_preparation_instead_of_received_agency_steps(db):
    db.add_task_item(1, rc.SCOPE, '의회 측 감사계획 작성 및 결과보고 지원', slot='담당업무')
    result = sections(db)
    procedure = '\n'.join(result['procedure'].lines)
    assert '의회 측 감사 지원 담당일 때' in procedure
    assert '결과보고서' in procedure and '대상 기관에 요구 항목' in procedure
    assert '수감기관 자료 취합 담당일 때' not in procedure
    assert '담당 역할을 먼저 구분하세요' not in '\n'.join(result['overview'].lines)
