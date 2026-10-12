"""일정 화면 시험.

이 화면의 존재 이유는 "지금 당장 손대야 하는 게 있나"에 답하는 것이다.
그리고 달력 앱과 갈라지는 지점은 "작년 이맘때 무슨 문서가 있었나"를 함께
보여준다는 데 있다. 두 가지가 실제로 화면에 오르는지를 여기서 못 박는다.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton, QWidget  # noqa: E402

from app.core import status  # noqa: E402
from app.db import Database  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.views import calendar as cal  # noqa: E402
from app.ui.views.calendar import CalendarView  # noqa: E402

TODAY = date(2026, 8, 12)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet())
    yield app


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    project = Database(tmp_path / "project.db")
    project.init()
    source_id = project.add_source(tmp_path / "자료")

    def add_doc(name: str, year: int, month: int) -> int:
        return project.upsert_document(source_id, {
            "path": str(tmp_path / f"{name}"), "filename": name, "ext": ".hwp",
            "parse_status": "ok", "eff_date": f"{year}-{month:02d}-05",
            "eff_date_kind": "body", "eff_precision": "day",
            "eff_year": year, "eff_month": month,
        })

    # 9~11월 반복 업무. 9월이면 D-20이라 '지금 챙길 일'에 올라와야 한다.
    project.con.execute("INSERT INTO tasks(id, name, confidence) VALUES (1, '행정사무감사', 'high')")
    sept_doc = None
    for year, month in [(2024, 9), (2025, 9), (2025, 10)]:
        doc_id = add_doc(f"{year}_행감_{month}월.hwp", year, month)
        project.con.execute("INSERT INTO task_docs(task_id, doc_id) VALUES (1, ?)", (doc_id,))
        if (year, month) == (2025, 9):
            sept_doc = doc_id
    project.con.execute(
        "INSERT INTO task_cycles(task_id, kind, months, years_observed, confidence, decided_by) "
        "VALUES (1, 'yearly', '9,10,11', 4, 'high', 'ai')"
    )
    project.replace_task_steps(1, 2025, [
        {"ordinal": 1, "label": "요구자료 접수", "month": 9, "day_hint": "9월 초",
         "doc_id": sept_doc, "gap_note": None},
    ])

    # 5월 반복 — 한참 뒤라 '앞으로 올 일'로 가야 한다. 근거는 2개 연도뿐이라 WEAK.
    project.con.execute("INSERT INTO tasks(id, name, confidence) VALUES (2, '계약관리', 'low')")
    contract_doc = add_doc("2025_계약.hwp", 2025, 5)
    project.con.execute(
        "INSERT INTO task_docs(task_id, doc_id) VALUES (2, ?)", (contract_doc,)
    )
    project.con.execute(
        "INSERT INTO task_cycles(task_id, kind, months, years_observed, confidence, decided_by) "
        "VALUES (2, 'yearly', '5', 2, 'low', 'ai')"
    )

    yield project
    project.close()


@pytest.fixture()
def view(db: Database, qapp, monkeypatch) -> CalendarView:
    class FixedDate(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(cal, "date", FixedDate)
    widget = CalendarView(db)
    yield widget
    widget.setParent(None)


def texts(widget) -> list[str]:
    out = []
    for child in widget.findChildren(QWidget):
        getter = getattr(child, "text", None)
        if getter:
            value = getter()
            if value and value.strip():
                out.append(value)
    return out


def blob(widget) -> str:
    return "\n".join(texts(widget))


def tab(view: CalendarView, key: str) -> str:
    """탭 하나의 내용만. 숨어 있는 탭도 위젯으로는 살아 있으므로 화면 전체를
    훑으면 어느 탭에 있는 내용인지 구분되지 않는다."""
    return blob(view.pages[key])


def tab_labels(view: CalendarView) -> list[str]:
    return [b.text() for b in view.tabs.findChildren(QPushButton)]


# ── 정체성 ──────────────────────────────────────────────────────────

def test_title_says_this_is_discovered_not_entered(view: CalendarView):
    """달력 앱이 아니라는 것을 제목이 먼저 말해야 한다 (계획서 §12)."""
    assert "자료에서 발견한 업무 일정" in blob(view)
    assert "직접 입력한 달력이 아니" in blob(view)


# ── 탭 ──────────────────────────────────────────────────────────────

def test_year_pattern_is_the_first_tab_and_opens_by_default(view: CalendarView):
    assert tab_labels(view)[0].startswith("연간 패턴")
    assert view.stack.currentWidget() is view.pages[cal.YEAR]


def test_tab_labels_carry_counts(view: CalendarView):
    """누르기 전에 규모를 알 수 있어야 탭이 값을 한다."""
    labels = tab_labels(view)
    assert any(l.startswith("지금 챙길 일") and l.rstrip().endswith("1") for l in labels), labels
    assert any(l.startswith("확인 필요") and l.rstrip().endswith("2") for l in labels), labels


def test_zero_count_tab_shows_no_number(view: CalendarView):
    view._confirm(1)
    view._confirm(2)
    assert "확인 필요" in tab_labels(view)


def test_switching_tabs_changes_the_visible_page(view: CalendarView):
    view._switch(cal.NOW)
    assert view.stack.currentWidget() is view.pages[cal.NOW]


def test_refresh_keeps_the_tab_the_user_was_reading(view: CalendarView):
    """분석 중에는 1.5초마다 다시 그린다. 보던 탭을 빼앗으면 읽을 수가 없다."""
    view._switch(cal.LATER)
    view.refresh()
    assert view.stack.currentWidget() is view.pages[cal.LATER]


def test_identity_line_stays_visible_on_every_tab(view: CalendarView):
    """어느 탭에 있든 '내가 입력한 달력이 아니다'가 사라지면 안 된다."""
    for key, _label in cal.TABS:
        view._switch(key)
        assert "직접 입력한 달력이 아니" in blob(view)


# ── 지금 챙길 일 ────────────────────────────────────────────────────

def test_imminent_task_goes_to_the_now_tab(view: CalendarView):
    """D-20짜리 업무가 목록 한 줄로 밀리면 안 된다 — 그게 옛 화면의 문제였다."""
    assert "행정사무감사" in tab(view, cal.NOW)
    assert "행정사무감사" not in tab(view, cal.LATER)


def test_distant_task_goes_to_the_later_tab(view: CalendarView):
    assert "계약관리" in tab(view, cal.LATER)
    assert "계약관리" not in tab(view, cal.NOW)


def test_urgency_is_shown_in_days_not_just_a_month(view: CalendarView):
    assert "20일 뒤" in tab(view, cal.NOW)


def test_last_time_shows_the_first_step_and_its_documents(view: CalendarView):
    """이 화면이 달력과 갈라지는 지점. 날짜만 있으면 후임자는 뭘 할지 모른다."""
    body = tab(view, cal.NOW)
    assert "'요구자료 접수'부터 시작했습니다" in body
    assert "2025_행감_9월.hwp" in body


def test_missing_history_is_stated_not_hidden(db: Database, qapp, monkeypatch):
    """근거가 없으면 빈칸이 아니라 없다고 말한다."""
    class FixedDate(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(cal, "date", FixedDate)
    db.con.execute("DELETE FROM task_docs WHERE task_id = 1")
    db.con.execute("DELETE FROM task_steps WHERE task_id = 1")
    widget = CalendarView(db)
    assert "9월에 무엇부터 했는지는 자료에서 확인되지 않았습니다" in tab(widget, cal.NOW)
    widget.setParent(None)


def test_monthly_task_is_always_running_now(db: Database, qapp, monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(cal, "date", FixedDate)
    db.con.execute("UPDATE task_cycles SET kind = 'monthly', months = '' WHERE task_id = 1")
    widget = CalendarView(db)
    assert "진행 중" in tab(widget, cal.NOW)
    widget.setParent(None)


# ── 확인 필요 ───────────────────────────────────────────────────────

def test_weak_cycles_are_listed_first_for_review(view: CalendarView):
    """판단이 실제로 필요한 건 근거가 얇은 쪽이다."""
    body = tab(view, cal.REVIEW)
    assert body.index("계약관리") < body.index("행정사무감사")
    assert "2개 연도뿐" in body


def test_cycle_can_be_confirmed_without_leaving_the_screen(view: CalendarView):
    """주기가 틀렸다는 걸 가장 먼저 알아채는 자리가 이 화면이다."""
    assert "맞습니다" in [b.text() for b in view.pages[cal.REVIEW].findChildren(QPushButton)]

    view._confirm(1)
    assert status.of_cycle(view.db.task_cycle(1)) == status.CONFIRMED
    assert "행정사무감사" not in tab(view, cal.REVIEW)


def test_all_confirmed_says_so_instead_of_showing_an_empty_tab(view: CalendarView):
    view._confirm(1)
    view._confirm(2)
    assert "모든 반복 주기를 확인했습니다" in tab(view, cal.REVIEW)


def test_not_recurring_removes_it_from_the_schedule(view: CalendarView):
    view.db.mark_no_cycle(2)
    view.refresh()
    assert "계약관리" not in blob(view)


def test_tasks_marked_not_a_task_leave_the_schedule(view: CalendarView):
    view.db.mark_not_a_task(2)
    view.refresh()
    assert "계약관리" not in blob(view)


# ── 연간 패턴 ──────────────────────────────────────────────────────

def test_year_grid_lists_every_task(view: CalendarView):
    body = tab(view, cal.YEAR)
    assert "행정사무감사" in body and "계약관리" in body


def test_year_grid_task_names_are_clickable(view: CalendarView):
    """읽기만 하는 그림이면 이상한 걸 발견해도 확인하러 갈 길이 없다."""
    names = [b.text() for b in view.pages[cal.YEAR].findChildren(QPushButton)]
    assert "행정사무감사" in names


def test_legend_does_not_repeat_itself(view: CalendarView):
    """status.label()은 이미 기호+문구다. 그대로 쓰면 '자료에서 추정 자료에서 추정'이 된다."""
    assert "자료에서 추정 자료에서 추정" not in blob(view)
    assert cal._legend().startswith(status.symbol(status.CONFIRMED))


# ── 빈 상태 ─────────────────────────────────────────────────────────

def test_no_cycles_explains_why_instead_of_showing_a_blank_screen(tmp_path, qapp):
    empty = Database(tmp_path / "empty.db")
    empty.init()
    empty.con.execute("INSERT INTO tasks(id, name) VALUES (1, '업무')")
    widget = CalendarView(empty)
    assert "반복 업무를 아직 찾지 못했습니다" in blob(widget.blank)
    assert "근거 없이 추측하지 않습니다" in blob(widget.blank)
    widget.setParent(None)
    empty.close()


def test_tabs_are_hidden_when_there_is_nothing_to_tab_through(tmp_path, qapp):
    """탭 네 개가 모두 비어 있으면 탭 자체가 소음이다."""
    empty = Database(tmp_path / "empty.db")
    empty.init()
    widget = CalendarView(empty)
    assert not widget.tabs.isVisible()
    widget.setParent(None)
    empty.close()


# ── 월별 일정 ───────────────────────────────────────────────────────
# 연간 패턴은 "9월에 한다"까지다. 문서에 적힌 일정·기한("9. 20.(금) 한")을 달별로
# 펴서 "며칠까지 무엇을 했나"를 보인다. 지난 회차의 날짜라는 것이 줄마다 보여야 한다.

def _schedule(db: Database, task_id: int, slot: str, content: str, doc_id: int, quote: str) -> None:
    from app.core import workstate as ws

    db.sync_task_items(task_id, [{
        "kind": "deadline", "slot": slot, "content": content, "basis_state": ws.STATED,
        "applicability": ws.PAST, "evidence": [(doc_id, "1문단", quote)]}])


def _doc_id(db: Database, name: str) -> int:
    return db.con.execute("SELECT id FROM documents WHERE filename = ?", (name,)).fetchone()[0]


def test_dates_written_in_documents_are_listed_by_month_from_this_month(db, qapp, monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(cal, "date", FixedDate)
    plan = _doc_id(db, "2025_행감_9월.hwp")
    _schedule(db, 1, "일정 · 요구자료 제출", "9월 20일 / 10월 5일", plan,
              "요구자료 제출 : 9. 20.(금) 한 … 10. 5.(일) 한")
    _schedule(db, 2, "일정 · 계약 준비", "5월 초", _doc_id(db, "2025_계약.hwp"), "계약 준비 : 5월 초 한")
    widget = CalendarView(db)

    assert any(label.startswith("월별 일정") and label.rstrip().endswith("3")
               for label in tab_labels(widget))
    assert tab_labels(widget)[0].startswith("연간 패턴")                 # 첫 탭은 그대로다
    shown = texts(widget.pages[cal.DATES])
    # 이번 달(8월)에는 없으니 9월부터 나오고, 지나간 5월은 내년으로 뒤에 온다.
    heads = [t for t in shown if t.endswith("월") or "· " in t and t.split(" · ")[0].endswith("월")]
    assert heads == ["9월", "10월", "5월 · 내년"]
    assert "20일" in shown and "5일" in shown and "월초" in shown
    assert "요구자료 제출" in shown and "행정사무감사" in shown
    assert "2025년 기록" in shown                                       # 올해 날짜가 아니다
    assert "올해의" in "\n".join(
        child.toolTip() for child in widget.pages[cal.DATES].findChildren(QWidget) if child.toolTip())
    widget.setParent(None)


def test_dates_tab_opens_when_documents_state_dates_but_nothing_recurs(tmp_path, qapp):
    """한 해 자료뿐이라 반복은 못 찾아도, 계획서가 적은 일정은 보여 줄 수 있다."""
    project = Database(tmp_path / "once.db")
    project.init()
    source_id = project.add_source(tmp_path / "자료")
    doc_id = project.upsert_document(source_id, {
        "path": str(tmp_path / "2025년 선발 계획.hwp"), "filename": "2025년 선발 계획.hwp",
        "ext": ".hwp", "parse_status": "ok", "eff_year": 2025})
    project.con.execute("INSERT INTO tasks(id, name) VALUES (1, '우수부서 선발')")
    _schedule(project, 1, "일정 · 부서 추천", "6월 17일", doc_id, "부서 추천 : 6. 17.(월) 한")
    widget = CalendarView(project)

    assert not widget.tabs.isHidden() and widget.blank.isHidden()
    assert widget.stack.currentWidget() is widget.pages[cal.DATES]
    assert "반복 업무를 아직 찾지 못했습니다" in blob(widget.pages[cal.YEAR])
    widget._choose(cal.YEAR)                     # 사용자가 고른 탭은 다시 그려도 빼앗지 않는다
    widget.refresh()
    assert widget.stack.currentWidget() is widget.pages[cal.YEAR]
    widget.setParent(None)
    project.close()


def test_the_same_plan_filed_under_two_tasks_is_one_line(db):
    from app.core import schedule

    plan = _doc_id(db, "2025_행감_9월.hwp")
    _schedule(db, 1, "일정 · 우수공무원 추천(전부서)", "2025-06-17", plan, "우수공무원 추천(전부서) : 2025. 6. 17.(화) 한")
    _schedule(db, 2, "일정 · 우수공무원 부서 추천", "2025-06-17", plan, "우수공무원 부서 추천 : 2025. 6. 17.(화) 한")
    found = schedule.entries(db)
    assert [(e.month, e.day, e.year, e.what) for e in found] == [(6, 17, 2025, "우수공무원 부서 추천")]
    assert found[0].filename == "2025_행감_9월.hwp" and found[0].path
