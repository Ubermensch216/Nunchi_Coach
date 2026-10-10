"""문서 화면 교정 UI 시험 — 시점 교정과 업무 배정.

DB에는 교정 함수가 먼저 갖춰져 있었지만 화면이 없어서 사용자는 손댈 수
없었다(계획서 §11의 구멍 두 개). 여기서 보는 것은 화면이 그 함수까지
실제로 이어지는가, 그리고 **고친 값이 재분석에서 살아남게 표시되는가**다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

NAME_ROLE = Qt.ItemDataRole.UserRole + 1   # _TaskPicker가 업무 이름을 담는 자리

from app.core import status  # noqa: E402
from app.db import Database  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.views import documents as documents_view  # noqa: E402
from app.ui.views.documents import (  # noqa: E402
    DocumentsView,
    _DateDialog,
    _parse_date_text,
    _TaskPicker,
    _TaskMoveDialog,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet())
    yield app


@pytest.fixture()
def view(tmp_path: Path, qapp):
    db = Database(tmp_path / "project.db")
    db.init()
    source_id = db.add_source(tmp_path / "자료")
    db.upsert_document(source_id, {
        "path": str(tmp_path / "감사자료.hwp"), "filename": "감사자료.hwp",
        "ext": ".hwp", "parse_status": "ok",
        "eff_date": "2024-09-01", "eff_date_kind": "fs",
        "eff_precision": "day", "eff_year": 2024, "eff_month": 9,
    })
    db.upsert_document(source_id, {
        "path": str(tmp_path / "미분류.hwp"), "filename": "미분류.hwp",
        "ext": ".hwp", "parse_status": "ok",
    })
    db.replace_dates(1, [
        ("body", "2024-03-11", "day", "2024. 3. 11.", "1쪽"),
        ("filename", "2024-01-01", "year", "2024년", ""),
    ])
    db.con.execute("INSERT INTO tasks(id, name, confidence) VALUES (1, '행정사무감사', 'high')")
    db.con.execute("INSERT INTO tasks(id, name, confidence) VALUES (2, '예산관리', 'medium')")
    db.con.execute("INSERT INTO task_docs(task_id, doc_id) VALUES (1, 1)")

    widget = DocumentsView(db)
    yield widget
    widget.setParent(None)
    db.close()


# ── 시점 교정 ───────────────────────────────────────────────────────

def test_date_dialog_lists_every_candidate_and_keeps_its_origin(view, qapp):
    """후보를 고르면 그 후보의 출처가 유지돼야 한다 — 근거와의 연결이 끊기면
    확정한 뒤에 '무엇을 보고 정했는지'가 사라진다."""
    row = view.db.document(1)
    dialog = _DateDialog(view, row, view.db.dates(1))

    labels = [b.text() for b in dialog._choices]
    assert any("① 본문" in text and "2024-03-11" in text for text in labels)
    assert any("② 파일명" in text and "2024년" in text for text in labels)

    button = next(b for b in dialog._choices if "① 본문" in b.text())
    button.setChecked(True)
    assert dialog.choice() == ("2024-03-11", "day", "body")


def test_choosing_a_candidate_marks_the_date_as_confirmed_by_a_person(view):
    view.db.set_document_date(1, "2024-03-11", precision="day", kind="body")

    row = view.db.document(1)
    assert row["eff_date"] == "2024-03-11"
    assert row["date_decided_by"] == "user"
    assert status.of_document_date(row) == status.CONFIRMED


def test_unknown_is_a_decision_not_a_blank(view):
    """'모름' 확정은 빈칸과 다르다. 다음 재분석이 다시 추정하면 안 된다."""
    row = view.db.document(1)
    dialog = _DateDialog(view, row, view.db.dates(1))
    dialog.unknown.setChecked(True)
    value, precision, kind = dialog.choice()

    view.db.set_document_date(1, value, precision=precision, kind=kind)
    after = view.db.document(1)
    assert after["eff_date"] is None
    assert after["date_decided_by"] == "user"

    # 파이프라인이 시점을 다시 채울 문서 목록에서 빠진다.
    pending = view.db.con.execute(
        "SELECT id FROM documents WHERE eff_date IS NULL "
        "AND parse_status != 'skipped' AND date_decided_by != 'user'"
    ).fetchall()
    assert 1 not in [r["id"] for r in pending]


def test_manual_input_is_validated_before_ok_is_enabled(view):
    dialog = _DateDialog(view, view.db.document(1), view.db.dates(1))
    dialog.manual.setChecked(True)

    dialog.text.setText("어제쯤")
    assert dialog.choice() is None
    assert not dialog._ok.isEnabled()

    dialog.text.setText("2023.5.7")
    assert dialog.choice() == ("2023-05-07", "day", "user")
    assert dialog._ok.isEnabled()


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2024", ("2024-01-01", "year")),
        ("2024-03", ("2024-03-01", "month")),
        ("2024년 3월", ("2024-03-01", "month")),
        ("2024.3.11", ("2024-03-11", "day")),
        ("2024-03-11", ("2024-03-11", "day")),
        ("2024-02-30", None),        # 없는 날짜를 지어내지 않는다
        ("2024-13-01", None),
        ("", None),
        ("올해", None),
        ("1000-01-01", None),
    ],
)
def test_date_text_parsing(text, expected):
    assert _parse_date_text(text) == expected


def test_detail_panel_offers_a_correction_even_when_no_date_was_found(view):
    view._show_detail(2)                      # 날짜 후보가 없는 문서
    labels = _buttons(view)
    assert "시점 직접 지정" in labels


def test_detail_panel_offers_a_correction_for_a_dated_document(view):
    view._show_detail(1)
    assert "시점 고치기" in _buttons(view)


# ── 업무 배정 ───────────────────────────────────────────────────────

def test_unclassified_filter_shows_only_documents_without_a_task(view):
    view.unclassified_only.setChecked(True)
    view.refresh()

    shown = [view.table.item(r, 0).text() for r in range(view.table.rowCount())]
    assert shown == ["미분류.hwp"]
    assert "미분류만 보기 (1)" == view.unclassified_only.text()


def test_task_picker_excludes_tasks_the_document_already_belongs_to(view):
    taken = {link["id"] for link in view.db.document_tasks(1)}
    choices = [task for task in view.db.tasks() if task["id"] not in taken]

    picker = _TaskPicker(view, choices)
    names = [picker.list.item(i).data(NAME_ROLE) for i in range(picker.list.count())]
    assert names == ["예산관리"]


def test_task_picker_filters_by_name(view):
    picker = _TaskPicker(view, view.db.tasks())
    picker.filter.setText("예산")

    visible = [
        picker.list.item(i).data(NAME_ROLE)
        for i in range(picker.list.count())
        if not picker.list.item(i).isHidden()
    ]
    assert visible == ["예산관리"]


def test_assigning_and_detaching_a_document(view):
    view.db.assign_document(2, 2)
    assert [link["name"] for link in view.db.document_tasks(2)] == ["예산관리"]
    assert view.db.unclassified_count() == 0

    view._detach_task(2, 2)
    assert view.db.document_tasks(2) == []
    assert view.db.unclassified_count() == 1


def test_detail_panel_shows_the_task_and_a_way_to_change_it(view):
    view._show_detail(1)
    assert "＋ 업무에 배정" in _buttons(view)
    assert "배정 해제" in _buttons(view)
    assert any("행정사무감사" in text for text in _labels(view))


def test_view_wires_the_picker_through_to_the_database(view, monkeypatch):
    """화면 → 대화상자 → repo까지 실제로 이어지는지 본다. 대화상자만 시험하면
    연결이 끊긴 채로 초록불이 켜진다."""
    monkeypatch.setattr(documents_view._TaskPicker, "run", classmethod(lambda cls, *a: 2))
    view._assign_task(2)

    assert [link["name"] for link in view.db.document_tasks(2)] == ["예산관리"]


def test_view_wires_the_date_dialog_through_to_the_database(view, monkeypatch):
    monkeypatch.setattr(
        documents_view._DateDialog, "run",
        classmethod(lambda cls, *a: ("2024-03-11", "day", "body")),
    )
    view._edit_date(1)

    row = view.db.document(1)
    assert (row["eff_date"], row["eff_date_kind"]) == ("2024-03-11", "body")
    assert row["date_decided_by"] == "user"


def test_cancelled_dialog_changes_nothing(view, monkeypatch):
    monkeypatch.setattr(documents_view._DateDialog, "run", classmethod(lambda cls, *a: None))
    before = view.db.document(1)["eff_date"]

    view._edit_date(1)

    assert view.db.document(1)["eff_date"] == before


def _buttons(view) -> list[str]:
    from PySide6.QtWidgets import QPushButton

    holder = view.detail.parentWidget()
    return [b.text() for b in holder.findChildren(QPushButton) if b.text()]


def _labels(view) -> list[str]:
    from PySide6.QtWidgets import QLabel

    holder = view.detail.parentWidget()
    return [w.text() for w in holder.findChildren(QLabel) if w.text()]


# ── 문서 역할·연도 교정 (계획서 M8) ─────────────────────────────────

def test_kind_dialog_changes_only_what_the_person_touched(view):
    from app.core import docrole, docyears
    from app.ui.views.documents import _KindDialog

    docyears.mark_documents(view.db, [1])
    view.db.con.execute("INSERT INTO doc_years(doc_id, kind, year, raw, locator) "
                        "VALUES (1, 'performance', 2023, '2023년 실적', '')")
    dialog = _KindDialog(view, docrole.OTHER, {"performance": [2023]})

    # 아무것도 건드리지 않으면 연도 변경은 없다.
    assert dialog.result_value() == (docrole.OTHER, {})

    dialog.role.setCurrentIndex(dialog.role.findData(docrole.PERFORMANCE))
    combo = dialog.years["evaluation"]
    combo.setCurrentIndex(combo.findData(2026))
    dialog.years["performance"].setCurrentIndex(1)   # 이 문서에는 없음
    assert dialog.result_value() == (
        docrole.PERFORMANCE, {"performance": None, "evaluation": 2026})


def test_corrected_kind_survives_reanalysis_and_is_shown(view):
    from app.core import docrole, docyears

    docrole.set_role(view.db, 1, docrole.CRITERIA)
    docyears.set_year(view.db, 1, "evaluation", 2026)

    docrole.classify_documents(view.db, [1])
    docyears.mark_documents(view.db, [1])

    assert docrole.roles(view.db, [1])[1]["role"] == docrole.CRITERIA
    assert docyears.years(view.db, [1])[1]["evaluation"] == [2026]

    view._show_detail(1)
    texts = [view.detail.itemAt(i).widget().text()
             for i in range(view.detail.count())
             if hasattr(view.detail.itemAt(i).widget(), "text")]
    assert any("담당자가 정함" in t for t in texts)
    assert any("평가 2026년" in t for t in texts)


# ── 업무 분류별 기본 보기 및 매핑 변경 ─────────────────────────────

def test_default_view_is_work_classification(tmp_path: Path, qapp):
    """업무가 존재할 때 기본 보기는 첫 번째 업무 분류에 속하는 파일이어야 한다."""
    db = Database(tmp_path / "proj.db")
    db.init()
    source_id = db.add_source(tmp_path / "files")
    db.upsert_document(source_id, {
        "path": str(tmp_path / "doc1.hwp"), "filename": "doc1.hwp", "ext": ".hwp", "parse_status": "ok",
    })
    db.upsert_document(source_id, {
        "path": str(tmp_path / "doc2.hwp"), "filename": "doc2.hwp", "ext": ".hwp", "parse_status": "ok",
    })
    db.upsert_document(source_id, {
        "path": str(tmp_path / "doc3.hwp"), "filename": "doc3.hwp", "ext": ".hwp", "parse_status": "ok",
    })
    db.con.execute("INSERT INTO tasks(id, name, confidence) VALUES (1, '행정사무감사', 'high')")
    db.con.execute("INSERT INTO tasks(id, name, confidence) VALUES (2, '예산관리', 'medium')")
    db.con.execute("INSERT INTO task_docs(task_id, doc_id) VALUES (1, 1)")
    db.con.execute("INSERT INTO task_docs(task_id, doc_id) VALUES (2, 2)")
    # doc3은 미분류

    view = DocumentsView(db)
    try:
        # 기본 선택된 업무는 첫 번째 업무(행정사무감사)여야 함
        assert view._selected_task_id == 1
        assert "행정사무감사" in view.banner_title.text()

        # 목록에는 행정사무감사에 속하는 doc1.hwp만 표시됨 (기본 보기 = 업무 분류별 파일)
        shown = [view.table.item(r, 0).text() for r in range(view.table.rowCount())]
        assert shown == ["doc1.hwp"]

        # 사이드바에서 예산관리(task_id=2) 선택 시
        view._selected_task_id = 2
        view.refresh()
        shown_budget = [view.table.item(r, 0).text() for r in range(view.table.rowCount())]
        assert shown_budget == ["doc2.hwp"]
        assert "예산관리" in view.banner_title.text()

        # 전체 문서 선택 시 (3건 모두 표시)
        view._selected_task_id = None
        view.unclassified_only.setChecked(False)
        view.refresh()
        assert view.table.rowCount() == 3
        assert "전체 문서" in view.banner_title.text()
    finally:
        view.setParent(None)
        db.close()


def test_move_document_remaps_task_classification(tmp_path: Path, qapp):
    """AI가 분류한 업무가 틀렸을 때 사용자가 직접 다른 업무로 변경(매핑)할 수 있어야 한다."""
    db = Database(tmp_path / "proj2.db")
    db.init()
    source_id = db.add_source(tmp_path / "files")
    db.upsert_document(source_id, {
        "path": str(tmp_path / "오분류.hwp"), "filename": "오분류.hwp", "ext": ".hwp", "parse_status": "ok",
    })
    db.con.execute("INSERT INTO tasks(id, name, confidence) VALUES (1, '행정사무감사', 'high')")
    db.con.execute("INSERT INTO tasks(id, name, confidence) VALUES (2, '예산관리', 'medium')")
    # AI가 1번 업무(행정사무감사)로 자동 분류했다고 가정
    db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (1, 1, 'ai', 'low')")

    view = DocumentsView(db)
    try:
        # 상세 패널 확인: AI분류 표시 및 '업무 변경' 버튼 존재
        view._show_detail(1)
        buttons = _buttons(view)
        assert "업무 변경" in buttons
        assert "배정 해제" in buttons

        # 업무 변경: 1번(행정사무감사)에서 2번(예산관리)으로 재매핑
        view.db.move_document(from_task=1, to_task=2, doc_id=1)
        view.refresh()

        # 2번 업무(예산관리)에 배정되었는지 확인
        tasks_of_doc = [t["name"] for t in view.db.document_tasks(1)]
        assert tasks_of_doc == ["예산관리"]

        # 상세 패널 갱신 확인
        view._show_detail(1)
        assert any("예산관리" in t for t in _labels(view))
    finally:
        view.setParent(None)
        db.close()


def test_task_move_dialog_execution(tmp_path: Path, qapp):
    """_TaskMoveDialog 대화상자가 업무 목록을 올바르게 표시하고 선택값을 돌려주는지 시험한다."""
    all_tasks = [
        {"id": 1, "name": "행정사무감사", "doc_count": 5},
        {"id": 2, "name": "예산관리", "doc_count": 3},
    ]
    curr_tasks = [{"id": 1, "name": "행정사무감사"}]

    dialog = _TaskMoveDialog(None, "테스트.hwp", curr_tasks, all_tasks)
    assert dialog.list.count() == 2
    # 현재 분류는 비활성 표시
    assert "(현재 분류)" in dialog.list.item(0).text()

    # 다른 업무 선택
    dialog.list.setCurrentRow(1)
    assert dialog._ok.isEnabled()
    items = dialog.list.selectedItems()
    assert items[0].data(Qt.ItemDataRole.UserRole) == 2
