"""오른쪽 판 — 대화로 정리한 내용을 서식에 맞춘 문서로 만들고, 메모를 남긴다.

    [문서 만들기]  업무보고 · 기본 계획서 · 검토보고서 …      ← 서식을 고르면 초안을 엮는다
    [산출물 | 메모]
      산출물: 엮은 문서를 고치고 한글·워드·PDF·엑셀·글 파일로 내보낸다
      메모:   대화의 답을 남기고, 검토한 뒤 지식으로 승격한다

문서는 AI 초안으로 저장된다. 근거를 찾지 못한 항목은 빈칸(확인 필요)으로
남으므로 사람이 채우거나 지워야 한다. 메모의 승격·게시 규칙은 core/notebook.py에 있다.
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ....core import notebook
from ....jobs.studio_task import StudioRunner
from ....studio import outputs, render, templates
from ... import theme
from ...widgets import TabBar, muted_label
from .panel import Panel

NOTE_STATES = {
    "ai_draft": "AI 초안",
    "edited": "수정함",
    "verified": "검토함",
    "stale": "재검토 필요",
    "memo": "직접 쓴 메모",
}
OUTPUTS, MEMOS = "outputs", "memos"
TILE_COLUMNS = 2


class StudioPanel(Panel):
    changed = Signal()

    def __init__(self, db, parent: QWidget | None = None):
        super().__init__("스튜디오", parent, side="right")
        self.db = db
        self.notebook_id: int | None = None
        self.editable = False
        self._signature = None
        self._studio = StudioRunner(db.path, self)
        self._studio.progress.connect(self._on_progress)
        self._studio.done.connect(self._on_composed)

        # ── 문서 만들기 ──
        head = QHBoxLayout()
        head.setSpacing(theme.SP_SM)
        head.addWidget(muted_label("문서 만들기", small=True, wrap=False))
        head.addStretch(1)
        manage = QPushButton("서식 관리")
        manage.setObjectName("Quiet")
        manage.setCursor(Qt.CursorShape.PointingHandCursor)
        manage.clicked.connect(lambda: self._safe(self._manage_templates))
        head.addWidget(manage)
        self.body.addLayout(head)
        self.tiles = QGridLayout()
        self.tiles.setSpacing(theme.SP_SM)
        self.body.addLayout(self.tiles)
        self.tile_buttons: list[QPushButton] = []

        self.progress_box = QWidget()
        progress = QVBoxLayout(self.progress_box)
        progress.setContentsMargins(0, 0, 0, 0)
        progress.setSpacing(theme.SP_XS)
        bar = QProgressBar()
        bar.setObjectName("Thinking")
        bar.setRange(0, 0)
        bar.setTextVisible(False)
        bar.setFixedHeight(3)
        progress.addWidget(bar)
        line = QHBoxLayout()
        self.progress_label = muted_label("", small=True)
        line.addWidget(self.progress_label, 1)
        cancel = QPushButton("취소")
        cancel.setObjectName("Quiet")
        cancel.clicked.connect(self._studio.cancel)
        line.addWidget(cancel)
        progress.addLayout(line)
        self.progress_box.hide()
        self.body.addWidget(self.progress_box)
        self.report = muted_label("", small=True)
        self.report.hide()
        self.body.addWidget(self.report)

        self.tabs = TabBar(((OUTPUTS, "산출물"), (MEMOS, "메모")))
        self.tabs.switched.connect(self._switch)
        self.body.addWidget(self.tabs)
        self.pages = QStackedWidget()
        self.body.addWidget(self.pages, 1)
        self.pages.addWidget(self._build_outputs())
        self.pages.addWidget(self._build_memos())
        self.tabs.select(OUTPUTS)

    # ── 화면 조립 ───────────────────────────────────────────────────
    def _build_outputs(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(theme.SP_SM)
        self.outputs = QListWidget()
        self.outputs.setObjectName("SourceList")
        self.outputs.setMaximumHeight(140)
        self.outputs.currentItemChanged.connect(self._show_output)
        column.addWidget(self.outputs)
        self.output_hint = muted_label(
            "위에서 서식을 고르면 이 노트북의 대화가 인용한 원문 대목으로 문서 초안을 엮습니다. "
            "근거를 찾지 못한 항목은 ‘확인 필요’로 남습니다.", small=True)
        column.addWidget(self.output_hint)
        self.output_state = muted_label("", small=True)
        column.addWidget(self.output_state)
        self.output_editor = QPlainTextEdit()
        self.output_editor.setPlaceholderText("산출물을 고르면 본문을 고칠 수 있습니다.")
        self.output_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        column.addWidget(self.output_editor, 1)
        line = QHBoxLayout()
        line.setSpacing(theme.SP_SM)
        self.output_save = QPushButton("수정 저장")
        self.output_save.clicked.connect(lambda: self._safe(self._save_output))
        line.addWidget(self.output_save)
        self.output_export = QPushButton("파일로 내보내기")
        self.output_export.setObjectName("Primary")
        menu = QMenu(self.output_export)
        for kind, label in render.FILTERS.items():
            menu.addAction(label.split(" (")[0], lambda k=kind: self._safe(lambda: self._export_output(k)))
        self.output_export.setMenu(menu)
        line.addWidget(self.output_export)
        line.addStretch(1)
        self.output_delete = QPushButton("삭제")
        self.output_delete.setObjectName("Quiet")
        self.output_delete.clicked.connect(lambda: self._safe(self._delete_output))
        line.addWidget(self.output_delete)
        column.addLayout(line)
        return page

    def _build_memos(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(theme.SP_SM)
        row = QHBoxLayout()
        row.addStretch(1)
        self.memo_button = QPushButton("메모 추가")
        self.memo_button.setObjectName("Quiet")
        self.memo_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.memo_button.clicked.connect(lambda: self._safe(self._add_memo))
        row.addWidget(self.memo_button)
        column.addLayout(row)
        self.notes = QListWidget()
        self.notes.setObjectName("SourceList")
        self.notes.setMaximumHeight(140)
        self.notes.currentItemChanged.connect(self._show_note)
        column.addWidget(self.notes)
        self.hint = muted_label(
            "대화의 답에서 ‘메모로 저장’을 누르면 여기에 쌓입니다. 저장한 메모는 "
            "AI 초안이며, 원문을 검토한 뒤에 지식으로 승격할 수 있습니다.", small=True)
        column.addWidget(self.hint)
        self.editor = QTextEdit()
        self.editor.setAcceptRichText(False)
        self.editor.setPlaceholderText("메모를 고르면 내용을 고칠 수 있습니다.")
        column.addWidget(self.editor, 1)
        self.actions: list[QPushButton] = []
        for group in ((("수정 저장", self._edit), ("내보내기", self._export)),
                      (("근거 검토 후 지식 승격", self._promote), ("업무 가이드에 게시", self._publish))):
            line = QHBoxLayout()
            line.setSpacing(theme.SP_SM)
            for text, action in group:
                button = QPushButton(text)
                button.clicked.connect(lambda _=False, a=action: self._safe(a))
                line.addWidget(button)
                self.actions.append(button)
            line.addStretch(1)
            column.addLayout(line)
        return page

    def _switch(self, key: str) -> None:
        self.pages.setCurrentIndex(0 if key == OUTPUTS else 1)

    def _build_tiles(self) -> None:
        while self.tiles.count():
            widget = self.tiles.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.tile_buttons = []
        for index, template in enumerate(templates.all_templates(self.db)):
            button = QPushButton(template.title)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(f"{template.description}\n항목: " + " · ".join(s.title for s in template.sections))
            button.clicked.connect(lambda _=False, key=template.id: self._safe(lambda: self._start(key)))
            self.tiles.addWidget(button, index // TILE_COLUMNS, index % TILE_COLUMNS)
            self.tile_buttons.append(button)

    # ── 상태 ────────────────────────────────────────────────────────
    def set_notebook(self, notebook_id: int | None, editable: bool) -> None:
        if notebook_id != self.notebook_id:
            self._studio.cancel()
            self.report.hide()
        self.notebook_id = notebook_id
        self.editable = editable
        self._signature = None
        self.editor.clear()
        self.output_editor.clear()
        self.refresh()

    def _safe(self, action) -> None:
        try:
            action()
        except (ValueError, OSError) as exc:
            QMessageBox.information(self, "스튜디오", str(exc))

    def note_rows(self):
        if self.notebook_id is None:
            return []
        return self.db.con.execute("SELECT * FROM notebook_notes WHERE notebook_id=? ORDER BY id DESC",
                                   (self.notebook_id,)).fetchall()

    def refresh(self, force: bool = False) -> None:
        # 게시한 지식의 원문 대조는 파일 해시를 읽는다. 주기적인 갱신에서는 바깥
        # (NotebooksView.refresh)이 한 번 하고, 여기서는 무언가 바꾼 직후에만 한다.
        if force:
            notebook.refresh_states(self.db)
        notes = self.note_rows()
        made = outputs.outputs(self.db, self.notebook_id) if self.notebook_id is not None else []
        shelf = tuple((t.id, t.title) for t in templates.all_templates(self.db))
        signature = (self.notebook_id, self.editable, shelf,
                     tuple((r["id"], r["state"], r["revision"], r["title"]) for r in notes),
                     tuple((r["id"], r["state"], r["revision"], r["title"]) for r in made))
        if not force and signature == self._signature:
            return
        self._signature = signature
        self._build_tiles()
        self._fill(self.outputs, made, lambda r: f"{r['title']}  ·  {r['doc_type']} · "
                                                 f"{outputs.STATE_LABELS.get(r['state'], r['state'])}")
        self._fill(self.notes, notes, lambda r: f"{r['title']}  ·  {NOTE_STATES.get(r['state'], r['state'])}")
        self.tabs.set_count(OUTPUTS, "산출물", len(made))
        self.tabs.set_count(MEMOS, "메모", len(notes))
        self.hint.setVisible(not notes)
        self.output_hint.setVisible(not made)
        self.memo_button.setEnabled(self.editable)
        self._sync_tiles()
        self._show_note(self.notes.currentItem())
        self._show_output(self.outputs.currentItem())

    @staticmethod
    def _fill(widget: QListWidget, rows, label) -> None:
        chosen = widget.currentItem().data(Qt.ItemDataRole.UserRole) if widget.currentItem() else None
        widget.blockSignals(True)
        widget.clear()
        for row in rows:
            item = QListWidgetItem(label(row))
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            widget.addItem(item)
            if row["id"] == chosen:
                widget.setCurrentItem(item)
        widget.blockSignals(False)

    def _sync_tiles(self) -> None:
        ready = self.editable and not self._studio.running
        for button in self.tile_buttons:
            button.setEnabled(ready)

    # ── 문서 만들기 ─────────────────────────────────────────────────
    def _start(self, template_id: int) -> None:
        book = notebook.require(self.db, self.notebook_id)
        template = templates.get(self.db, template_id)
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{template.title} 만들기")
        layout = QVBoxLayout(dialog)
        layout.addWidget(muted_label(
            "이 노트북의 대화가 인용한 원문 대목만으로 항목을 채웁니다. 근거를 찾지 못한 항목은 "
            "‘확인 필요’로 남깁니다. 항목마다 AI를 한 번씩 불러 1분쯤 걸립니다."))
        layout.addWidget(muted_label("문서 제목", small=True))
        title = QLineEdit(f"{book['title']} {template.title}")
        layout.addWidget(title)
        layout.addWidget(muted_label("추가로 지킬 것 (선택)", small=True))
        request = QLineEdit()
        request.setPlaceholderText("예: 2026년 회차 기준으로, 금액은 천원 단위로")
        layout.addWidget(request)
        layout.addWidget(muted_label("항목 — " + " · ".join(s.title for s in template.sections), small=True))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("초안 만들기")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(520, 300)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.compose(template_id, title.text(), request.text())

    def compose(self, template_id: int, title: str = "", request: str = "") -> None:
        if self.notebook_id is None or not self.editable:
            return
        if self._studio.start(self.notebook_id, template_id, title, request):
            self.report.hide()
            self.progress_label.setText("문서 초안을 엮는 중")
            self.progress_box.show()
            self._sync_tiles()

    def _on_progress(self, done: int, total: int, label: str) -> None:
        self.progress_label.setText(f"{done} / {total} · {label}")

    def _on_composed(self, output_id, message: str) -> None:
        self.progress_box.hide()
        self.report.setText(message)
        self.report.setVisible(bool(message))
        self.refresh(force=True)
        if output_id is not None:
            self.tabs.select(OUTPUTS)
            self._switch(OUTPUTS)
            for index in range(self.outputs.count()):
                if self.outputs.item(index).data(Qt.ItemDataRole.UserRole) == output_id:
                    self.outputs.setCurrentRow(index)
            self.changed.emit()

    def _manage_templates(self) -> None:
        dialog = TemplateDialog(self.db, self)
        dialog.exec()
        self.refresh(force=True)

    # ── 산출물 ──────────────────────────────────────────────────────
    def _output_id(self) -> int | None:
        item = self.outputs.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _output(self):
        key = self._output_id()
        if key is None:
            raise ValueError("산출물을 선택하세요.")
        return outputs.get(self.db, key)

    def _show_output(self, item, *_):
        chosen = item is not None
        self.output_save.setEnabled(chosen and self.editable)
        self.output_delete.setEnabled(chosen and self.editable)
        self.output_export.setEnabled(chosen)
        self.output_editor.setReadOnly(not chosen or not self.editable)
        if not chosen:
            self.output_editor.clear()
            self.output_state.setText("")
            return
        row = self._output()
        value = outputs.payload(row)
        self.output_editor.setPlainText(value["markdown"])
        notes = [value.get("note") or ""]
        if value.get("empty_sections"):
            notes.append(f"근거를 찾지 못한 항목 {value['empty_sections']}곳은 ‘확인 필요’로 남겼습니다.")
        if outputs.stale(self.db, row):
            notes.append("근거로 쓴 원문이 달라졌습니다. 본문을 다시 확인하세요.")
        self.output_state.setText(" ".join(note for note in notes if note))

    def _save_output(self) -> None:
        outputs.update(self.db, self._output()["id"], self.output_editor.toPlainText())
        self.refresh(force=True)
        self.changed.emit()

    def _delete_output(self) -> None:
        row = self._output()
        answer = QMessageBox.question(
            self, "산출물 삭제", f"‘{row['title']}’을(를) 지울까요? 이미 내보낸 파일은 지우지 않습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            outputs.delete(self.db, row["id"])
            self.refresh(force=True)

    def _export_output(self, kind: str) -> None:
        row = self._output()
        if self.editable and self.output_editor.toPlainText() != outputs.payload(row)["markdown"]:
            outputs.update(self.db, row["id"], self.output_editor.toPlainText())
            row = self._output()
        name = "".join(c for c in row["title"] if c not in '<>:"/\\|?*').strip() or f"output-{row['id']}"
        path, _ = QFileDialog.getSaveFileName(
            self, "파일로 내보내기", str(self.db.path.parent / "outputs" / f"{name}.{kind}"), render.FILTERS[kind])
        if not path:
            return
        target = outputs.export(self.db, row["id"], path)
        message = f"저장했습니다 — {target}"
        if kind == "hwpx":
            message += "\n한글에서 열어 모양을 확인하세요. 결재란·쪽 번호 같은 문서 틀은 들어 있지 않습니다."
        self.report.setText(message)
        self.report.show()
        self.refresh(force=True)

    # ── 메모 ────────────────────────────────────────────────────────
    def _note_id(self) -> int | None:
        item = self.notes.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _note(self):
        key = self._note_id()
        if key is None:
            raise ValueError("메모를 선택하세요.")
        return self.db.con.execute("SELECT * FROM notebook_notes WHERE id=?", (key,)).fetchone()

    def _show_note(self, item, *_):
        for button in self.actions:
            button.setEnabled(item is not None and self.editable)
        if item is None:
            self.editor.clear()
            self.editor.setReadOnly(True)
            return
        row = self._note()
        valid = row["kind"] == "memo" or notebook.evidence_current(
            self.db, row["notebook_id"], json.loads(row["evidence"]), check_file=True)
        self.editor.setPlainText(row["body"] if valid
                                 else "근거 연결 또는 원문 판본이 달라졌습니다. 다시 검토하세요.")
        self.editor.setReadOnly(not valid or not self.editable)

    def select_latest(self) -> None:
        """방금 저장한 메모를 편다 — 저장이 됐는지 눈으로 확인할 수 있어야 한다."""
        self.refresh(force=True)
        self.tabs.select(MEMOS)
        self._switch(MEMOS)
        if self.notes.count():
            self.notes.setCurrentRow(0)

    def _add_memo(self) -> None:
        notebook.require(self.db, self.notebook_id)
        title, ok = QInputDialog.getText(self, "메모 추가", "메모 제목")
        if not ok:
            return
        body, ok = QInputDialog.getMultiLineText(self, "메모 추가", "내용")
        if ok:
            notebook.add_memo(self.db, self.notebook_id, title, body)
            self.select_latest()
            self.changed.emit()

    def _edit(self) -> None:
        row = self._note()
        reason, ok = QInputDialog.getText(self, "메모 수정", "수정 이유")
        if ok:
            notebook.edit_note(self.db, row["id"], self.editor.toPlainText(), reason)
            self.refresh(force=True)
            self.changed.emit()

    def _promote(self) -> None:
        row = self._note()
        reviewer, ok = QInputDialog.getText(self, "근거 검토", "검토자 이름 (로컬 기록이며 본인 인증은 아닙니다)")
        if not ok:
            return
        condition, ok = QInputDialog.getMultiLineText(
            self, "적용 조건 확인", "원문을 대조한 적용 대상·회차·기간과 주의사항")
        if ok:
            notebook.promote(self.db, row["id"], reviewer, condition)
            self.refresh(force=True)
            self.changed.emit()

    def _publish(self) -> None:
        row = self._note()
        card = self.db.con.execute(
            "SELECT id FROM knowledge_cards WHERE note_id=? AND state='verified' ORDER BY id DESC LIMIT 1",
            (row["id"],)).fetchone()
        if card is None:
            raise ValueError("현재 판본으로 지식 승격을 먼저 진행하세요.")
        tasks = self.db.tasks()
        if not tasks:
            raise ValueError("게시할 업무가 없습니다.")
        choice, ok = QInputDialog.getItem(
            self, "업무 가이드에 게시", "기존 업무 항목은 유지하며 별도 지식 카드로 게시합니다.",
            [t["name"] for t in tasks], 0, False)
        if ok:
            notebook.publish(self.db, card[0], next(t["id"] for t in tasks if t["name"] == choice))
            self.refresh(force=True)
            self.changed.emit()

    def _export(self) -> None:
        row = self._note()
        path, _ = QFileDialog.getSaveFileName(
            self, "메모 내보내기", str(self.db.path.parent / "outputs" / f"note-{row['id']}.md"),
            "Markdown (*.md)")
        if path:
            notebook.export_note(self.db, row["id"], path)


class TemplateDialog(QDialog):
    """서식 관리 — 항목과 작성 지침을 고치거나 새 서식을 만든다.

    내장 서식은 고치면 사본으로 저장된다. 글꼴·기호 같은 모양은 여기서 다루지
    않는다(모든 서식이 기본 모양을 쓴다).
    """

    def __init__(self, db, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("서식 관리")
        self.resize(720, 480)
        row = QHBoxLayout(self)
        left = QVBoxLayout()
        self.shelf = QListWidget()
        self.shelf.currentItemChanged.connect(self._show)
        left.addWidget(self.shelf, 1)
        new = QPushButton("새 서식")
        new.clicked.connect(self._new)
        left.addWidget(new)
        row.addLayout(left, 1)

        form = QVBoxLayout()
        form.addWidget(muted_label("서식 이름", small=True))
        self.name = QLineEdit()
        form.addWidget(self.name)
        form.addWidget(muted_label("문서 종류", small=True))
        self.kind = QLineEdit()
        form.addWidget(self.kind)
        form.addWidget(muted_label("항목 — 한 줄에 하나. ‘항목 제목 | 이 항목에 담을 것’", small=True))
        self.sections = QPlainTextEdit()
        form.addWidget(self.sections, 1)
        form.addWidget(muted_label("작성 지침", small=True))
        self.guidance = QLineEdit()
        form.addWidget(self.guidance)
        self.state = muted_label("", small=True)
        form.addWidget(self.state)
        buttons = QHBoxLayout()
        self.save_button = QPushButton("저장")
        self.save_button.setObjectName("Primary")
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.save_button)
        self.delete_button = QPushButton("삭제")
        self.delete_button.clicked.connect(self._delete)
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        form.addLayout(buttons)
        row.addLayout(form, 2)
        self._current: templates.Template | None = None
        self._reload()

    def _reload(self, select: int | None = None) -> None:
        self.shelf.blockSignals(True)
        self.shelf.clear()
        for template in templates.all_templates(self.db):
            item = QListWidgetItem(template.title + ("  · 내장" if template.builtin else ""))
            item.setData(Qt.ItemDataRole.UserRole, template.id)
            self.shelf.addItem(item)
            if template.id == select:
                self.shelf.setCurrentItem(item)
        self.shelf.blockSignals(False)
        if self.shelf.currentItem() is None and self.shelf.count():
            self.shelf.setCurrentRow(0)
        self._show(self.shelf.currentItem())

    def _show(self, item, *_):
        if item is None:
            return
        self._load(templates.get(self.db, item.data(Qt.ItemDataRole.UserRole)))

    def _load(self, template: templates.Template) -> None:
        self._current = template
        self.name.setText(template.title)
        self.kind.setText(template.doc_type)
        self.sections.setPlainText("\n".join(
            f"{s.title} | {s.instruction}" if s.instruction else s.title for s in template.sections))
        self.guidance.setText(template.guidance)
        self.delete_button.setEnabled(template.id is not None and not template.builtin)
        self.state.setText("내장 서식입니다. 고쳐서 저장하면 사본이 만들어집니다." if template.builtin else "")

    def _new(self) -> None:
        self.shelf.clearSelection()
        self._load(templates.Template("새 서식", "기타", sections=[templates.Section("1. 개요")]))

    def _save(self) -> None:
        template = self._current or templates.Template("", "기타")
        template.title, template.doc_type = self.name.text(), self.kind.text()
        template.guidance = self.guidance.text()
        template.sections = []
        for line in self.sections.toPlainText().splitlines():
            title, _, instruction = line.partition("|")
            if title.strip():
                template.sections.append(templates.Section(title.strip(), instruction.strip()))
        try:
            key = templates.save(self.db, template)
        except ValueError as exc:
            self.state.setText(str(exc))
            return
        self._reload(key)
        self.state.setText("저장했습니다.")

    def _delete(self) -> None:
        if self._current is None or self._current.id is None:
            return
        try:
            templates.delete(self.db, self._current.id)
        except ValueError as exc:
            self.state.setText(str(exc))
            return
        self._reload()
