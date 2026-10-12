"""업무 안내와 매뉴얼이 함께 읽는 자료 기반 구조. 생성·수정 부작용이 없다."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import reconstruction as rc, workcard, workstate as ws


@dataclass
class Section:
    key: str
    title: str
    lines: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)


@dataclass
class Manual:
    task_id: int
    name: str
    sections: list[Section]


def build(db, task_id: int, card=None) -> Manual:
    task = db.task(task_id)
    if task is None or task_id not in {row['id'] for row in db.tasks()}:
        raise ValueError('현재 업무 목록에 없는 업무입니다')
    card = card or workcard.build(db, task_id)
    sections = [Section(key, title) for key, title in (
        ('overview', '업무 개요'), ('output', '목적·결과물'),
        ('timing', '시작 조건·시기'), ('prepare', '준비물·관련 서식'),
        ('procedure', '수행 절차'), ('notes', '주의사항·담당자 메모'),
        ('past', '과거 수행 사례'), ('first', '처음 맡으면 할 일'),
        ('basis', '근거·빠진 정보'))]
    by_key = {section.key: section for section in sections}
    by_key['overview'].lines.append(task['description'] or '업무 설명을 자료에서 찾지 못했습니다.')
    mapping = {rc.PURPOSE: 'output', rc.OUTPUT: 'output', rc.AMOUNT: 'output',
               rc.RECIPIENT: 'output', rc.DEADLINE: 'timing', rc.RISK: 'notes',
               rc.NOTE: 'notes', rc.RECORD: 'past', rc.STATEMENT: 'overview'}
    for item in card.todo + card.prepare + card.notes + card.records + card.found:
        section = by_key[mapping.get(item['kind'], 'prepare')]
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
    if card.conflicting_procedure:
        item = card.conflicting_procedure
        by_key['procedure'].lines.append('자료가 서로 다릅니다. 절차를 확정할 수 없습니다.\n' + item['content'])
        by_key['procedure'].evidence.extend(item['evidence'])
    by_key['past'].lines.insert(0, '과거 사례의 날짜·제출처·순서는 현재 회차에 적용되는지 별도로 확인하세요.')
    by_key['first'].lines.extend(action['text'] for action in card.actions)
    by_key['basis'].lines.extend(f"{gap.get('field', '확인 필요')}: {gap.get('detail', '')}" for gap in card.gaps)
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
            section.lines.append('자료에서 찾지 못했습니다. 확인·보완이 필요합니다.')
    if not by_key['basis'].lines:
        by_key['basis'].lines.append('자료에 적힌 내용과 현재 적용 여부·사람 확인 상태를 구분해 표시합니다.')
    return Manual(task_id, task['name'], sections)
