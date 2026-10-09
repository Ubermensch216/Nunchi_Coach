"""선택한 원문으로 탐구하고, 노트와 검토한 지식을 구분하는 작업 공간."""

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QPushButton,
    QLabel, QListWidget, QListWidgetItem, QInputDialog, QMessageBox, QDialog,
    QDialogButtonBox, QTextEdit, QSplitter, QFileDialog)

from ...core import notebook
from ...search.query import QueryScope
from ...search.rag import Answer, Citation
from ..widgets import clear_layout
from .ask import AskView


class NotebookAskView(AskView):
    def __init__(self, db, parent=None):
        self.notebook_id = None
        super().__init__(db, parent)
        self.history.hide()
        self.task_scope.hide()
        self.year_scope.hide()
        for label in self.findChildren(QLabel):
            if label.text() in ("찾는 범위", "자주 찾는 것"):
                label.hide()

    def _chosen_scope(self):
        return QueryScope(notebook_id=self.notebook_id)

    def _readiness(self):
        if self.notebook_id is None:
            return False, "노트북을 만들고 자료를 선택하세요."
        selected = notebook.selected_snapshot(self.db, self.notebook_id)
        return bool(selected), "선택한 원문만 검색합니다. 답변 저장과 근거 검토는 별도 단계입니다."

    def _reload_questions(self):
        clear_layout(self.chip_row)
        self.history.hide()

    def _render_actions(self):
        # 프로젝트 전체 후속 질문과 피드백을 노트북에 섞지 않는다.
        pass

    def _render_reader(self):
        # 프로젝트 전체의 버전·업무 문서 탭은 노트북 범위 밖 자료를 노출할 수 있다.
        clear_layout(self.reader_column)
        self.reader_tabs.hide()
        citation = self._selected_citation()
        if self.notebook_id is None or citation is None:
            return
        if str(citation.doc_id) in notebook.selected_snapshot(self.db, self.notebook_id):
            self._render_source(citation, self._context.get(citation.doc_id, {}))


class NotebooksView(QWidget):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.notebook_id = None
        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("업무 노트북"))
        self.picker = QComboBox()
        self.picker.currentIndexChanged.connect(self._choose)
        bar.addWidget(self.picker, 1)
        for text, action in (("새 노트북", self._create), ("이름 변경", self._rename), ("자료 연결", self._add), ("보관·복원", self._archive)):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, a=action: self._safe(a))
            bar.addWidget(button)
        root.addLayout(bar)
        self.notice = QLabel("개인 로컬 작업 공간 · AI 노트는 초안이며, 지식 승격은 원문과 적용 조건을 검토한 뒤 진행합니다.")
        self.notice.setWordWrap(True)
        root.addWidget(self.notice)
        panes = QSplitter(Qt.Orientation.Horizontal)
        source_pane = QWidget()
        source_column = QVBoxLayout(source_pane)
        source_column.addWidget(QLabel("자료 선택"))
        self.source_list = QListWidget()
        self.source_list.itemChanged.connect(self._select_source)
        source_column.addWidget(self.source_list)
        accept = QPushButton("선택 문서의 새 판본 사용")
        accept.clicked.connect(lambda: self._safe(self._accept))
        source_column.addWidget(accept)
        source_column.addWidget(QLabel("현재 자료의 지난 질문"))
        self.messages = QListWidget()
        self.messages.itemClicked.connect(lambda item: self._safe(lambda: self._show_message(item)))
        source_column.addWidget(self.messages)
        panes.addWidget(source_pane)
        self.ask_view = NotebookAskView(db)
        self.ask_view._runner.done.connect(lambda _: self._update_notes())
        # 셸의 종료 절차가 내부 작업도 기다린다.
        self._runner = self.ask_view._runner
        self._warmup = self.ask_view._warmup
        panes.addWidget(self.ask_view)
        studio = QWidget()
        column = QVBoxLayout(studio)
        column.addWidget(QLabel("연구 노트 · 지식 검토"))
        self.notes = QListWidget()
        self.notes.currentItemChanged.connect(self._show_note)
        column.addWidget(self.notes)
        self.editor = QTextEdit()
        self.editor.setPlaceholderText("답변을 저장한 뒤 내용을 고칠 수 있습니다.")
        column.addWidget(self.editor)
        for text, action in (("답변을 노트로 저장", self._save), ("노트 수정 저장", self._edit),
                             ("근거 검토 후 지식 승격", self._promote), ("업무 가이드에 게시", self._publish),
                             ("노트 내보내기", self._export)):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, a=action: self._safe(a))
            column.addWidget(button)
        panes.addWidget(studio)
        panes.setSizes([230, 850, 300])
        root.addWidget(panes, 1)
        self.refresh()

    def _safe(self, action):
        try:
            action()
        except (ValueError, OSError) as exc:
            QMessageBox.information(self, "노트북", str(exc))

    def refresh(self):
        notebook.refresh_states(self.db)
        chosen = self.notebook_id
        self.picker.blockSignals(True)
        self.picker.clear()
        for row in self.db.con.execute("SELECT * FROM notebooks ORDER BY id DESC"):
            self.picker.addItem(("[보관] " if row["archived"] else "") + row["title"], row["id"])
        if chosen is not None:
            self.picker.setCurrentIndex(self.picker.findData(chosen))
        self.picker.blockSignals(False)
        self._choose()

    def _choose(self, *_):
        chosen = self.picker.currentData()
        if chosen != self.notebook_id:
            self._runner.cancel()
            self.ask_view._answer = None
            self.editor.clear()
        self.notebook_id = chosen
        row = notebook.require(self.db, chosen, active=False) if chosen is not None else None
        self.source_list.setEnabled(bool(row and not row["archived"]))
        self.ask_view.notebook_id = chosen if row and not row["archived"] else None
        self.source_list.blockSignals(True)
        self.source_list.clear()
        if chosen is not None:
            for source in notebook.sources(self.db, chosen):
                state = "원문 변경" if source["hash"] != source["version_hash"] else "원문"
                if not notebook.file_matches(source["path"], source["version_hash"]):
                    state = "원문 변경·재분석 필요"
                if not source["allowed"] or source["missing_since"]:
                    state = "이용 불가"
                item = QListWidgetItem(f"{source['filename']} · {state}")
                item.setData(Qt.ItemDataRole.UserRole, source["doc_id"])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if source["selected"] else Qt.CheckState.Unchecked)
                self.source_list.addItem(item)
        self.source_list.blockSignals(False)
        self.ask_view.refresh()
        self._update_notes()

    def _create(self):
        name, ok = QInputDialog.getText(self, "새 노트북", "이름")
        if ok:
            self.notebook_id = notebook.create(self.db, name)
            self.refresh()

    def _archive(self):
        row = notebook.require(self.db, self.notebook_id, active=False)
        notebook.archive(self.db, self.notebook_id, not row["archived"])
        self.refresh()

    def _rename(self):
        row = notebook.require(self.db, self.notebook_id)
        title, ok = QInputDialog.getText(self, "이름 변경", "노트북 이름", text=row["title"])
        if ok:
            notebook.rename(self.db, self.notebook_id, title)
            self.refresh()

    def _export(self):
        row = self._note()
        path, _ = QFileDialog.getSaveFileName(self, "노트 내보내기", str(self.db.path.parent / "outputs" / f"note-{row['id']}.md"), "Markdown (*.md)")
        if path:
            notebook.export_note(self.db, row["id"], path)

    def _add(self):
        notebook.require(self.db, self.notebook_id)
        dialog = QDialog(self)
        dialog.setWindowTitle("분석한 원문 연결 — 파일은 복사하지 않습니다")
        layout = QVBoxLayout(dialog)
        choices = QListWidget()
        linked = {row["doc_id"] for row in notebook.sources(self.db, self.notebook_id)}
        for row in self.db.con.execute("SELECT id,filename FROM documents WHERE hash IS NOT NULL AND missing_since IS NULL ORDER BY filename"):
            item = QListWidgetItem(f"{row['filename']} · #{row['id']}")
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            item.setCheckState(Qt.CheckState.Checked if row["id"] in linked else Qt.CheckState.Unchecked)
            choices.addItem(item)
        layout.addWidget(choices)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(550, 450)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        picked = {choices.item(i).data(Qt.ItemDataRole.UserRole) for i in range(choices.count())
                  if choices.item(i).checkState() == Qt.CheckState.Checked}
        notebook.add_sources(self.db, self.notebook_id, picked)
        for doc_id in linked - picked:
            notebook.detach(self.db, self.notebook_id, doc_id)
        self.ask_view._answer = None
        self.refresh()

    def _select_source(self, item):
        self._runner.cancel()
        notebook.select(self.db, self.notebook_id, item.data(Qt.ItemDataRole.UserRole),
                        item.checkState() == Qt.CheckState.Checked)
        self.ask_view._answer = None
        self.ask_view.refresh()
        self._update_notes()

    def _accept(self):
        item = self.source_list.currentItem()
        if item:
            notebook.accept_version(self.db, self.notebook_id, item.data(Qt.ItemDataRole.UserRole))
            self.ask_view._answer = None
            self.refresh()

    def _update_notes(self):
        notebook.refresh_states(self.db)
        self.notes.clear()
        self.messages.clear()
        if self.notebook_id is not None:
            book = notebook.require(self.db, self.notebook_id, active=False)
            active = notebook.selected_snapshot(self.db, self.notebook_id) if not book["archived"] else {}
            for message in self.db.con.execute("SELECT * FROM notebook_messages WHERE notebook_id=? ORDER BY id DESC", (self.notebook_id,)):
                if active and json.loads(message["source_snapshot"]) == active:
                    item = QListWidgetItem(message["question"])
                    item.setData(Qt.ItemDataRole.UserRole, message["id"])
                    self.messages.addItem(item)
            for row in self.db.con.execute("SELECT * FROM notebook_notes WHERE notebook_id=? ORDER BY id DESC", (self.notebook_id,)):
                item = QListWidgetItem(f"{row['title']} · {row['state']}")
                item.setData(Qt.ItemDataRole.UserRole, row["id"])
                self.notes.addItem(item)

    def _show_message(self, item):
        from dataclasses import fields
        row = self.db.con.execute("SELECT * FROM notebook_messages WHERE id=? AND notebook_id=?",
                                  (item.data(Qt.ItemDataRole.UserRole), self.notebook_id)).fetchone()
        if row is None or json.loads(row["source_snapshot"]) != notebook.selected_snapshot(self.db, self.notebook_id):
            raise ValueError("자료 선택이나 원문 판본이 달라졌습니다. 다시 질문하세요.")
        keys = {field.name for field in fields(Citation)}
        citations = [Citation(**{key: value for key,value in proof.items() if key in keys})
                     for proof in json.loads(row["citations"])]
        generation = json.loads(row["generation"])
        generation["notebook_message_id"] = row["id"]
        self.ask_view._render_answer(Answer(row["question"], row["answer"], bool(row["withheld"]),
                                          citations=citations, generation=generation, model=row["model"]))
        self.ask_view.input.setText(row["question"])

    def _note(self):
        item = self.notes.currentItem()
        if item is None:
            raise ValueError("노트를 선택하세요.")
        return self.db.con.execute("SELECT * FROM notebook_notes WHERE id=?", (item.data(Qt.ItemDataRole.UserRole),)).fetchone()

    def _show_note(self, item, *_):
        self.editor.clear()
        if item:
            row = self._note()
            valid = notebook.evidence_current(self.db, row["notebook_id"], json.loads(row["evidence"]), check_file=True)
            self.editor.setPlainText(row["body"] if valid else "근거 연결 또는 원문 판본이 달라졌습니다. 다시 검토하세요.")
            self.editor.setReadOnly(not valid)

    def _save(self):
        answer = self.ask_view._answer
        if answer is None:
            raise ValueError("먼저 질문해 답변을 받으세요.")
        notebook.save_note(self.db, self.notebook_id, answer.generation.get("notebook_message_id"), answer.question)
        self._update_notes()

    def _edit(self):
        row = self._note()
        reason, ok = QInputDialog.getText(self, "노트 수정", "수정 이유")
        if ok:
            notebook.edit_note(self.db, row["id"], self.editor.toPlainText(), reason)
            self._update_notes()

    def _promote(self):
        row = self._note()
        reviewer, ok = QInputDialog.getText(self, "근거 검토", "검토자 이름 (로컬 기록이며 본인 인증은 아닙니다)")
        if not ok:
            return
        condition, ok = QInputDialog.getMultiLineText(self, "적용 조건 확인", "원문을 대조한 적용 대상·회차·기간과 주의사항")
        if ok:
            notebook.promote(self.db, row["id"], reviewer, condition)
            self._update_notes()

    def _publish(self):
        row = self._note()
        card = self.db.con.execute("SELECT id FROM knowledge_cards WHERE note_id=? AND state='verified' ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
        if card is None:
            raise ValueError("현재 판본으로 지식 승격을 먼저 진행하세요.")
        tasks = self.db.tasks()
        if not tasks:
            raise ValueError("게시할 업무가 없습니다.")
        choice, ok = QInputDialog.getItem(self, "업무 가이드에 게시", "기존 업무 항목은 유지하며 별도 지식 카드로 게시합니다.", [t["name"] for t in tasks], 0, False)
        if ok:
            notebook.publish(self.db, card[0], next(t["id"] for t in tasks if t["name"] == choice))
            self._update_notes()
