"""주기 정보를 사람이 읽는 문장으로, 그리고 '다음에 언제 오는가'로 바꾼다.

업무 상세(tasks.py)·일정 화면(calendar.py)·업무 홈의 '지금 먼저 확인할 것'이
같은 표현과 **같은 판정**을 써야 한다. 일정 화면에서는 '지금 챙길 일'인데
업무 홈에서는 안 보이면, 사용자는 둘 중 어느 화면을 믿어야 할지 모른다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ...core import status
from ...core.timeline import CycleGuess
from ...core.upcoming import (  # noqa: F401  (화면 모듈이 같은 이름으로 쓴다)
    SOON_DAYS, Upcoming, guess_from_row, is_now, parse_months, upcoming,
)

CONFIDENCE_LABEL = {"high": "높음", "medium": "보통", "low": "낮음"}

def month_counts(cycles: list, today: date) -> dict[str, int]:
    """첫날 항목 '이번 달 업무' (계획서 §18).

    발령 첫날 가장 급한 것은 업무 일곱 개를 다 보는 일이 아니라, **지금
    돌아가고 있는 일**을 아는 것이다. 그래서 이번 달에 걸린 업무만 따로
    센다.

    '지금 걸렸나'의 판정(is_now)은 일정 화면·업무 홈과 같은 것을 쓴다.
    한 업무에 주기가 여럿일 수 있으므로 업무 단위로 접는다 — 주기 수로
    세면 주기를 많이 찾은 업무가 그만큼 무거워진다.
    """
    running: dict[int, bool] = {}
    for entry in upcoming(cycles, today):
        if not is_now(entry):
            continue
        confirmed = status.of_task({
            "review_state": entry.row["task_review_state"],
            "status": entry.row["task_status"],
            "origin": entry.row["task_origin"],
        }) == status.CONFIRMED
        running[entry.task_id] = running.get(entry.task_id, True) and confirmed
    return {
        "month_total": len(running),
        "month_done": sum(1 for done in running.values() if done),
    }


def cycle_headline(row) -> str:
    """'매년 9~11월', '매월 5~10일' 같은 한 줄 요약."""
    if row["kind"] == "monthly":
        text = "매월"
        if row["day_hint"]:
            text += f" {row['day_hint']}"
        return text

    months = parse_months(row["months"])
    text = f"매년 {_format_months(months)}"
    if row["day_hint"]:
        text += f" {row['day_hint']}"
    return text


def cycle_note(row) -> str:
    conf = CONFIDENCE_LABEL.get(row["confidence"], row["confidence"])
    return f"{row['years_observed']}년 확인 · 신뢰도 {conf}"


def next_occurrence_text(row, today: date | None = None) -> str | None:
    """'2026년 9월 (D-24)'. 매월 반복은 '항상 이번 달'이라 해당 없음."""
    guess = guess_from_row(row)
    today = today or date.today()
    next_date = guess.next_occurrence(today)
    if next_date is None:
        return None
    days = (date(next_date.year, next_date.month, 1) - today).days
    return f"{next_date.year}년 {next_date.month}월 (D-{days})"


def _format_months(months: list[int]) -> str:
    """연속된 달은 '9~11월'로, 떨어진 달은 '3월·12월'로 묶는다."""
    if not months:
        return ""
    months = sorted(months)
    ranges: list[tuple[int, int]] = []
    start = prev = months[0]
    for m in months[1:]:
        if m == prev + 1:
            prev = m
            continue
        ranges.append((start, prev))
        start = prev = m
    ranges.append((start, prev))
    parts = [f"{a}월" if a == b else f"{a}~{b}월" for a, b in ranges]
    return "·".join(parts)
