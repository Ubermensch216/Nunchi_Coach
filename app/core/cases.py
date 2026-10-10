"""수행 건 — 올해 실제로 하는 한 건 (행동형 업무 비서 계획서 F3).

업무(tasks)는 '어떤 일인가'이고 수행 건은 '올해의 그 일'이다. 같은 업무의
2025년 절차와 2026년 수행을 가르는 단위가 없으면, 지난해에 끝낸 단계가 올해도
끝난 것으로 보인다.

지키는 것
  · 과거는 올해의 의무가 아니다. 기한과 근거 문서는 그 해의 공식 문서(official)나
    사람의 확인(user)에서만 받는다. 과거 사례에서 짐작한 날짜를 넣는 길이 없다.
  · 같은 업무·연도에 이름이 같은 건은 하나다. 다시 열면 있던 것이 열린다.
  · '올해는 없음'도 기록이다. 지우지 않는다 — 같은 제안을 다시 내지 않으려면
    그 판단이 남아 있어야 한다.
"""

from __future__ import annotations

from datetime import date

from ..db import taskedit

OPEN, CLOSED, NOT_THIS_YEAR = "open", "closed", "not_this_year"
OFFICIAL, USER = "official", "user"

STATE_LABEL = {OPEN: "진행 중", CLOSED: "마침", NOT_THIS_YEAR: "올해는 없음"}
BASIS_LABEL = {"unconfirmed": "올해 공식 문서 미확인", OFFICIAL: "올해 공식 문서 확인",
               USER: "담당자 확인"}
DUE_LABEL = {"unknown": "기한 확인 필요", OFFICIAL: "공식 기한", USER: "담당자가 정한 기한"}


def open_case(db, task_id: int, period_year: int, label: str = "") -> int:
    if not taskedit.live(db, task_id):
        raise ValueError("없거나 합쳐진 업무에는 수행 건을 열 수 없습니다.")
    case_id, _ = db.open_case(task_id, int(period_year), label)
    return case_id


def title(row) -> str:
    """화면과 산출물에 쓰는 이름. 예: '2026년 지방규제혁신 평가'."""
    name = f"{row['period_year']}년 {row['task_name']}"
    return f"{name} — {row['label']}" if row["label"] else name


def set_due(db, case_id: int, due: date | str | None, source: str = USER) -> None:
    if due is None or due == "":
        db.set_case_due(case_id, None)
        return
    value = due if isinstance(due, date) else date.fromisoformat(str(due))
    db.set_case_due(case_id, value.isoformat(), source)


def set_basis(db, case_id: int, doc_id: int | None, source: str = USER) -> None:
    if doc_id is not None and db.document(doc_id) is None:
        raise ValueError("없는 문서입니다.")
    db.set_case_basis(case_id, doc_id, source)


def close(db, case_id: int, not_this_year: bool = False) -> None:
    db.set_case_state(case_id, NOT_THIS_YEAR if not_this_year else CLOSED)


def reopen(db, case_id: int) -> None:
    db.set_case_state(case_id, OPEN)


def due_text(row) -> str:
    """기한 한 줄. 확인된 날짜가 없으면 날짜를 쓰지 않는다."""
    if row["due_date"] and row["due_state"] in (OFFICIAL, USER):
        return f"{row['due_date']} ({DUE_LABEL[row['due_state']]})"
    return DUE_LABEL["unknown"]
