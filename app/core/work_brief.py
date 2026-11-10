"""업무 안내용 개조식 정보 모델. 사실 값·상태를 유지하고 설명만 짧게 정리한다."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import reconstruction as rc, task_description, workstate as ws


@dataclass
class Fact:
    label: str
    value: str
    state: str = ''


@dataclass
class Stage:
    title: str
    when: str
    action: str
    output: str = ''
    state: str = ''


@dataclass
class Brief:
    bullets: list[str] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    stages: list[Stage] = field(default_factory=list)
    caption: str = ''
    ordered: bool = False
    details: list[str] = field(default_factory=list)
    details_label: str = '상세 안내'


def found_evidence(item):
    """근거 문장 한 줄 — '[갈래] 문장 (파일명)'."""
    category = (item.get('slot') or '').split(' · ')[0].strip()
    names = list(dict.fromkeys(e.get('filename') for e in item.get('evidence') or [] if e.get('filename')))
    return (f'[{category}] ' if category else '') + item['content'] + (f"  — {', '.join(names)}" if names else '')


def points(text):
    """원문 값을 줄이지 않고 문장 단위로 나눈다. 날짜의 마침표는 유지한다."""
    return [s.strip() for s in re.split(r'(?<=[다함임요])\.\s+|\n+', text) if s.strip()]


def suggested_stages(audit=False, council=False):
    if audit and council:
        return [
            Stage('감사계획 준비', '계획 수립 시', '대상 기관·실적 기간·일정·요구자료 확인', '감사계획·요구목록 초안'),
            Stage('자료 요구', '자료 요구 시', '내부 검토 및 의결·결재 절차 확인 → 대상 기관에 요구 항목·서식·기한 안내', '자료 요구 공문'),
            Stage('접수·보완', '감사 준비 기간', '제출자료의 누락·보완사항 확인', '위원회 검토·질의 준비자료'),
            Stage('감사 진행 지원', '감사 실시 기간', '일정·추가 자료 요구·답변 관리', '회의·감사 기록'),
            Stage('결과 정리', '감사 종료 후', '보고·의결·통보 절차 지원 및 처리결과 관리', '결과보고서·시정·처리 요구사항'),
        ]
    if audit:
        return [
            Stage('요구사항 확인', '요청 접수 시', '대상 기관·실적 기간·기준일·서식·제출처 확인', '요청 항목 목록'),
            Stage('부서별 자료 요청', '부서별 요청 시', '작성 부서·담당자 지정 / 내부 회신기한·증빙 기준 안내', '부서별 자료 요청서'),
            Stage('취합·보완', '내부 취합 마감까지', '회신자료·증빙 취합 / 수치·집계기간·누락·답변 일치 여부 점검', '제출자료 초안·보완 목록'),
            Stage('검토·제출', '최종 제출기한 전', '소관 부서 사실 확인 → 내부 검토·결재 → 지정 회신처 제출', '제출본·붙임·발송 기록'),
            Stage('추가답변·후속 조치', '감사 진행 중·종료 후', '추가 요구·질의 답변 취합·회신 / 통보된 지적사항의 조치계획·처리결과 관리', '추가 답변자료·조치관리 목록'),
        ]
    return [
        Stage('범위 확인', '착수 시', '요청 목적·대상·기준일·기한·제출처 확인', '처리 항목 목록'),
        Stage('자료 취합', '내부 마감까지', '담당자별 자료 접수 / 누락·수치·증빙 확인', '결과물 초안·보완 목록'),
        Stage('검토·제출', '제출 전', '검토·보완·필요 결재 → 지정 제출처 송부', '제출본·붙임·발송 기록'),
        Stage('접수·후속 확인', '제출 후', '접수 여부·추가 보완 요청 확인', '최종본·접수 기록·후속 할 일'),
    ]


def _state(row):
    return ws.describe(row['basis_state'], row['applicability'], row['human_state'])


def _label(item):
    if item['kind'] == rc.SCOPE:
        return {'감사주체': '감사 주체', '감사기관': '감사 기관', '감사대상기관': '대상 기관',
                '감사대상기간': '감사 대상 기간', '감사기간': '감사 실시 기간',
                '담당업무': '내 역할', '수감기관': '수감기관',
                '자료작성기준일': '자료 작성 기준일'}.get(item['slot'].replace(' ', ''), item['slot'])
    return rc.KIND_LABEL.get(item['kind'], item['slot'])


def populate(task, card, sections, cycle=None, summary=None):
    """UI 개요·표·흐름도는 상세 매뉴얼과 같은 분석 항목에서 구성한다."""
    task = dict(task)
    by_key = {s.key: s for s in sections}
    audit = task['name'].replace(' ', '') in ('행정사무감사', '행정사무감사준비', '사무감사준비')
    items = card.todo + card.prepare

    def facts(kinds):
        rows = []
        for kind in kinds:
            for item in (i for i in items if i['kind'] == kind):
                value = item['content']
                if item['kind'] == rc.OUTPUT:
                    value = ' · '.join(task_description._artifact(part, task['name']) for part in value.split(' / '))
                rows.append(Fact(_label(item), value, _state(item)))
        return rows

    overview = Brief(facts=facts((rc.PURPOSE, rc.SCOPE)))
    order = ('목적', '감사 주체', '감사 기관', '대상 기관', '수감기관', '감사 대상 기간', '감사 실시 기간', '내 역할')
    overview.facts.sort(key=lambda f: order.index(f.label) if f.label in order else len(order))
    if audit:
        overview.bullets.append('업무 개념 · 지방의회가 해당 지방자치단체의 사무 처리 내용을 감사')
        overview.caption = '일반 개념 · 실제 대상·기간·역할은 이번 회차 감사계획·업무분장 기준'
        if task.get('origin') == 'user' and task.get('description'):
            overview.bullets.extend(points(task['description']))
    elif not any(i['kind'] == rc.PURPOSE for i in items):
        existing = task.get('description') or ''
        inventory = re.search(r'문서\s*[\d,]+\s*건|폴더.*자료|본문을 읽지 못한', existing)
        if existing and (task.get('origin') == 'user' or not inventory):
            overview.bullets.extend(points(existing))
        elif card.stated_steps or card.precedent_steps:
            steps = card.stated_steps or card.precedent_steps
            labels = [re.sub(r'\s*\([^)]*20\d{2}[^)]*\)', '', s['label']) for s in steps]
            overview.facts.append(Fact('처리 내용' if card.stated_steps else '과거 수행 내용', ' / '.join(labels[:4]),
                                       '문서에 적힌 절차' if card.stated_steps else '과거 사례 · 현재 적용 여부 확인'))
        else:
            overview.bullets.append('업무 목적·담당 범위 · 요청 공문과 업무분장 확인 필요')
    if not any(i['kind'] == rc.SCOPE and i['slot'].replace(' ', '') == '담당업무' for i in items):
        overview.bullets.append('내 역할 확인 · 직접 작성 / 타 부서 취합 / 최종 검토·제출 담당 구분')
    if card.found:
        # 문장을 하나씩 늘어놓지 않는다. 종합 요약을 맨 위에 두고 원문 문장은 근거로 접어 둔다.
        summary = summary or {}
        by_model = summary.get('by') not in (None, 'rule')
        state = (f"자료 문장 {len(card.found)}건 종합 · "
                 + ('AI 요약 · ' if by_model else '자동 정리 · ') + '자료에서 추정 · 사람 확인 없음')
        lead = []
        if summary.get('summary'):
            lead.append(Fact('종합 요약', summary['summary'], state))
        if summary.get('keywords'):
            lead.append(Fact('핵심 키워드', ' · '.join(summary['keywords'])))
        overview.facts[:0] = lead
        overview.details = [found_evidence(i) for i in card.found]
        overview.details_label = f'근거 문장 {len(card.found)}건'
    by_key['overview'].brief = overview

    first = Brief(bullets=['대조 자료 · 이번 회차 요청 공문·시행계획 ↔ 지난 회차 제출본',
                           '확인 항목 · 대상 범위 / 기준일 / 제출 항목·서식 / 기한 / 회신처',
                           '진행 상태 · 부서별 담당자 / 접수 / 미회신 / 보완 건 파악'])
    steps_by_id = {s.get('id'): s for s in card.stated_steps + card.precedent_steps if s.get('id')}
    gaps_by_id = {g.get('id'): g for g in card.gaps if g.get('id')}
    for action in card.actions:
        step = steps_by_id.get(action.get('step_id'))
        gap = gaps_by_id.get(action.get('gap_id'))
        if step and step['procedure_kind'] == ws.PROCEDURE_PRECEDENT:
            value = step['label']
            if step.get('output'):
                value += ' / 참고 자료: ' + task_description._artifact(step['output'], task['name'])
            first.facts.append(Fact('지난 수행 사례', value, _state(step)))
        elif step and action['type'] == 'do':
            first.facts.append(Fact('지금 할 일', step['label'], _state(step)))
        elif gap and gap.get('field') in ('기한', '제출처', '절차', '이후 상태'):
            checks = {'기한': '이번 요청·시행 공문의 본문·붙임에서 기한 확인',
                      '제출처': '요청 공문 발신 부서·회신처와 지난 제출본 수신처 대조',
                      '절차': '현행 지침·시행계획·운영 매뉴얼에서 수행 순서 확인',
                      '이후 상태': '마지막 기록 이후의 파일·결재·발송 문서 확인'}
            first.facts.append(Fact('먼저 확인 · ' + gap['field'], checks[gap['field']], '확인 필요'))
        else:
            first.bullets.extend(points(action['text']))
        first.details.append(('지금 할 일: ' if action['type'] == 'do' else '먼저 확인할 일: ') + action['text'])
    # 접근권한 재신청 등 별도 담당자 메모도 유지한다.
    first.bullets.extend(by_key['first'].lines[1 + len(card.actions):])
    by_key['first'].brief = first

    timing = Brief(facts=facts((rc.DEADLINE,)))
    if cycle is not None and cycle['kind'] != 'none':
        period = '매월' if cycle['kind'] == 'monthly' else cycle['months'].replace(',', '·') + '월'
        timing.facts.insert(0, Fact('반복 시기', period + (' / ' + cycle['day_hint'] if cycle['day_hint'] else ''),
                                   '담당자 확인' if cycle['decided_by'] == 'user' else '과거 자료에서 추정 · 이번 회차 일정 확인'))
    if audit:
        timing.facts = [Fact('감사 대상 기간', '실적을 집계할 범위 · 감사계획에서 확인'),
                        Fact('감사 실시 기간', '실제 감사 진행일 · 감사계획에서 확인'),
                        Fact('준비·제출 기간', '요청 접수 → 자료 취합·제출 → 추가답변')] + timing.facts
        for row in timing.facts[:2]:
            source = next((i for i in items if i['kind'] == rc.SCOPE and _label(i) == row.label), None)
            if source:
                row.value, row.state = source['content'], _state(source)
    if not any(i['kind'] == rc.DEADLINE for i in items):
        timing.bullets.append('확인 필요 · 착수일 / 내부 취합 마감 / 최종 제출기한 → 요청 공문·시행계획 확인')
    timing.bullets.append('일정 원칙 · 최종 제출 전 검토·보완 시간 확보 / 과거 날짜는 이번 회차와 대조')
    by_key['timing'].brief = timing

    output = Brief(facts=facts((rc.OUTPUT, rc.RECIPIENT, rc.AMOUNT)),
                   bullets=['완료 확인 · 검토·결재본 + 첨부 증빙 + 발송·접수 기록 보관'])
    for kind, label, value in ((rc.OUTPUT, '결과물', '요청 공문의 제출 항목·붙임 서식 확인'),
                               (rc.RECIPIENT, '제출처', '요청 공문의 회신처·내부 취합부서 확인')):
        if not any(i['kind'] == kind for i in items):
            output.facts.append(Fact(label, value, '확인 필요'))
    by_key['output'].brief = output

    prepare = Brief(facts=facts((rc.INPUT, rc.FORM, rc.CONTACT, rc.REGULATION, rc.AGENCY, rc.RISK)))
    if len(prepare.facts) > 12:
        prepare.facts = prepare.facts[:10]
    if not prepare.facts:
        prepare.bullets = ['기본 자료 · 요청 공문·붙임 서식 / 지난 제출본 / 이번 실적·증빙 / 부서별 담당자 목록',
                           '취합표 항목 · 요청 항목 / 작성 부서 / 회신기한 / 접수 / 보완 / 최종 반영']
    by_key['prepare'].brief = prepare

    procedure = Brief()
    if card.conflicting_procedure:
        procedure.bullets = ['자료가 서로 다릅니다 · 절차 확정 전 원문 대조', card.conflicting_procedure['content']]
    if card.stated_steps:
        steps = card.stated_steps
        procedure.stages = [Stage(s['label'], ws.procedure_label(s['procedure_kind']),
                                  s.get('inputs') or '입력 자료 · 해당 단계의 근거 문서 확인',
                                  s.get('output') or '', _state(s)) for s in steps]
        for stage, source in zip(procedure.stages, steps):
            if source.get('forms'):
                stage.action += '\n서식 · ' + source['forms']
            if source.get('done_criteria'):
                stage.output += '\n완료 기준 · ' + source['done_criteria']
        procedure.ordered = len(steps) > 1 and all(
            s.get('after_step_id') and s['after_step_id'] == prev.get('id') for prev, s in zip(steps, steps[1:]))
        procedure.caption = '자료에 명시된 수행 단계' + (' · 화살표는 확인된 선후관계' if procedure.ordered else ' · 선후관계는 근거 문서 확인')
    else:
        council = any(i['kind'] == rc.SCOPE and i['slot'].replace(' ', '') == '담당업무'
                      and i['basis_state'] != ws.CONFLICT and i['human_state'] != ws.RECHECK
                      and '의회' in i['content'] and '수감' not in i['content'] for i in items)
        procedure.stages = suggested_stages(audit, council)
        procedure.ordered = True
        procedure.caption = ('의회 측 감사 지원' if council else '수감기관 자료 취합') + ' · 준비 순서 제안' if audit else '준비 순서 제안'
        procedure.caption += ' · 실제 절차·기한은 요청 공문·운영 지침으로 확인'
    by_key['procedure'].brief = procedure
