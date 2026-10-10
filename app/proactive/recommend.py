"""신호 → 제안. 거절·나중에·중복·만료를 다룬다 (계획서 S4).

제안의 지문(rec_key)
    recurring_prep:{업무}:{연도}            과거 반복에서 나온 준비 제안 (시간 신호)
    official:{업무}:{연도}:{문서 집합}      올해 공식 문서가 들어와 준비를 시작하자는 제안
    refresh:{업무}:{연도}:{문서 집합}       준비해 둔 초안의 기준이 달라져 다시 만들자는 제안
    collection_request:{업무}:{연도}:{수행 건}  준비 작업이 끝났으니 부서에 보낼 요청문 초안을 만들자는 제안

같은 지문의 제안은 한 번만 만든다. 거절한 제안은 같은 해에 다시 내지 않는다 — 다만 올해
공식 문서가 새로 들어온 것처럼 **새 사실**이 생기면 지문이 달라져 다시 낼 수 있다.
‘나중에’는 정한 날 뒤에 다시 나타난다. 수행 건이 이미 있으면 제안은 만료된다.
"""

from __future__ import annotations

from datetime import date, timedelta

from ..core import cases
from . import events, gapcheck, recurrence   # noqa: F401  (gapcheck: 브리핑이 쓴다)

PLAN_KEY = "recurring_prep"
REQUEST_PLAN = "collection_request"
SNOOZE_DAYS = 7
EVIDENCE_DOCS = 3
OPEN = ("new", "seen", "snoozed")
DISMISS_REASONS = ("이미 했음", "올해는 하지 않음", "다른 방식으로 함", "지금은 필요 없음")
NOT_THIS_YEAR = "올해는 하지 않음"


def scan(db, today: date | None = None) -> dict[str, int]:
    """감지를 한 번 돌린다. 만든·바꾼 제안의 건수를 돌려준다."""
    today = today or date.today()
    summary = {"new": 0, "official": 0, "refresh": 0, "stale": 0, "woken": 0, "expired": 0, "next": 0}
    with db.transaction():
        summary["woken"] = _wake(db, today)
        for candidate in recurrence.candidates(db, today):
            summary["new"] += _time(db, candidate)
        for event in events.detect(db, today):
            _event(db, event, summary)
        summary["next"] = _next_steps(db)
        summary["expired"] = _expire(db, today)
    db.set_meta("proactive_scanned", today.isoformat())
    return summary


def case_of(db, task_id: int, year: int):
    return db.con.execute(
        "SELECT * FROM work_cases WHERE task_id = ? AND period_year = ? ORDER BY id LIMIT 1",
        (task_id, year)).fetchone()


# ── 시간 신호 ───────────────────────────────────────────────────────

def _time(db, candidate: recurrence.Candidate) -> int:
    if case_of(db, candidate.task_id, candidate.target_year) is not None:
        return 0
    if events.official_documents(db, candidate.task_id, candidate.target_year):
        return 0                      # 올해 공식 문서가 있으면 이벤트 쪽 제안이 대신한다
    rec_key = f"{PLAN_KEY}:{candidate.task_id}:{candidate.target_year}"
    if db.recommendation_by_key(rec_key) is not None:
        return 0                      # 있으면 상태가 무엇이든 다시 만들지 않는다 (거절 기억 포함)
    signal_id, _ = db.add_signal("time", candidate.task_id, candidate.key(), {
        "months": candidate.months, "days_away": candidate.days_away, "running": candidate.running_now,
        "years_observed": candidate.years_observed, "confidence": candidate.confidence,
        "last_year": candidate.last_year})
    rec_id = db.add_recommendation(signal_id, candidate.task_id, candidate.target_year, PLAN_KEY,
                                   rec_key, "precedent")
    db.replace_recommendation_evidence(rec_id, _last_year_documents(db, candidate))
    return 1


def _last_year_documents(db, candidate: recurrence.Candidate) -> list[dict]:
    """지난해 같은 시기의 문서 — 제안의 근거. 업무가 가장 최근에 그 달에 남긴 것부터."""
    found, seen_ids = [], set()
    for month in sorted(candidate.months):
        for row in db.task_documents_in_month(candidate.task_id, month, limit=EVIDENCE_DOCS):
            if row["id"] not in seen_ids and row["eff_year"] < candidate.target_year:
                seen_ids.add(row["id"])
                found.append({"doc_id": row["id"], "locator": row["eff_date"] or "", "quote": ""})
    return found[:EVIDENCE_DOCS]


# ── 이벤트 신호 ─────────────────────────────────────────────────────

def _event(db, event: events.Event, summary: dict) -> None:
    case = case_of(db, event.task_id, event.year)
    if case is not None:
        if case["state"] != "open" or events.up_to_date(db, case["id"], event):
            return
        if not db.artifacts(case_id=case["id"]):
            return                    # 수행 건만 열었고 만든 것이 아직 없다 — 이미 하고 있는 일이다
    kind = "refresh" if case is not None else "official"
    rec_key = f"{kind}:{event.task_id}:{event.year}:{event.fingerprint()}"
    if db.recommendation_by_key(rec_key) is not None:
        return
    if kind == "refresh":
        summary["stale"] += db.mark_artifacts_stale(case["id"], ("preliminary", "official"))
    signal_id, _ = db.add_signal("event", event.task_id, f"event:{rec_key}", {
        "year": event.year, "documents": list(event.doc_ids), "case": case["id"] if case else None})
    for old in db.recommendations(OPEN):                          # 이전 제안은 새 사실로 대체된다
        if old["task_id"] == event.task_id and old["target_year"] == event.year:
            db.set_recommendation_state(old["id"], "superseded", reason="올해 공식 문서가 새로 들어옴")
    rec_id = db.add_recommendation(signal_id, event.task_id, event.year, PLAN_KEY, rec_key, "official")
    db.replace_recommendation_evidence(
        rec_id, [{"doc_id": doc, "locator": "", "quote": ""} for doc in event.doc_ids[:EVIDENCE_DOCS]])
    summary[kind] += 1


# ── 다음 단계 ───────────────────────────────────────────────────────

def _next_steps(db) -> int:
    """준비 작업을 마친 수행 건에 후속 작업(요청문 초안)을 제안한다 — 설계서 D7.

    조건: 준비 작업이 끝났고(체크 항목까지 올림), 낡지 않은 취합 서식이 있고, 아직 요청문
    작업을 한 적이 없다. 제안일 뿐이며 발송은 이 프로그램이 하지 않는다.
    """
    made = 0
    for case in db.cases(state="open"):
        runs = db.runs(case_id=case["id"])
        done = any(r["plan_key"] == PLAN_KEY and r["state"] == "completed" for r in runs)
        if not done or any(r["plan_key"] == REQUEST_PLAN for r in runs):
            continue
        if not any(a["kind"] == "collection_form" and a["state"] != "stale"
                   for a in db.artifacts(case_id=case["id"])):
            continue
        rec_key = f"{REQUEST_PLAN}:{case['task_id']}:{case['period_year']}:{case['id']}"
        if db.recommendation_by_key(rec_key) is not None:
            continue
        signal_id, _ = db.add_signal("event", case["task_id"], f"event:{rec_key}",
                                     {"case": case["id"], "after": PLAN_KEY})
        db.add_recommendation(signal_id, case["task_id"], case["period_year"], REQUEST_PLAN, rec_key, "suggested")
        made += 1
    return made


# ── 상태 정리 ───────────────────────────────────────────────────────

def _wake(db, today: date) -> int:
    woken = 0
    for rec in db.recommendations(("snoozed",)):
        if rec["snooze_until"] and rec["snooze_until"] <= today.isoformat():
            db.set_recommendation_state(rec["id"], "new")
            woken += 1
    return woken


def _expire(db, today: date) -> int:
    """수행 건이 생겼거나, 업무가 사라졌거나, 때가 지난 제안을 거둔다."""
    expired = 0
    live = {task["id"] for task in db.tasks()}
    for rec in db.recommendations(OPEN):
        case = case_of(db, rec["task_id"], rec["target_year"])
        refresh = rec["rec_key"].startswith("refresh:")
        reason = None
        if rec["task_id"] not in live:
            reason = "업무가 없어졌거나 합쳐짐"
        elif rec["rec_key"].startswith(f"{REQUEST_PLAN}:"):      # 후속 제안도 수행 건 위에서 성립한다
            if case is None or case["state"] != "open":
                reason = "수행 건이 닫혔거나 없음"
            elif any(r["plan_key"] == REQUEST_PLAN for r in db.runs(case_id=case["id"])):
                reason = "요청문 작업을 이미 시작함"
        elif refresh:                 # 다시 만들자는 제안은 수행 건이 있어야 성립한다
            if case is None or case["state"] != "open":
                reason = "수행 건이 닫혔거나 없음"
            else:
                official = events.official_documents(db, rec["task_id"], rec["target_year"])
                if official and events.up_to_date(db, case["id"], events.Event(
                        rec["task_id"], rec["target_year"], tuple(official))):
                    reason = "다시 만들어 최신이 됨"
        elif case is not None:
            reason = "올해 수행 건이 이미 있음"
        elif rec["target_year"] < today.year:
            reason = "해가 지남"
        if reason:
            db.set_recommendation_state(rec["id"], "expired", reason=reason)
            expired += 1
    return expired


# ── 사용자의 응답 ───────────────────────────────────────────────────

def seen(db, rec_id: int) -> None:
    rec = db.recommendation(rec_id)
    if rec is not None and rec["state"] == "new":
        db.set_recommendation_state(rec_id, "seen")


def accept(db, rec_id: int, today: date | None = None):
    """제안을 받아들여 준비 작업을 시작한다. (실행기, 실행 id). 실행은 부르는 쪽이 돌린다."""
    from ..actions import catalog

    rec = db.recommendation(rec_id)
    if rec is None or rec["state"] not in OPEN:
        raise ValueError("받아들일 수 없는 제안입니다.")
    if rec["plan_key"] == REQUEST_PLAN:
        case = case_of(db, rec["task_id"], rec["target_year"])
        runner, run_id = catalog.prepare_request(db, case["id"], today=today, recommendation_id=rec_id)
    else:
        runner, run_id = catalog.prepare(db, rec["task_id"], rec["target_year"], today=today,
                                         recommendation_id=rec_id)
    db.set_recommendation_state(rec_id, "accepted")
    return runner, run_id


def dismiss(db, rec_id: int, reason: str = "") -> None:
    rec = db.recommendation(rec_id)
    if rec is None or rec["state"] not in OPEN:
        raise ValueError("거절할 수 없는 제안입니다.")
    with db.transaction():
        db.set_recommendation_state(rec_id, "dismissed", reason=" ".join(reason.split())[:80] or None)
        if reason == NOT_THIS_YEAR:     # 올해는 없다는 판단을 수행 건에 남긴다 — 다시 묻지 않는다
            cases.close(db, cases.open_case(db, rec["task_id"], rec["target_year"]), not_this_year=True)


def snooze(db, rec_id: int, today: date | None = None, days: int = SNOOZE_DAYS) -> None:
    rec = db.recommendation(rec_id)
    if rec is None or rec["state"] not in OPEN:
        raise ValueError("미룰 수 없는 제안입니다.")
    until = (today or date.today()) + timedelta(days=days)
    db.set_recommendation_state(rec_id, "snoozed", snooze_until=until.isoformat())
