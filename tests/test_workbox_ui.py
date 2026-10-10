"""준비 작업 창 — 실행·승인·산출물·체크리스트 (행동형 업무 비서 계획서 U1·U2)."""

import json
import os
import time
from datetime import date

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QPushButton

from app.tools import action_eval
from app.ui import theme
from app.ui.views import workbox
from app.ui.views.tasks import TasksView

TODAY = date(2026, 10, 10)
CORPUS = json.loads((action_eval.FIXTURES / "corpus.json").read_text(encoding="utf-8"))["scenarios"][0]


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet())
    return app


@pytest.fixture
def project(tmp_path, qapp):
    source = tmp_path / "자료"
    action_eval.write_corpus(source, CORPUS["documents"])
    db, task_id, by_path = action_eval.analyse(tmp_path / "project", source, CORPUS, TODAY,
                                               CORPUS["documents"])
    yield db, task_id
    db.close()


def spin(qapp, predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert predicate()


def texts(dialog):
    return " ".join(label.text() for label in dialog.findChildren(QLabel))


def buttons(dialog, text):
    return [b for b in dialog.findChildren(QPushButton) if b.text() == text]


def test_run_waits_for_approval_and_shows_the_preliminary_warning(qapp, project):
    db, task_id = project
    dialog = workbox.WorkboxDialog(None, db, task_id, today=TODAY)
    assert "아직 실행한 준비 작업이 없습니다" in texts(dialog)
    try:
        dialog.run_button.click()
        spin(qapp, lambda: not dialog._busy)
        assert db.runs(task_id=task_id)[0]["state"] == "waiting_for_approval"
        text = texts(dialog)
        assert "공식 평가기준이 확인되지 않았습니다" in text and "승인이 필요합니다" in text
        assert len(buttons(dialog, "열기")) == 9 and len(buttons(dialog, "승인하고 올리기")) == 1
        assert db.checklist(db.runs(task_id=task_id)[0]["case_id"]) == []     # 승인 전에는 올리지 않았다
        assert buttons(dialog, "이어서 하기") == []
    finally:
        dialog.close()


def test_approving_registers_the_checklist_and_checking_an_item_is_saved(qapp, project):
    db, task_id = project
    dialog = workbox.WorkboxDialog(None, db, task_id, today=TODAY)
    try:
        dialog.run_button.click()
        spin(qapp, lambda: not dialog._busy)
        buttons(dialog, "승인하고 올리기")[0].click()
        spin(qapp, lambda: not dialog._busy)
        run = db.runs(task_id=task_id)[0]
        assert run["state"] == "completed"
        boxes = dialog.findChildren(QCheckBox)
        assert len(boxes) == len(db.checklist(run["case_id"])) > 5
        boxes[0].setChecked(True)
        assert db.checklist(run["case_id"])[0]["state"] == "done"
        assert buttons(dialog, "승인하고 올리기") == []
    finally:
        dialog.close()


def test_declining_leaves_the_drafts_and_registers_nothing(qapp, project):
    db, task_id = project
    dialog = workbox.WorkboxDialog(None, db, task_id, today=TODAY)
    try:
        dialog.run_button.click()
        spin(qapp, lambda: not dialog._busy)
        buttons(dialog, "올리지 않기")[0].click()
        spin(qapp, lambda: not dialog._busy)
        run = db.runs(task_id=task_id)[0]
        assert run["state"] == "completed" and db.checklist(run["case_id"]) == []
        assert len(db.artifacts(run_id=run["id"])) == 9 and db.run_steps(run["id"])[-1]["state"] == "skipped"
    finally:
        dialog.close()


def test_open_button_asks_the_system_to_open_the_file(qapp, project, no_external_launch):
    db, task_id = project
    dialog = workbox.WorkboxDialog(None, db, task_id, today=TODAY)
    try:
        dialog.run_button.click()
        spin(qapp, lambda: not dialog._busy)
        buttons(dialog, "열기")[0].click()
        assert no_external_launch and no_external_launch[0].endswith((".md", ".xlsx", ".json"))
    finally:
        dialog.close()


def test_work_tab_has_the_prep_button(qapp, project):
    db, task_id = project
    view = TasksView(db)
    view.show_task(task_id) if hasattr(view, "show_task") else None
    assert any(b.text() == "준비 작업" for b in view.findChildren(QPushButton)) or hasattr(view, "_prep_button")
    button = view._prep_button(task_id)
    assert button.text() == "준비 작업" and "발송은 하지 않습니다" in button.toolTip()
