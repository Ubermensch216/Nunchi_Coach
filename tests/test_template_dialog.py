"""서식 관리의 편집·저장·이동 흐름을 실제 Qt 위젯으로 검증한다."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox, QTableWidgetItem

from app.db import Database
from app.studio import templates
from app.ui import theme
from app.ui.views.notebooks.template_dialog import TemplateDialog


@pytest.fixture
def editor(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet())
    db = Database(tmp_path / "templates.db")
    db.init()
    dialog = TemplateDialog(db)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Discard)
    yield dialog, db, app
    dialog._dirty = False
    dialog.close()
    dialog.deleteLater()
    app.processEvents()
    db.close()


def test_structure_reorder_and_literal_separator_survive_copy_save(editor):
    dialog, db, app = editor
    original = templates.get(db, dialog._current.id)
    dialog.name.setText("분기 실적 보고")
    dialog.sections.setCurrentCell(0, 0)
    dialog.down_button.click()
    dialog.sections.item(1, 1).setText("배경 | 목적을 구분해 작성")
    dialog.guidance.setPlainText("확인된 사실만 씁니다.\n금액은 원 단위로 씁니다.")
    dialog._preview()
    assert original.sections[-1].title in dialog.preview.toPlainText()
    assert dialog._save()
    saved = templates.get(db, dialog._current.id)
    assert not saved.builtin
    assert saved.sections[0].title == original.sections[1].title
    assert saved.sections[1].instruction == "배경 | 목적을 구분해 작성"
    assert "\n" in saved.guidance
    assert templates.get(db, original.id) == original


def test_style_parts_keep_independent_values_and_unexposed_properties(editor):
    dialog, db, app = editor
    original = templates.get(db, dialog._current.id)
    dialog.style_slot.setCurrentIndex(dialog.style_slot.findData("levels.0"))
    dialog.glyph.setText("1.")
    dialog.size.setValue(14.5)
    dialog.lead.setValue(4)
    dialog.style_slot.setCurrentIndex(dialog.style_slot.findData("heading"))
    dialog.font.setCurrentText("휴먼명조")
    dialog.style_slot.setCurrentIndex(dialog.style_slot.findData("levels.0"))
    assert dialog.glyph.text() == "1." and dialog.size.value() == 14.5
    assert dialog._save()
    saved = templates.get(db, dialog._current.id)
    assert saved.format["levels"][0]["key"] == "num"
    assert saved.format["levels"][0]["size"] == 14.5
    assert saved.format["levels"][0]["lead"] == 4
    assert saved.format["heading"]["font"] == "휴먼명조"
    assert saved.format["page"] == original.format["page"]


def test_cancel_discard_and_save_on_selection_change(editor, monkeypatch):
    dialog, db, app = editor
    original_id = dialog._current.id
    dialog.name.setText("저장할 변경")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Cancel)
    dialog.shelf.setCurrentRow(1)
    assert dialog._current.id == original_id
    assert dialog.shelf.currentRow() == 0
    assert dialog.name.text() == "저장할 변경"
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Save)
    target_id = dialog.shelf.item(1).data(Qt.ItemDataRole.UserRole)
    dialog.shelf.setCurrentRow(1)
    assert dialog._current.id == target_id
    assert any(t.title == "저장할 변경" and not t.builtin for t in templates.all_templates(db))
    dialog.name.setText("버릴 변경")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Discard)
    dialog.shelf.setCurrentRow(0)
    assert dialog._current.id == original_id
    assert all(t.title != "버릴 변경" for t in templates.all_templates(db))


def test_failed_save_keeps_editing_and_close_can_be_cancelled(editor, monkeypatch):
    dialog, db, app = editor
    before = len(templates.all_templates(db))
    dialog.name.clear()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Save)
    dialog.shelf.setCurrentRow(1)
    assert dialog.shelf.currentRow() == 0 and dialog._dirty
    assert len(templates.all_templates(db)) == before
    dialog.show()
    app.processEvents()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Cancel)
    dialog.close()
    assert dialog.isVisible()


def test_new_sections_search_filters_and_confirmed_delete(editor, monkeypatch):
    dialog, db, app = editor
    before = len(templates.all_templates(db))
    dialog.new_button.click()
    dialog.name.setText("맞춤 회의록")
    dialog.sections.setRowCount(2)
    dialog.sections.setItem(1, 0, QTableWidgetItem("2. 결정 사항"))
    dialog.sections.setItem(1, 1, QTableWidgetItem("결정된 사항만 적습니다."))
    dialog.sections.setCurrentCell(0, 0)
    dialog.remove_section_button.click()
    assert dialog._titles() == ["2. 결정 사항"]
    assert dialog._save()
    dialog.filter.setCurrentIndex(2)
    visible = [dialog.shelf.item(i) for i in range(dialog.shelf.count()) if not dialog.shelf.item(i).isHidden()]
    assert len(visible) == 1 and "맞춤 회의록" in visible[0].text()
    dialog.search.setText("없는 이름")
    assert not dialog.empty.isHidden()
    dialog.search.clear()
    custom_id = dialog._current.id
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    dialog.delete_button.click()
    assert templates.get(db, custom_id)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    dialog.delete_button.click()
    assert len(templates.all_templates(db)) == before


def test_keyboard_save_commits_the_cell_being_edited(editor):
    dialog, db, app = editor
    dialog.show()
    app.processEvents()
    dialog.sections.setCurrentCell(0, 0)
    dialog.sections.editItem(dialog.sections.item(0, 0))
    app.processEvents()
    field = dialog.sections.findChild(QLineEdit)
    field.setText("편집 중인 새 제목")
    QTest.keyClick(field, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    app.processEvents()
    saved = templates.all_templates(db)[-1]
    assert not saved.builtin
    assert saved.sections[0].title == "편집 중인 새 제목"


def test_minimum_window_keeps_structure_actions_and_optional_guidance_reachable(editor):
    dialog, db, app = editor
    dialog.resize(1080, 660)
    dialog.show()
    app.processEvents()
    page = dialog.editor_stack.currentWidget()
    assert page.verticalScrollBar().maximum() == 0
    assert page.horizontalScrollBar().maximum() == 0
    assert dialog.guidance.isHidden()
    dialog.guidance_button.click()
    assert dialog.guidance.isVisible()
    dialog.guidance.setPlainText("첫째, 사실을 확인합니다.\n둘째, 근거를 적습니다.")
    dialog.guidance_button.click()
    assert dialog.guidance.isHidden()
    assert dialog._save()
    assert templates.get(db, dialog._current.id).guidance.count("\n") == 1
