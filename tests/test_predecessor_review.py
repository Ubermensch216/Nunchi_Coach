"""전임자의 선택적 보충·확인과 검토 인계본 (C07).

완료 증거: 전임자가 참여하면 품질이 보강되고, 응답이 없어도 자료 기반 경로가
그대로 유지된다.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QPushButton  # noqa: E402

from app.core import continuity, handover_release as release  # noqa: E402
from app.core import reconstruction as rc, workcard, workstate as ws  # noqa: E402
from app.db import Database  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.views import tasks as tasks_view  # noqa: E402
from app.ui.views.tasks import TasksView  # noqa: E402

BASIS = date(2025, 9, 10)
REQUEST = "절차: 집계 자료 준비 → 실적표 작성 → 검토 후 제출\n제출처: 기획예산과"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet())
    yield app


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "project" / "project.db")
    database.init()
    database.con.execute("INSERT INTO tasks(id, name) VALUES (1, '정기 실적보고')")
    root = tmp_path / "자료"
    root.mkdir()
    source = database.add_source(root)
    path = root / "2025_실적보고_요청.hwpx"
    path.write_text(REQUEST, encoding="utf-8")
    doc = database.upsert_document(source, {
        "path": str(path), "filename": path.name, "ext": ".hwpx", "parse_status": "ok",
        "hash": "h1", "char_count": len(REQUEST), "eff_date": "2025-09-01", "eff_year": 2025,
        "eff_month": 9, "eff_precision": "day", "eff_date_kind": "body"})
    database.replace_sections(doc, [("paragraph", 1, "1문단", REQUEST)])
    database.index_document(doc, path.name, str(path), "", REQUEST)
    database.assign_document(1, doc)
    rc.restore(database, BASIS)
    yield database
    database.close()


@pytest.fixture
def view(db, qapp):
    widget = TasksView(db)
    widget.open_task(1)
    yield widget
    widget.close()
    widget.deleteLater()


def page(view):
    return view.pages[tasks_view.WORK]


def texts(widget) -> list[str]:
    return [label.text() for label in widget.findChildren(QLabel) if label.text()]


def gap_keys(db):
    return {gap["gap_key"] for gap in db.info_gaps(1)}


# ── 전임자가 없어도 그대로 된다 ─────────────────────────────────────

def test_default_reviewer_is_an_unnamed_successor_and_nothing_waits_for_a_predecessor(db):
    assert db.reviewer() == ("", ws.SUCCESSOR)
    assert db.work_context()["mode"] == ws.MODE_MATERIALS
    card = workcard.build(db, 1)
    assert card.actions[0]["type"] == "do" and not card.empty
    assert release.save(db)[1]
    with pytest.raises(ValueError, match="전임자가 참여하지 않았다면"):
        release.publish(db)
    assert db.snapshots(ws.SNAPSHOT_HANDOVER) == []


def test_predecessor_joining_and_never_answering_changes_nothing(db):
    step = workcard.build(db, 1).stated_steps[0]
    db.set_step_run(step["id"], ws.DONE, note="후임자가 먼저 취합")
    before = [(i["id"], i["content"], i["human_state"]) for i in db.task_items(1)]

    db.set_reviewer("김전임", ws.PREDECESSOR)
    context = db.work_context()
    assert (context["mode"], context["predecessor"]) == (ws.MODE_WITH_PREDECESSOR, "김전임")
    # 참여 방식만 바뀌었다. 복원 결과와 후임자의 수행 기록은 그대로다.
    assert [(i["id"], i["content"], i["human_state"]) for i in db.task_items(1)] == before
    assert workcard.build(db, 1).stated_steps[0]["run_state"] == ws.DONE
    rc.restore(db, BASIS)
    assert workcard.build(db, 1).stated_steps[0]["run_note"] == "후임자가 먼저 취합"
    assert workcard.build(db, 1).actions[0]["type"] == "do"
    # 같은 사람으로 다시 지정해도 맥락이 불어나지 않는다.
    db.set_reviewer("김전임", ws.PREDECESSOR)
    assert db.con.execute("SELECT COUNT(*) FROM work_contexts").fetchone()[0] == 2


# ── 전임자가 참여하면 보강된다 ──────────────────────────────────────

def test_predecessor_supplies_the_missing_deadline_and_the_gap_closes(view):
    db = view.db
    assert "missing:deadline" in gap_keys(db)
    assert workcard.build(db, 1).stated_steps[-1]["blocked"]

    view._set_reviewer("김전임", ws.PREDECESSOR)
    item_id = view._add_work_item(1, rc.DEADLINE, "2025-09-30 (매년 9월 말, 공문 없이 관행)")
    item = next(i for i in db.task_items(1) if i["id"] == item_id)
    # 문서에는 없고 전임자가 말한 것이다 — 그렇게 남는다.
    assert (item["origin"], item["basis_state"], item["human_state"]) == (
        "user", ws.MISSING, ws.PREDECESSOR)
    assert workcard.state_line(item) == "자료 없음 · 적용 여부 미상 · 전임자 확인"
    assert "missing:deadline" not in gap_keys(db)
    assert not workcard.build(db, 1).stated_steps[-1]["blocked"]
    review, = db.reviews("item", item_id)
    assert (review["action"], review["reviewer_role"], review["reviewer_name"]) == (
        "supplement", ws.PREDECESSOR, "김전임")
    # 재분석해도 전임자가 보탠 값과 닫힌 공백이 유지된다.
    rc.restore(db, BASIS)
    assert any(i["id"] == item_id for i in db.task_items(1))
    assert "missing:deadline" not in gap_keys(db)


def test_predecessor_adds_a_note_and_a_step_that_no_document_mentions(view):
    db = view.db
    view._set_reviewer("김전임", ws.PREDECESSOR)
    view._add_work_item(1, rc.NOTE, "예산팀 박주무관에게 9월 초에 미리 전화해 두면 수월함")
    step_id = view._add_work_step(1, "과장 사전 보고")

    card = workcard.build(db, 1)
    assert [n["content"] for n in card.notes] == ["예산팀 박주무관에게 9월 초에 미리 전화해 두면 수월함"]
    told = next(s for s in card.stated_steps if s["id"] == step_id)
    assert (told["procedure_kind"], told["human_state"], told["basis_state"]) == (
        ws.PROCEDURE_TOLD, ws.PREDECESSOR, ws.MISSING)
    # 문서에 적힌 단계 뒤, 과거 사례 앞에 놓인다.
    assert [s["label"] for s in card.stated_steps][-1] == "과장 사전 보고"
    shown = texts(page(view))
    assert "보충 설명" in shown and "전임자 확인 · 문서에 없음" in shown
    rc.restore(db, BASIS)
    assert any(s["id"] == step_id for s in workcard.build(db, 1).stated_steps)
    draft = workcard.draft_markdown(db, 1, BASIS)
    assert "과장 사전 보고 (사람이 알려 준 단계)" in draft and "## 보충 설명" in draft


def test_both_people_leave_their_own_marks_on_the_same_item(view):
    db = view.db
    recipient = next(i for i in db.task_items(1) if i["kind"] == rc.RECIPIENT)
    view._review_work_item(recipient["id"], "confirm")            # 후임자가 먼저 확인
    view._set_reviewer("김전임", ws.PREDECESSOR)
    view._review_work_item(recipient["id"], "edit", "기획예산과 예산1팀")   # 전임자가 고침

    item = next(i for i in db.task_items(1) if i["id"] == recipient["id"])
    assert (item["content"], item["human_state"], item["auto_content"]) == (
        "기획예산과 예산1팀", ws.PREDECESSOR, "기획예산과")
    history = [(r["action"], r["reviewer_role"], r["before_val"], r["after_val"])
               for r in db.reviews("item", recipient["id"])]
    assert history == [
        ("confirm", ws.SUCCESSOR, "기획예산과", "기획예산과"),
        ("edit", ws.PREDECESSOR, "기획예산과", "기획예산과 예산1팀")]


def test_gap_closed_by_the_predecessor_says_who_closed_it(view):
    db = view.db
    view._set_reviewer("김전임", ws.PREDECESSOR)
    gap = next(g for g in db.info_gaps(1) if g["gap_key"] == "afterwards")
    view._close_work_gap(gap["id"], "9/8에 과장 보고까지 마침")
    closed = next(g for g in db.info_gaps(1, state=None) if g["id"] == gap["id"])
    assert closed["resolution"] == "전임자 김전임 확인 — 9/8에 과장 보고까지 마침"


# ── 검토 인계본 ─────────────────────────────────────────────────────

def test_handover_states_exactly_what_the_predecessor_did_and_did_not_confirm(view):
    db = view.db
    view._set_reviewer("김전임", ws.PREDECESSOR)
    recipient = next(i for i in db.task_items(1) if i["kind"] == rc.RECIPIENT)
    view._review_work_item(recipient["id"], "confirm")
    view._add_work_item(1, rc.DEADLINE, "2025-09-30")

    snapshot_id, created = view._publish_handover("9월 둘째 주까지는 취합을 끝내 두세요")
    assert created
    row = db.snapshot(snapshot_id)
    summary = json.loads(row["summary"])
    assert row["kind"] == ws.SNAPSHOT_HANDOVER
    assert (summary["items_by_predecessor"], summary["items_by_successor"]) == (2, 0)
    assert "전임자 확인 2개" in release.describe(row)

    text = release.to_markdown(release.load(db, snapshot_id), row["kind"])
    assert text.startswith("# 전임자 검토 인계본")
    assert "- 시작 방식: 전임자와 함께" in text and "- 전임자: 김전임" in text
    assert "본인 확인을 거친 서명이 아닙니다" in text
    assert "- 발행은 내용 전체의 승인이 아닙니다." in text
    assert "- 전임자의 말: 9월 둘째 주까지는 취합을 끝내 두세요" in text
    assert "| 제출처 | 기획예산과 | 문서에 명시 | 적용 여부 미상 | 전임자 확인 |" in text
    # 전임자가 보지 않은 항목은 따로 적힌다 — 발행했다고 확인된 것이 아니다.
    unreviewed = text.split("## 전임자가 확인하지 않은 항목")[1].split("## 근거")[0]
    assert "정기 실적보고 · 절차:" in unreviewed and "사람 확인 없음" in unreviewed
    assert "제출처" not in unreviewed

    # 검토 이력이 인계본에 실려 다음 사람에게 넘어간다.
    payload = release.load(db, snapshot_id)
    saved = next(i for i in payload["tasks"][0]["items"] if i["kind"] == rc.RECIPIENT)
    assert [(r["action"], r["role"], r["name"]) for r in saved["reviews"]] == [
        ("confirm", ws.PREDECESSOR, "김전임")]


def test_handover_and_restoration_versions_are_kept_side_by_side(view):
    db = view.db
    restoration, _ = release.save(db)
    view._set_reviewer("김전임", ws.PREDECESSOR)
    handover, created = view._publish_handover()
    assert created and handover != restoration
    assert view._publish_handover() == (handover, False)
    assert [row["kind"] for row in db.snapshots()] == [
        ws.SNAPSHOT_HANDOVER, ws.SNAPSHOT_RESTORATION]
    # 인계본을 낸 뒤에도 자료 기반 복원본은 계속 저장된다.
    view._set_reviewer("", ws.SUCCESSOR)
    step = workcard.build(db, 1).stated_steps[0]
    db.set_step_run(step["id"], ws.DONE)
    assert release.save(db)[1]


# ── 화면 ────────────────────────────────────────────────────────────

def test_reviewer_bar_offers_publishing_only_while_the_predecessor_is_reviewing(view):
    def buttons():
        return [b.text() for b in page(view).findChildren(QPushButton)]

    assert "검토 인계본 발행" not in buttons()
    assert "전임자가 없어도 됩니다. 함께 볼 수 있을 때만 바꾸세요." in texts(page(view))
    who = next(c for c in page(view).findChildren(QComboBox) if c.accessibleName() == "검토자 역할")
    assert who.currentData() == ws.SUCCESSOR
    view._set_reviewer("김전임", ws.PREDECESSOR)
    assert "검토 인계본 발행" in buttons()
    assert next(c for c in page(view).findChildren(QComboBox) if c.accessibleName() == "검토자 역할").currentData() == ws.PREDECESSOR
    view._set_reviewer("이후임", ws.SUCCESSOR)
    assert "검토 인계본 발행" not in buttons()
    assert {"아는 내용 보태기", "단계 보태기"} <= set(buttons())


def test_later_answer_from_the_predecessor_does_not_restart_work_in_progress(view):
    """후임자가 이미 진행한 뒤에 전임자의 답을 받아도 하던 일은 그대로다."""
    db = view.db
    first, second, _third = workcard.build(db, 1).stated_steps
    db.set_step_run(first["id"], ws.DONE)
    db.set_step_run(second["id"], ws.IN_PROGRESS)

    view._set_reviewer("김전임", ws.PREDECESSOR)
    view._add_work_item(1, rc.DEADLINE, "2025-09-30")
    # 단계 문구를 고치는 단추는 화면에 아직 없어 저장 계층으로 직접 고친다.
    db.review_work_step(second["id"], "edit", "실적표 작성(별지 3호, 전년 대비 증감 포함)",
                        "김전임", ws.PREDECESSOR)
    continuity.review_task(db, 1)

    steps = workcard.build(db, 1).stated_steps
    assert [s["run_state"] for s in steps[:2]] == [ws.DONE, ws.IN_PROGRESS]
    assert steps[1]["label"].startswith("실적표 작성(별지 3호") and steps[1]["human_state"] == ws.PREDECESSOR
    assert not steps[2]["blocked"]
    assert workcard.build(db, 1).actions[0]["step_id"] == second["id"]
