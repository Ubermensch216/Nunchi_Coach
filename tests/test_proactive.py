"""선제 감지·제안·브리핑 (행동형 업무 비서 계획서 3단계).

합성한 주기와 문서의 연·월로 감지를 돌린다. 평가 수치는 test_proactive_eval.py가 보고,
여기서는 약속 하나씩을 본다 — 거절을 기억하는가, 나중에가 돌아오는가, 과거 반복을 올해의
일정처럼 말하지 않는가, 올해 공식 문서가 들어오면 사전조사용 초안을 낡음으로 표시하는가.
"""

import json
import os
from datetime import date

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from app.core import cases, upcoming as upcoming_module
from app.db import Database
from app.proactive import briefing, events, gapcheck, recommend
from app.tools import action_eval, proactive_eval
from app.ui import theme
from app.ui.views import cycle_format
from app.ui.views.today import TodayView

TODAY = date(2026, 10, 10)
YEARLY = {"name": "연간 정기 평가", "expect": "fire", "why": "",
          "cycle": {"kind": "yearly", "months": "9,10,11", "years_observed": 2, "confidence": "medium"},
          "docs": [[2024, 9], [2024, 10], [2025, 9], [2025, 10], [2025, 11]]}


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet())
    return app


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "project.db")
    database.init()
    yield database
    database.close()


def seed(db, **changes):
    return proactive_eval.seed(db, {**YEARLY, **changes})


def recs(db, *states):
    return db.recommendations(states or None)


# ── 시간 신호 ───────────────────────────────────────────────────────

def test_screens_and_detection_share_one_judgement():
    """일정 화면의 ‘지금 챙길 일’과 감지가 같은 함수를 쓴다."""
    assert cycle_format.is_now is upcoming_module.is_now
    assert cycle_format.upcoming is upcoming_module.upcoming
    assert cycle_format.SOON_DAYS == upcoming_module.SOON_DAYS == 45


def test_a_recurring_task_gets_one_precedent_recommendation_with_last_years_documents(db):
    task_id = seed(db)
    summary = recommend.scan(db, TODAY)
    [rec] = recs(db)
    assert summary["new"] == 1 and rec["task_id"] == task_id
    assert (rec["certainty"], rec["state"], rec["target_year"], rec["plan_key"]) == (
        "precedent", "new", 2026, "recurring_prep")
    evidence = db.evidence_for("rec", [rec["id"]])[rec["id"]]
    assert evidence and all(e["current"] for e in evidence)
    assert {db.document(e["doc_id"])["eff_year"] for e in evidence} <= {2024, 2025}
    assert recommend.scan(db, TODAY)["new"] == 0 and len(recs(db)) == 1      # 다시 돌려도 늘지 않는다


def test_precedent_card_does_not_speak_of_this_years_schedule(db):
    seed(db)
    recommend.scan(db, TODAY)
    [card] = briefing.build(db, TODAY).cards
    text = " ".join([card.title, *card.why, briefing.CERTAINTY_LABEL[card.certainty]])
    assert card.certainty == "precedent" and "과거 반복업무 · 올해 일정 미확인" in text
    assert "아직 확인되지 않았습니다" in text and "2026-" not in text and "2026." not in text
    assert any("승인한 뒤에만" in line for line in card.actions) and card.evidence


# ── 거절·나중에·수락 ────────────────────────────────────────────────

def test_a_dismissed_recommendation_is_not_offered_again_that_year_but_is_next_year(db):
    seed(db)
    recommend.scan(db, TODAY)
    recommend.dismiss(db, recs(db)[0]["id"], "이미 했음")
    assert recommend.scan(db, TODAY)["new"] == 0
    assert [r["state"] for r in recs(db)] == ["dismissed"] and briefing.build(db, TODAY).empty
    recommend.scan(db, date(2027, 10, 10))
    assert sorted((r["target_year"], r["state"]) for r in recs(db)) == [(2026, "dismissed"), (2027, "new")]


def test_not_this_year_is_remembered_as_a_case(db):
    task_id = seed(db)
    recommend.scan(db, TODAY)
    recommend.dismiss(db, recs(db)[0]["id"], recommend.NOT_THIS_YEAR)
    assert db.cases(state="not_this_year")[0]["task_id"] == task_id
    recommend.scan(db, TODAY)
    assert [r["state"] for r in recs(db)] == ["dismissed"]


def test_snoozed_recommendation_returns_after_its_day(db):
    seed(db)
    recommend.scan(db, TODAY)
    recommend.snooze(db, recs(db)[0]["id"], TODAY)
    assert briefing.build(db, TODAY).cards == []
    recommend.scan(db, date(2026, 10, 16))
    assert briefing.build(db, TODAY).cards == []
    assert recommend.scan(db, date(2026, 10, 17))["woken"] == 1
    assert len(briefing.build(db, TODAY).cards) == 1


def test_accepting_starts_a_case_and_links_the_run(db):
    task_id = seed(db)
    recommend.scan(db, TODAY)
    rec_id = recs(db)[0]["id"]
    runner, run_id = recommend.accept(db, rec_id, TODAY)
    assert db.recommendation(rec_id)["state"] == "accepted"
    run = db.run(run_id)
    assert run["recommendation_id"] == rec_id and run["task_id"] == task_id
    assert cases.title(db.case(run["case_id"])) == "2026년 연간 정기 평가"
    assert recommend.scan(db, TODAY)["new"] == 0
    with pytest.raises(ValueError):
        recommend.accept(db, rec_id, TODAY)


def test_a_case_started_by_hand_expires_the_open_recommendation(db):
    task_id = seed(db)
    recommend.scan(db, TODAY)
    cases.open_case(db, task_id, 2026)
    assert recommend.scan(db, TODAY)["expired"] == 1
    rec = recs(db)[0]
    assert rec["state"] == "expired" and rec["dismiss_reason"] == "올해 수행 건이 이미 있음"


def test_old_recommendation_for_a_past_year_expires(db):
    seed(db)
    recommend.scan(db, TODAY)
    recommend.scan(db, date(2027, 2, 1))
    assert {(r["target_year"], r["state"]) for r in recs(db)} == {(2026, "expired")}


def test_at_most_five_new_cards_and_the_rest_are_counted(db):
    for number in range(7):
        seed(db, name=f"정기 평가 {number}")
    recommend.scan(db, TODAY)
    data = briefing.build(db, TODAY)
    assert len(data.cards) == 5 and data.hidden == 2 and data.tiles["new"] == 7


# ── 올해 공식 문서 (T12) ────────────────────────────────────────────

def official_document(db, task_id, year=2026, role="criteria", name="평가지표", mtime="h-official"):
    source_id = db.con.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
    doc_id = db.con.execute(
        "INSERT INTO documents(source_id, path, filename, ext, hash, eff_year, eff_month, eff_date, "
        "eff_date_kind, parse_status) VALUES (?, ?, ?, '.hwpx', ?, ?, 10, ?, 'body', 'ok')",
        (source_id, f"/합성/{year}_{name}.hwpx", f"{year}_{name}.hwpx", mtime, year, f"{year}-10-12")).lastrowid
    db.con.execute("INSERT INTO task_docs(task_id, doc_id) VALUES (?, ?)", (task_id, doc_id))
    db.con.execute("INSERT INTO doc_roles(doc_id, role, confidence) VALUES (?, ?, 'high')", (doc_id, role))
    db.con.execute("INSERT INTO doc_years(doc_id, kind, year) VALUES (?, 'evaluation', ?)", (doc_id, year))
    return doc_id


def test_official_document_replaces_the_precedent_recommendation(db):
    task_id = seed(db)
    recommend.scan(db, TODAY)
    precedent = recs(db)[0]["id"]
    official_document(db, task_id)
    summary = recommend.scan(db, TODAY)
    states = {r["rec_key"].split(":")[0]: r["state"] for r in recs(db)}
    assert summary["official"] == 1 and states == {"recurring_prep": "superseded", "official": "new"}
    [card] = briefing.build(db, TODAY).cards
    assert card.certainty == "official" and card.kind == "official" and card.rec_id != precedent
    assert briefing.CERTAINTY_LABEL["official"] == "올해 공식 문서 확인됨"


def test_official_document_alone_is_enough_even_out_of_season(db):
    task_id = seed(db, cycle={"kind": "yearly", "months": "3", "years_observed": 2, "confidence": "high"})
    assert recommend.scan(db, TODAY)["new"] == 0
    official_document(db, task_id, role="directive", name="시행계획 알림")
    assert recommend.scan(db, TODAY)["official"] == 1
    assert recommend.scan(db, TODAY)["official"] == 0                          # 같은 문서는 한 번만


def test_a_different_official_document_set_is_new_information_even_after_dismissal(db):
    task_id = seed(db)
    official_document(db, task_id)
    recommend.scan(db, TODAY)
    recommend.dismiss(db, recs(db)[0]["id"], "지금은 필요 없음")
    assert recommend.scan(db, TODAY)["official"] == 0
    official_document(db, task_id, role="directive", name="시행계획 알림", mtime="h-second")
    assert recommend.scan(db, TODAY)["official"] == 1


def test_documents_from_other_years_or_roles_are_not_official(db):
    task_id = seed(db)
    official_document(db, task_id, year=2025)
    official_document(db, task_id, role="result", name="결과 통보", mtime="h-result")
    assert events.official_documents(db, task_id, 2026) == []
    assert recommend.scan(db, TODAY)["official"] == 0


@pytest.fixture(scope="module")
def scenario(tmp_path_factory):
    """합성 시나리오로 분석하고 사전조사용 준비 작업까지 마친 프로젝트."""
    root = tmp_path_factory.mktemp("proactive-scenario")
    import json as _json

    corpus = _json.loads((action_eval.FIXTURES / "corpus.json").read_text(encoding="utf-8"))["scenarios"][0]
    source = root / "자료"
    action_eval.write_corpus(source, corpus["documents"])
    db, task_id, by_path = action_eval.analyse(root / "project", source, corpus, TODAY, corpus["documents"])
    yield db, task_id, by_path, corpus, source
    db.close()


def test_official_documents_arriving_late_mark_the_preliminary_drafts_stale(scenario):
    """설계서 T12 — 준비해 둔 초안은 낡음, 다시 만들자는 제안 하나, 다시 만들면 더는 알리지 않는다."""
    from app.actions import catalog
    from app.jobs import pipeline as pipeline_module

    db, task_id, by_path, corpus, source = scenario
    runner, run_id = catalog.prepare(db, task_id, 2026, today=TODAY)
    runner.execute(run_id)
    case_id = db.run(run_id)["case_id"]
    before = [a["id"] for a in db.artifacts(case_id=case_id)]
    assert before and {a["mode"] for a in db.artifacts(case_id=case_id)} == {"preliminary"}
    assert recommend.scan(db, TODAY)["refresh"] == 0                            # 아직 올해 문서가 없다

    action_eval.write_corpus(source, corpus["official_update"])
    worker = pipeline_module.Pipeline(db.path)
    for stage in (worker._scan, worker._hash, worker._parse, worker._date):
        stage(db)
    for row in db.con.execute("SELECT id, path FROM documents").fetchall():
        by_path.setdefault(relative_path(row["path"], source), row["id"])
    for document in corpus["official_update"]:
        db.con.execute("INSERT OR IGNORE INTO task_docs(task_id, doc_id) VALUES (?, ?)",
                       (task_id, by_path[document["path"]]))
    worker._roles(db)

    summary = recommend.scan(db, TODAY)
    assert summary["refresh"] == 1 and summary["stale"] == len(before)
    assert {db.artifact(i)["state"] for i in before} == {"stale"}
    [card] = briefing.build(db, TODAY).cards
    assert card.kind == "refresh" and "다시 만들어" in card.title and "낡음" in " ".join(card.why)
    assert recommend.scan(db, TODAY)["refresh"] == 0                            # 같은 알림을 두 번 하지 않는다

    rerun, new_run = catalog.prepare(db, task_id, 2026, today=TODAY)
    assert new_run != run_id and rerun.execute(new_run) == "waiting_for_approval"
    assert {a["mode"] for a in db.artifacts(run_id=new_run)} == {"official"}
    after = recommend.scan(db, TODAY)
    assert after["refresh"] == after["official"] == after["stale"] == 0


def relative_path(path, source):
    from pathlib import Path

    return Path(path).relative_to(source).as_posix()


# ── 빠진 단계 후보 ──────────────────────────────────────────────────

def test_gap_check_only_names_past_steps_inside_the_cycle_and_calls_them_precedent(tmp_path):
    from app.actions import catalog

    corpus = json.loads((action_eval.FIXTURES / "corpus.json").read_text(encoding="utf-8"))["scenarios"][0]
    source = tmp_path / "자료"
    action_eval.write_corpus(source, corpus["documents"])
    db, task_id, _ = action_eval.analyse(tmp_path / "project", source, corpus, TODAY, corpus["documents"])
    try:
        case_id = cases.open_case(db, task_id, 2026)
        case = db.case(case_id)
        cycle_months = set(upcoming_module.parse_months(db.task_cycle(task_id)["months"]))
        gaps = gapcheck.find(db, case, TODAY)
        assert gaps and all(g["month"] in cycle_months and g["month"] < TODAY.month for g in gaps)
        assert all("과거 관행" in g["why"] and g["prior_year"] == 2025 for g in gaps)
        assert gapcheck.find(db, case, date(2026, 1, 15)) == []                    # 아직 그 달이 오지 않았다
        # 체크 항목을 시작하면 그 단계는 더는 빠졌다고 하지 않는다.
        label = gaps[0]["label"]
        item = db.add_check_item(case_id, f"{label} 진행")
        db.set_check_state(item, "doing")
        assert label not in {g["label"] for g in gapcheck.find(db, case, TODAY)}
    finally:
        db.close()


def test_gap_check_stays_quiet_without_a_yearly_cycle(db):
    task_id = seed(db, cycle={"kind": "monthly", "months": "", "years_observed": 2, "confidence": "high"})
    case = db.case(cases.open_case(db, task_id, 2026))
    assert gapcheck.find(db, case, TODAY) == []


# ── 화면 ────────────────────────────────────────────────────────────

def buttons(view, text):
    return [b for b in view.findChildren(QPushButton) if b.text() == text]


def test_today_view_says_nothing_to_do_and_what_it_checked(db):
    view = TodayView(db, today=TODAY)
    assert view.data.empty
    from PySide6.QtWidgets import QLabel
    text = " ".join(label.text() for label in view.findChildren(QLabel))
    assert "지금 먼저 챙길 일이 없습니다" in text and "마지막으로 살핀 날" not in text


def test_today_view_shows_the_card_and_its_actions(db, monkeypatch):
    seed(db)
    recommend.scan(db, TODAY)
    view = TodayView(db, today=TODAY)
    opened = []
    monkeypatch.setattr(view, "_open_workbox", lambda task, year, run: opened.append((task, year, run)))
    from PySide6.QtWidgets import QLabel
    text = " ".join(label.text() for label in view.findChildren(QLabel))
    assert "과거 반복업무 · 올해 일정 미확인" in text and "받아들이면 하는 일" in text
    assert view.data.tiles == {"cases": 0, "new": 1, "due": 0, "completed": 0}
    buttons(view, "준비 작업 시작")[0].click()
    assert len(opened) == 1 and opened[0][1] == 2026 and opened[0][2] is not None
    assert recs(db)[0]["state"] == "accepted" and db.cases(state="open")


def test_today_view_snooze_and_decline_update_the_screen(db):
    seed(db, name="하나")
    seed(db, name="둘")
    recommend.scan(db, TODAY)
    view = TodayView(db, today=TODAY)
    changes = []
    view.changed.connect(lambda: changes.append(1))
    buttons(view, "나중에")[0].click()
    assert view.data.tiles["new"] == 1
    first = view.data.cards[0].rec_id
    view.dismiss(first, "이미 했음")
    assert view.data.empty and len(changes) == 2
