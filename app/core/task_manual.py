"""업무 안내와 매뉴얼이 함께 읽는 자료 기반 구조. 생성·수정 부작용이 없다."""
from __future__ import annotations

import re

from dataclasses import dataclass, field

from . import (changes, matters, overview_summary, reconstruction as rc, workcard, workstate as ws,
               work_instruction, work_brief)


@dataclass
class Section:
    key: str
    title: str
    lines: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    # 기본으로는 접어 두는 나머지 줄. 화면은 '나머지 N건 펼치기'로 보인다(R05: 준비 항목 708건).
    more: list[str] = field(default_factory=list)
    brief: work_brief.Brief | None = None


@dataclass
class Manual:
    task_id: int
    name: str
    sections: list[Section]


_MAX_CASE_LINES = 15
# 한 구획에 이보다 많으면 먼저 볼 _SHOWN건만 펴 둔다.
_FOLD_OVER = 12
_SHOWN = 10
# 먼저 볼 것: 서식·준비 자료·규정·유의사항이 담당 연락처나 다른 업무와의 관계보다 앞선다.
_FIRST_KINDS = (rc.FORM, rc.INPUT, rc.REGULATION, rc.RISK, rc.EVALUATION, rc.DEADLINE, rc.RECIPIENT,
                rc.PURPOSE, rc.OUTPUT, rc.AMOUNT, rc.CONTACT, rc.AGENCY)


def _ranked(items: list[dict]) -> list[dict]:
    """사람이 확인한 것, 먼저 볼 종류, 지금도 적용될 수 있는 것, 문서에 적힌 것 순. 같으면 원래 순서.

    종류가 먼저다 — 다른 업무와의 관계 표시는 '적용 여부 미상'이라 지난 회차의 서식보다 앞서면 안 된다.
    """
    def key(item):
        kind = _FIRST_KINDS.index(item['kind']) if item['kind'] in _FIRST_KINDS else len(_FIRST_KINDS)
        return (item.get('human_state') not in ws.CONFIRMED_BY, kind, item.get('applicability') == ws.PAST,
                item.get('basis_state') != ws.STATED)
    return sorted(items, key=key)


_ACCESS_FORM = re.compile(r"(?:접근\s*권한|사용자?|계정|권한)\s*(?:부여\s*)?신청서")

def build(db, task_id: int, card=None) -> Manual:
    task = db.task(task_id)
    if task is None or task_id not in {row['id'] for row in db.tasks()}:
        raise ValueError('현재 업무 목록에 없는 업무입니다')
    card = card or workcard.build(db, task_id)
    sections = [Section(key, title) for key, title in (
        ('overview', '업무 개요'), ('output', '만들어야 할 결과물·제출처'),
        ('timing', '일정·처리 시점'), ('prepare', '준비물·관련 서식'),
        ('procedure', '수행 절차'), ('cases', '건별 현황'), ('notes', '주의사항·담당자 메모'),
        ('past', '과거 수행 사례'), ('first', '처음 맡으면 할 일'),
        ('basis', '근거·빠진 정보'))]
    if any(item['kind'] == rc.EVALUATION for item in card.prepare):
        # 평가 업무에만 둔다. 다른 업무에 빈 구획을 늘어놓지 않는다.
        sections.insert(4, Section('evaluation', '평가 기준·배점'))
    by_key = {section.key: section for section in sections}
    mapping = {rc.PURPOSE: 'output', rc.OUTPUT: 'output', rc.AMOUNT: 'output',
               rc.RECIPIENT: 'output', rc.DEADLINE: 'timing', rc.RISK: 'notes',
               rc.NOTE: 'notes', rc.RECORD: 'past', rc.STATEMENT: 'overview',
               rc.EVALUATION: 'evaluation', rc.SCOPE: 'overview'}
    placed: dict[str, list[dict]] = {}
    for item in card.todo + card.prepare + card.notes + card.records + card.found:
        placed.setdefault(mapping.get(item['kind'], 'prepare'), []).append(item)
    for key, items in placed.items():
        section = by_key[key]
        # 긴 구획은 먼저 볼 것을 앞에 두고 나머지를 접는다. 지우지 않는다.
        if len(items) > _FOLD_OVER:
            ranked = _ranked(items)
            items, folded = ranked[:_SHOWN], ranked[_SHOWN:]
            section.more.extend(f"{workcard.item_label(item)}: {item['content']}" for item in folded)
        for item in items:
            section.lines.append(f"{workcard.item_label(item)}: {item['content']}\n{workcard.state_line(item)}")
            section.evidence.extend(item['evidence'])
    for key, steps in (('procedure', card.stated_steps), ('past', card.precedent_steps)):
        for index, step in enumerate(steps, 1):
            line = f"{index}. {step['label']} · {ws.procedure_label(step['procedure_kind'])}\n{workcard.state_line(step)}"
            for field_name, label in (('inputs', '입력'), ('forms', '서식'),
                                      ('output', '결과물'), ('done_criteria', '완료 기준')):
                if step.get(field_name):
                    line += f"\n{label}: {step[field_name]}"
            by_key[key].lines.append(line)
            by_key[key].evidence.extend(step.get('evidence', []))
    if card.matters:
        cases = by_key['cases']
        answered = sum(m['stage'] in (matters.ANSWERED, matters.DECIDED) for m in card.matters)
        summary = (f"우리 기관의 건 {len(card.matters)}건 · 회신이 이어진 것 {answered}건. "
                   f"진행은 목록·취합표·회신 칸에서 확인한 데까지만 적습니다. 제출·수용·시행은 공문으로 확인하세요.")
        if card.matter_others:
            summary += f" 같은 목록의 다른 기관 행 {card.matter_others:,}건은 참고 사례로만 셉니다."
        cases.lines.append(summary)
        for matter in card.matters[:_MAX_CASE_LINES]:
            cases.lines.append(f"{matters.describe(matter)}\n{matters.progress_line(matter)}")
            cases.evidence.extend(matter['evidence'][:2])
        if len(card.matters) > _MAX_CASE_LINES:
            cases.lines.append(f"외 {len(card.matters) - _MAX_CASE_LINES}건은 결과 초안의 건별 현황에 있습니다.")
    elif matters.waiting_for_locality(db, task_id):
        # 남의 건을 우리 건으로 만들지 않으려고 기관을 모르면 목록을 읽지 않는다. 그렇다고 말한다.
        by_key['cases'].lines.append(
            f"우리 기관을 정하지 못해 표 계산 문서 {matters.waiting_for_locality(db, task_id)}건의 건 목록을 "
            f"읽지 않았습니다. '건 확인·고치기'에서 우리 기관(시·군·구)을 정하면 읽습니다.")
    if card.conflicting_procedure:
        item = card.conflicting_procedure
        by_key['procedure'].lines.append('자료가 서로 다릅니다. 절차를 확정할 수 없습니다.\n' + item['content'])
        by_key['procedure'].evidence.extend(item['evidence'])
    by_key['past'].lines.insert(0, '과거 사례의 날짜·제출처·순서는 현재 회차에 적용되는지 별도로 확인하세요.')
    # 조건이 갖춰진 일과 먼저 확인할 일을 섞지 않는다. 과거 절차는 '과거 수행 사례'에 따로 있다.
    by_key['first'].lines.extend(
        f"{'지금 할 일' if action['type'] == 'do' else '먼저 확인할 일'}: {action['text']}"
        for action in card.actions)
    # 접근권한·사용 신청서는 사람 앞으로 나는 것이다. 담당자가 바뀌면 다시 내야 한다.
    forms = [row for row in db.con.execute(
        "SELECT d.id, d.filename FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "WHERE td.task_id = ? AND d.missing_since IS NULL ORDER BY d.filename", (task_id,))
        if _ACCESS_FORM.search(row['filename'] or '')]
    if forms:
        names = list(dict.fromkeys(row['filename'] for row in forms))[:4]
        by_key['first'].lines.append(
            '담당자가 바뀌면 본인 이름으로 다시 내야 하는 신청서가 있습니다: ' + ', '.join(names))
        by_key['first'].evidence.extend(
            {'doc_id': row['id'], 'locator': '', 'quote': row['filename']} for row in forms[:4])
    if card.changes:
        # 새 자료가 무엇을 바꿨는가. 지금 값만 보여 주면 바뀐 줄 모른다.
        by_key['basis'].lines.append(changes.summary(card.changes))
        by_key['basis'].lines.extend(changes.describe(change) for change in card.changes[:5])
        by_key['basis'].more.extend(changes.describe(change) for change in card.changes[5:])
    by_key['basis'].lines.extend(f"{gap.get('field', '확인 필요')}: {gap.get('detail', '')}" for gap in card.gaps)
    cycle = db.con.execute('SELECT * FROM task_cycles WHERE task_id=? ORDER BY id DESC LIMIT 1', (task_id,)).fetchone()
    work_instruction.improve(task, card, sections, cycle)
    for section in sections[:-1]:
        seen = set()
        unique = []
        for proof in section.evidence:
            key = (proof.get('doc_id'), proof.get('locator'), proof.get('quote'))
            if key not in seen:
                seen.add(key)
                unique.append(proof)
        section.evidence = unique
        by_key['basis'].evidence.extend(section.evidence)
        if not section.lines:
            section.lines.append({
                'cases': '진행 중인 개별 건은 아직 확인되지 않았습니다. 현재 취합표와 미회신·보완 목록을 확인하세요.',
                'notes': '별도로 확인된 주의사항은 없습니다. 변경된 지침과 담당자 메모를 확인해 보완하세요.',
                'past': '과거 수행 사례는 아직 확인되지 않았습니다. 지난 제출본과 발송 공문에서 처리 내역을 확인하세요.',
            }.get(section.key, '요청 공문·운영 지침에서 이 항목을 확인해 업무 안내에 보완하세요.'))
    if not by_key['basis'].lines:
        by_key['basis'].lines.append('자료에 적힌 내용과 현재 적용 여부·사람 확인 상태를 구분해 표시합니다.')
    work_brief.populate(task, card, sections, cycle,
                        summary=overview_summary.read(db, dict(task), card.found))
    return Manual(task_id, task['name'], sections)
