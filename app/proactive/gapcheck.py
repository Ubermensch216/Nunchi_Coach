"""눈치점검 — 올해 수행을 과거 처리 순서와 대조해 빠진 단계 후보를 알린다 (계획서 S6).

과거 처리 순서(task_steps)에서 **이 업무의 반복 시기 안에 있었고 이미 지난 달**의 단계 가운데
올해 문서에도, 올해 체크 항목에도 대응하는 것이 없으면 ‘빠진 단계 후보’다.

후보일 뿐이다. 과거에 했던 단계가 올해도 필요한지는 공식 문서만 정한다. 그래서 문구를
‘과거 관행’으로 쓰고 ‘누락’이라고 단정하지 않는다. 올해 근거 문서가 공식으로 확인된 단계는
이 검사가 아니라 체크리스트의 ‘확인된 필수 작업’이 맡는다.
"""

from __future__ import annotations

from datetime import date

from ..core.labeling import FALLBACK_LABEL, label_document
from ..core.upcoming import parse_months

DONE_STATES = ("doing", "review", "done")


def prior_year(db, task_id: int, year: int) -> int | None:
    row = db.con.execute(
        "SELECT MAX(year) FROM task_steps WHERE task_id = ? AND year < ?", (task_id, year)).fetchone()
    return row[0] if row else None


def find(db, case, today: date) -> list[dict]:
    task_id, year = case["task_id"], case["period_year"]
    before = prior_year(db, task_id, year)
    cycle = db.task_cycle(task_id)
    if before is None or cycle is None or cycle["kind"] != "yearly":
        return []
    months = set(parse_months(cycle["months"]))
    this_year = {label_document(row["filename"]) for row in db.con.execute(
        "SELECT d.filename FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "WHERE td.task_id = ? AND d.eff_year = ? AND d.eff_date_kind != 'fs' AND d.missing_since IS NULL",
        (task_id, year))}
    titles = [row["title"] for row in db.con.execute(
        "SELECT title FROM checklist_items WHERE case_id = ? AND state IN (%s)"
        % ", ".join("?" * len(DONE_STATES)), (case["id"], *DONE_STATES))]

    gaps, seen = [], set()
    for step in db.task_steps(task_id, before):
        label, month = step["label"], step["month"]
        if label == FALLBACK_LABEL or month is None or month not in months:
            continue
        passed = (year, month) < (today.year, today.month) if year == today.year else year < today.year
        if not passed or label in seen:
            continue
        seen.add(label)
        if label in this_year or any(label in title for title in titles):
            continue
        gaps.append({"label": label, "month": month, "prior_year": before, "doc_id": step["doc_id"],
                     "why": f"{before}년 {month}월에는 ‘{label}’ 단계가 있었으나 {year}년 자료와 체크 항목에서는 "
                            "보이지 않습니다. 과거 관행이며 올해도 필요한지는 공식 문서로 확인하세요."})
    return sorted(gaps, key=lambda gap: gap["month"])
