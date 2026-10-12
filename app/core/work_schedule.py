"""홈과 업무 상세가 공유하는 일정. 과거 기록을 올해 기한으로 바꾸지 않는다."""
from dataclasses import dataclass
from datetime import date
from . import schedule


@dataclass(frozen=True)
class Mark:
    task_id: int
    year: int | None
    month: int
    text: str
    kind: str
    case_id: int | None = None
    evidence: tuple = ()


def marks(db):
    result = []
    for entry in schedule.entries(db, keep_task_links=True):
        proof = ()
        if entry.doc_id:
            proof = tuple(e for e in db.evidence_for('item', [entry.item_id]).get(entry.item_id, []) if e.get('doc_id') == entry.doc_id)
        result.append(Mark(entry.task_id, entry.year, entry.month,
                           f"{entry.when or '날짜 미상'} {entry.what} · {entry.year or '연도 미상'} 기록",
                           'record', evidence=proof))
    active = {row['id'] for row in db.tasks()}
    for case in db.cases():
        if case['task_id'] not in active or case['state'] == 'not_this_year':
            continue
        if case['due_state'] not in ('official', 'user') or not case['due_date']:
            continue
        try:
            due = date.fromisoformat(case['due_date'])
        except ValueError:
            continue
        result.append(Mark(case['task_id'], due.year, due.month,
                           f"{due.day}일 {case['label'] or '수행 건'} · 확인된 기한",
                           'confirmed', case['id']))
    return result


def select(found, task_id=None, year=None, month=None):
    return [mark for mark in found if (task_id is None or mark.task_id == task_id)
            and (year is None or mark.year == year)
            and (month is None or mark.month == month)]
