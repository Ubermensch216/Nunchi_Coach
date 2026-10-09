"""첫 업무 카드·결과 초안·진행 기록 (C05).

완료 증거: 후임자가 원문 → 처리 단계 → 결과 초안 → 수행 기록까지 이용한다.
전임자 확인을 요구하는 입력이나 대기 화면이 없다.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from app.core import reconstruction as rc, workcard, workstate as ws  # noqa: E402
from app.core.output_policy import UnsafeOutputError  # noqa: E402
from app.db import Database  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.views import tasks as tasks_view  # noqa: E402
from app.ui.views.tasks import TasksView  # noqa: E402

BASIS = date(2025, 9, 10)
REQUEST = ("절차: 집계 자료 준비 → 실적표 작성 → 검토 후 제출\n"
           "제출처: 기획예산과\n구비서류: 부서별 집계 자료\n사용 서식: 실적표 별지 제3호\n"
           "추진 목적: 분기별 실적의 정기 점검")


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
    yield database
    database.close()


def add(db, name, when, text="", folder="자료"):
    root = Path(db.path).parent.parent / folder
    root.mkdir(exist_ok=True)
    source = db.add_source(root)
    path = root / name
    path.write_text(text or name, encoding="utf-8")
    doc = db.upsert_document(source, {
        "path": str(path), "filename": name, "ext": ".hwpx", "parse_status": "ok",
        "hash": name, "char_count": len(text),
        "eff_date": when, "eff_year": int(when[:4]), "eff_month": int(when[5:7]),
        "eff_precision": "day", "eff_date_kind": "body"})
    if text:
        db.replace_sections(doc, [("paragraph", 1, "1문단", text)])
    db.index_document(doc, name, str(path), "", text)
    db.assign_document(1, doc)
    return doc


@pytest.fixture
def restored(db):
    old = add(db, "2024_제출본.hwpx", "2024-09-27")
    db.replace_task_steps(1, 2024, [{"ordinal": 1, "label": "제출", "month": 9,
                                     "day_hint": "9월 말", "doc_id": old, "gap_note": None}])
    add(db, "2025_실적보고_요청.hwpx", "2025-09-01", REQUEST)
    rc.restore(db, BASIS)
    return db


def texts(widget) -> list[str]:
    return [label.text() for label in widget.findChildren(QLabel) if label.text()]


def button(widget, text: str) -> QPushButton:
    return next(b for b in widget.findChildren(QPushButton) if b.text() == text)


def action(widget, text: str) -> QAction:
    """줄 끝 ⋯ 메뉴 안의 동작."""
    return next(a for a in widget.findChildren(QAction) if a.text() == text)


# ── 카드 ────────────────────────────────────────────────────────────

def test_card_gathers_everything_a_successor_needs_from_the_materials_alone(restored):
    card = workcard.build(restored, 1)
    assert not card.empty
    assert [(i["kind"], i["content"]) for i in card.todo] == [
        (rc.PURPOSE, "분기별 실적의 정기 점검"), (rc.RECIPIENT, "기획예산과")]
    assert [i["kind"] for i in card.prepare] == [rc.INPUT, rc.FORM]
    assert card.records[0]["slot"] == rc.SLOT_LAST_RECORD
    assert [s["label"] for s in card.stated_steps] == ["집계 자료 준비", "실적표 작성", "검토 후 제출"]
    assert [s["label"] for s in card.precedent_steps] == ["제출 (2024년 9월 말)"]
    assert card.actions[0]["type"] == "do"
    assert {g["gap_key"] for g in card.gaps} == {"missing:deadline", "afterwards"}
    assert card.headline() == "3단계 중 0단계 완료 · 확인할 것 2건"
    # 사람은 아무것도 입력하지 않았다.
    assert all(i["human_state"] == ws.NONE for i in card.todo + card.prepare)


def test_task_without_restored_content_has_an_empty_card(db):
    assert workcard.build(db, 1).empty
    assert workcard.build(db, 1).headline() == "복원된 내용이 없습니다"


# ── 결과 초안 ───────────────────────────────────────────────────────

def test_draft_carries_confirmed_values_with_sources_and_leaves_unknowns_blank(restored):
    text = workcard.draft_markdown(restored, 1, BASIS)
    assert text.startswith("# 정기 실적보고 — 수행 초안")
    assert "| 제출처 | 기획예산과 | 문서에 명시 · 적용 여부 미상 · 사람 확인 없음 | [" in text
    # 기한은 자료에 없다 — 빈칸으로 남고, 지어낸 날짜가 들어가지 않는다.
    assert "| 기한 |  | 자료 없음 |  |" in text
    assert "2025-09-30" not in text and "9월 30일" not in text
    assert "| 1 | 집계 자료 준비 | 부서별 집계 자료 |" in text
    assert "과거 수행 사례입니다. 그렇게 했다는 기록이며" in text
    assert "- [할 수 있는 일] '집계 자료 준비' 단계를 진행할 수 있습니다." in text
    assert "**기한** — 기한이(가) 이 업무의 자료에 적혀 있지 않습니다." in text
    # 근거는 번호로 이어지고, 인용문이 함께 실린다.
    assert "2025_실적보고_요청.hwpx · 1문단 — “제출처: 기획예산과”" in text
    assert "- 아직 기록한 단계가 없습니다." in text


def test_draft_marks_conflicts_and_execution_records(restored):
    add(restored, "메모.hwpx", "2025-09-05", "제출처: 감사담당관")
    rc.restore(restored, BASIS)
    first = workcard.build(restored, 1).stated_steps[0]
    restored.set_step_run(first["id"], ws.DONE, result_path="outputs/집계.xlsx", note="9월분 취합")
    text = workcard.draft_markdown(restored, 1, BASIS)
    assert "(엇갈림) 기획예산과 / 감사담당관" in text
    assert "- 집계 자료 준비: 완료 (" in text and "결과 outputs/집계.xlsx · 9월분 취합" in text


# ── 화면: 원문 → 처리 단계 → 결과 초안 → 수행 기록 ──────────────────

@pytest.fixture
def view(restored, qapp):
    widget = TasksView(restored)
    widget.open_task(1)
    yield widget
    widget.setParent(None)


def test_task_opens_on_the_work_tab_with_the_next_action_first(view):
    assert view._tab == tasks_view.WORK
    assert view.stack.currentWidget() is view.pages[tasks_view.WORK]
    shown = texts(view.pages[tasks_view.WORK])
    # 할 수 있는 일이 '다음 행동'의 첫 줄이다 — 이름표 바로 아래에 값으로 선다.
    assert shown.index("지금 할 수 있는 일") + 1 == next(
        i for i, t in enumerate(shown) if t.startswith("'집계 자료 준비'"))
    assert "기획예산과" in shown
    assert any(t.startswith("문서에 적힌 절차입니다.") for t in shown)
    assert any("지난 회차에 이렇게 했다는 기록입니다" in t for t in shown)
    assert "확인이 필요한 정보 2건" in shown
    # 전임자의 확인·승인을 요구하는 문구나 대기 화면이 없다.
    assert not any("전임자" in t and ("승인" in t or "기다" in t) for t in shown)


def test_evidence_opens_the_quoted_passage_and_the_original(view):
    page = view.pages[tasks_view.WORK]
    next(b for b in page.findChildren(QPushButton) if b.text() == "근거 1건").click()
    drawer = texts(view.drawer)
    assert "2025_실적보고_요청.hwpx" in drawer
    assert any("추진 목적: 분기별 실적의 정기 점검" in t for t in drawer)


def test_steps_are_recorded_only_by_the_user_and_the_next_action_moves_on(view):
    db = view.db
    page = view.pages[tasks_view.WORK]
    assert workcard.build(db, 1).progress["stated_done"] == 0   # 열어 본 것만으로는 0이다
    [b for b in page.findChildren(QPushButton) if b.text() == "완료로 기록"][0].click()

    card = workcard.build(db, 1)
    assert card.stated_steps[0]["run_state"] == ws.DONE and card.progress["stated_done"] == 1
    shown = texts(view.pages[tasks_view.WORK])
    assert "지금 할 수 있는 일" in shown
    assert any(t.startswith("'실적표 작성'") for t in shown)
    assert "3단계 중 1단계 완료 · 확인할 것 2건" in shown
    # 단추는 지금 차례인 단계에만 선다. 끝낸 단계는 ⋯ 안에서 되돌린다.
    assert [b.text() for b in view.pages[tasks_view.WORK].findChildren(QPushButton)
            ].count("완료로 기록") == 1
    action(view.pages[tasks_view.WORK], "완료 취소").trigger()
    assert workcard.build(db, 1).stated_steps[0]["run_state"] == ws.IN_PROGRESS


def test_blocked_step_says_so_while_other_steps_stay_usable(view):
    shown = texts(view.pages[tasks_view.WORK])
    assert sum("이 단계에 필요한 정보가 아직 확인되지 않았습니다" in t for t in shown) == 1
    assert any("다음 행동 — 이번 회차의 요청·시행 공문 본문과 붙임에서 기한을 확인" in t
               for t in shown)


def test_confirming_a_value_is_optional_and_recorded(view):
    db = view.db
    # 값마다 단추를 늘어놓지 않는다. 확인·고치기는 줄 끝의 ⋯ 안에 있다.
    action(view.pages[tasks_view.WORK], "맞습니다").trigger()
    confirmed = [i for i in db.task_items(1) if i["human_state"] == ws.SUCCESSOR]
    assert len(confirmed) == 1
    assert db.con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1


def test_draft_is_saved_outside_the_source_folder_and_never_over_an_original(view):
    db = view.db
    target = workcard.default_output_dir(db) / "초안.md"
    assert view._write_draft(1, str(target)) > 0
    saved = target.read_text(encoding="utf-8-sig")
    assert saved.startswith("# 정기 실적보고 — 수행 초안")
    assert not any(Path(row["path"]).is_relative_to(target.parent) for row in db.sources())

    original = Path(db.document(1)["path"])
    before = original.read_bytes()
    for unsafe in (original, original.parent / "새초안.md"):
        with pytest.raises(UnsafeOutputError):
            view._write_draft(1, str(unsafe))
    assert original.read_bytes() == before and not (original.parent / "새초안.md").exists()


def test_work_survives_closing_and_reopening_the_project(restored, qapp):
    """수행 중 종료·재시작 뒤에도 남은 단계에서 이어진다."""
    path = restored.path
    first = workcard.build(restored, 1).stated_steps[0]
    restored.set_step_run(first["id"], ws.DONE, note="취합 끝")
    restored.close()

    reopened = Database(path)
    reopened.init()
    try:
        rc.restore(reopened, BASIS)   # 다시 분석해도 기록이 남는다
        card = workcard.build(reopened, 1)
        assert (card.stated_steps[0]["run_state"], card.stated_steps[0]["run_note"]) == (
            ws.DONE, "취합 끝")
        assert card.actions[0]["step_id"] == card.stated_steps[1]["id"]
    finally:
        reopened.close()
