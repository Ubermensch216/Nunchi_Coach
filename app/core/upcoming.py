"""'다음에 언제 오는가'의 판정 — 화면과 선제 제안이 같은 것을 쓴다.

일정 화면의 '지금 챙길 일'과 업무 홈의 '지금 먼저 확인할 것', 오늘 화면의 제안이
같은 판정을 써야 한다. 한 화면에서는 급한데 다른 화면에서는 안 보이면 사용자는 어느
쪽을 믿어야 할지 모른다. 그래서 화면 모듈(ui/views/cycle_format.py)이 아니라 여기
한 곳에 둔다. 화면 모듈은 같은 이름으로 다시 내보낸다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .timeline import CycleGuess

# 이 안에 들어오면 '지금 챙길 일'로 올린다. 공직 업무는 한 달 전부터
# 준비 문서가 돌기 시작한다 — D-30을 넘겨 알려 주면 이미 늦다.
SOON_DAYS = 45


@dataclass(slots=True)
class Upcoming:
    """다가오는 반복 하나. 화면이 쓰기 좋은 모양으로 미리 계산해 둔다."""

    row: object
    when: date
    days_away: int
    running_now: bool

    @property
    def task_id(self) -> int:
        return self.row["task_id"]

    @property
    def name(self) -> str:
        return self.row["task_name"]


def upcoming(cycles: list, today: date) -> list[Upcoming]:
    """모든 주기를 '다음에 언제 오는가' 순으로 편다.

    매월 반복은 다음 시점이 따로 없다 — 지금이 곧 그때다. 그래서 항상
    '진행 중'으로 맨 앞에 둔다.
    """
    entries: list[Upcoming] = []
    for row in cycles:
        guess = guess_from_row(row)
        if guess.kind == "monthly":
            entries.append(Upcoming(row=row, when=today, days_away=0, running_now=True))
            continue

        when = guess.next_occurrence(today)
        if when is None:
            continue
        running = guess.applies_to_month(today.month)
        days = 0 if running else (when - today).days
        entries.append(
            Upcoming(row=row, when=today if running else when,
                     days_away=days, running_now=running)
        )

    entries.sort(key=lambda e: (not e.running_now, e.days_away, e.name))
    return entries


def is_now(entry: Upcoming) -> bool:
    """'지금 챙길 일'인가. 이 판정을 쓰는 화면이 셋이라 여기 하나만 둔다."""
    return entry.running_now or entry.days_away <= SOON_DAYS


def parse_months(raw: str | None) -> list[int]:
    if not raw:
        return []
    return [int(m) for m in raw.split(",") if m.strip()]


def guess_from_row(row) -> CycleGuess:
    """DB 행을 다시 CycleGuess로 만든다. next_occurrence 계산에 쓴다."""
    return CycleGuess(
        kind=row["kind"],
        months=parse_months(row["months"]),
        years_observed=row["years_observed"],
        confidence=row["confidence"],
        day_hint=row["day_hint"],
        evidence_doc_ids=[],
    )
