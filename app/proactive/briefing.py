"""‘오늘’ 화면이 쓰는 묶음 — 지금 챙길 제안, 진행 중인 수행 건, 빠진 단계 후보 (계획서 U3).

화면은 상태 어휘를 해석하지 않는다(core/status.py와 같은 규칙). 제안마다 **왜 추천했는지,
어떤 문서를 근거로 했는지, 무엇을 실행할 수 있는지**를 여기서 문장으로 만들어 준다.
세 가지가 없는 제안은 보이지 않는다.

문구의 규칙
  · 과거 반복에서 나온 제안은 ‘과거 반복업무 · 올해 일정 미확인’이다. 날짜를 올해의 일정처럼
    쓰지 않는다.
  · ‘올해 공식 문서 확인됨’은 평가 연도가 올해로 적힌 시행문·지표가 실제로 있을 때만 쓴다.
  · 새 제안은 한 번에 다섯 개까지 보이고 나머지는 건수로만 알린다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..actions import catalog
from ..actions.policy import RISK_LABEL
from ..actions.registry import REGISTRY
from ..core import cases
from ..core.upcoming import parse_months
from . import gapcheck
from .recommend import OPEN

MAX_NEW = 5
DUE_WINDOW_DAYS = 30
CERTAINTY_LABEL = {
    "official": "올해 공식 문서 확인됨",
    "precedent": "과거 반복업무 · 올해 일정 미확인",
    "suggested": "추천",
}
RISK_ORDER = ("read", "draft", "change")


@dataclass
class Card:
    rec_id: int
    task_id: int
    task_name: str
    year: int
    certainty: str
    kind: str                         # prepare | official | refresh
    state: str
    title: str
    why: list[str]
    evidence: list[dict]              # {doc_id, filename}
    actions: list[str]
    days_away: int | None = None


@dataclass
class CaseRow:
    case_id: int
    task_id: int
    title: str
    basis: str
    due: str
    due_soon: bool
    done: int
    total: int
    waiting: bool
    run_state: str


@dataclass
class Briefing:
    scanned_on: str | None
    tiles: dict[str, int]
    cards: list[Card] = field(default_factory=list)
    hidden: int = 0
    cases: list[CaseRow] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.cards or self.cases or self.gaps or self.hidden)


def _months_text(months: list[int]) -> str:
    return "·".join(f"{m}월" for m in sorted(months))


def _what_it_does() -> list[str]:
    plan = catalog.PLANS.get(catalog.RECURRING_PREP)
    by_risk: dict[str, list[str]] = {}
    for step in plan.steps:
        tool = REGISTRY.get(step.tool)
        by_risk.setdefault(tool.risk, []).append(tool.label or tool.name)
    return [f"{RISK_LABEL[risk]}: {', '.join(by_risk[risk])}" + (" — 승인한 뒤에만" if risk == "change" else " — 자동")
            for risk in RISK_ORDER if risk in by_risk]


def card_for(db, rec) -> Card:
    detail = json.loads(rec["signal_detail"] or "{}")
    kind = rec["rec_key"].split(":", 1)[0]
    kind = "prepare" if kind == "recurring_prep" else kind
    year, name = rec["target_year"], rec["task_name"]
    why: list[str] = []
    if kind == "prepare":
        title = f"{name} 준비를 제안합니다"
        months = detail.get("months") or []
        why.append(f"과거 {detail.get('years_observed', '?')}개 연도의 {_months_text(months)}에 반복된 업무입니다 "
                   f"(신뢰도 {cases_confidence(detail.get('confidence'))}).")
        why.append(f"{year}년 평가 기준과 일정은 아직 확인되지 않았습니다. 지금은 과거 자료를 참고한 사전조사용으로 준비합니다.")
    elif kind == "official":
        title = f"{name}: 올해 공식 문서가 등록되었습니다"
        why.append(f"평가 연도가 {year}년으로 적힌 시행문·평가지표가 업무에 묶여 있습니다. 과거와 달라진 점을 확인하며 준비할 수 있습니다.")
    else:
        title = f"{name}: 준비해 둔 초안을 다시 만들어야 합니다"
        why.append(f"{year}년 공식 문서가 들어와, 그 전에 만들어 둔 서식·체크리스트는 올해 기준이 아닌 것이 되었습니다. "
                   "다시 만들어 변경점을 확인하세요. 이전 초안은 ‘낡음’으로 표시되고 파일은 남습니다.")
    docs = [{"doc_id": e["doc_id"], "filename": e["filename"]}
            for e in db.evidence_for("rec", [rec["id"]]).get(rec["id"], []) if e["doc_id"]]
    return Card(rec["id"], rec["task_id"], name, year, rec["certainty"], kind, rec["state"], title, why,
                docs, _what_it_does(), detail.get("days_away"))


def cases_confidence(value: str | None) -> str:
    return {"high": "높음", "medium": "보통", "low": "낮음"}.get(value or "", "알 수 없음")


def case_rows(db, today: date) -> list[CaseRow]:
    rows = []
    for case in db.cases(state="open"):
        items = db.checklist(case["id"])
        runs = db.runs(case_id=case["id"])
        due_soon = bool(case["due_date"] and case["due_state"] in ("official", "user")
                        and date.fromisoformat(case["due_date"]) <= today + timedelta(days=DUE_WINDOW_DAYS))
        rows.append(CaseRow(
            case["id"], case["task_id"], cases.title(case), cases.BASIS_LABEL[case["basis_state"]],
            cases.due_text(case), due_soon, sum(1 for i in items if i["state"] == "done"), len(items),
            any(r["state"] == "waiting_for_approval" for r in runs),
            runs[0]["state"] if runs else ""))
    return rows


def build(db, today: date | None = None) -> Briefing:
    today = today or date.today()
    recs = db.recommendations(OPEN)
    recs = [r for r in recs if r["state"] != "snoozed"]
    cards = sorted((card_for(db, r) for r in recs),
                   key=lambda c: (c.certainty != "official", c.days_away if c.days_away is not None else 0, c.rec_id))
    rows = case_rows(db, today)
    gaps = []
    for case in db.cases(state="open"):
        gaps += [{**gap, "case_id": case["id"], "case_title": cases.title(case)}
                 for gap in gapcheck.find(db, case, today)]
    completed = db.con.execute("SELECT COUNT(*) FROM action_runs WHERE state = 'completed'").fetchone()[0]
    return Briefing(
        scanned_on=db.get_meta("proactive_scanned"),
        tiles={"cases": len(rows), "new": len(cards), "due": sum(1 for r in rows if r.due_soon),
               "completed": completed},
        cards=cards[:MAX_NEW], hidden=max(0, len(cards) - MAX_NEW), cases=rows, gaps=gaps)
