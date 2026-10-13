"""시간 기반 감지 — 과거 반복 주기가 다가왔는데 올해 수행 건이 없는 업무.

설계서 Ⅱ-1의 다섯 질문을 그대로 코드로 옮긴다.

  1. 현재 날짜가 과거 업무 준비 시점과 가까운가       core/upcoming.is_now (일정 화면과 같은 판정)
  2. 올해 이 업무의 수행 건이 이미 있는가            있으면 제안하지 않는다
  3. 올해 공식 평가계획·시행문이 등록돼 있는가        확실성(certainty)을 정한다
  4. 지난해 같은 시기의 문서가 있는가                 근거로 붙인다
  5. 같은 제안이 이미 있는가                          recommend가 판정한다

제안하지 않는 경우(‘내지 말아야 할 제안’)
  · 반복 주기가 연간이 아니다 — 월간·분기 반복에는 해마다의 준비가 없다
  · 주기의 신뢰도가 낮고 사람이 확정하지도 않았다
  · 마지막 활동이 오래전이다 — 이미 끝난 사업일 수 있다
  · 사람이 ‘반복 아님’으로 확정했다 (all_cycles가 이미 뺀다)
  · 파일 수정일로만 시점이 잡힌 묶음 (core/timeline이 격자에 넣지 않으므로 주기가 안 생긴다)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..core.upcoming import Upcoming, is_now, parse_months, upcoming

RECENT_YEARS = 2          # 마지막 활동이 이만큼 안에 있어야 ‘진행 중인 일’로 본다 (core/health.py와 같다)
MIN_CONFIDENCE = ("high", "medium")


@dataclass(frozen=True)
class Candidate:
    task_id: int
    task_name: str
    target_year: int
    months: list[int]
    days_away: int
    running_now: bool
    years_observed: int
    confidence: str
    last_year: int | None

    def key(self) -> str:
        return f"time:{self.task_id}:{self.target_year}"


def last_active_year(db, task_id: int) -> int | None:
    """신뢰할 수 있는 시점으로 읽은 마지막 연도. 파일 수정일뿐인 시점은 뺀다."""
    row = db.con.execute(
        "SELECT MAX(d.eff_year) FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "WHERE td.task_id = ? AND d.missing_since IS NULL AND d.eff_year IS NOT NULL "
        "AND d.eff_date_kind != 'fs'", (task_id,)).fetchone()
    return row[0] if row else None


def candidates(db, today: date) -> list[Candidate]:
    out: list[Candidate] = []
    cycles = [row for row in db.all_cycles() if row["kind"] == "yearly"]
    for entry in upcoming(cycles, today):
        if not is_now(entry):
            continue
        row = entry.row
        # 확신이 낮아도 받은 공문에 날까지 적힌 기한이 있으면 알린다(종합평가 1월 10일).
        dated = row["confidence"] == "low" and (row["day_hint"] or "").endswith("일까지")
        if row["confidence"] not in MIN_CONFIDENCE and row["decided_by"] != "user" and not dated:
            continue
        last = last_active_year(db, entry.task_id)
        if last is None or last < today.year - RECENT_YEARS:
            continue
        out.append(Candidate(
            task_id=entry.task_id, task_name=entry.name, target_year=entry.when.year,
            months=parse_months(row["months"]), days_away=entry.days_away,
            running_now=entry.running_now, years_observed=row["years_observed"],
            confidence=row["confidence"], last_year=last))
    return out
