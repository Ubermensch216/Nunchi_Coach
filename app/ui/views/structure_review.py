"""업무 체계 확인 대화상자 (S4).

분석을 막지 않는다. 업무 목록을 한 번 보고 이름 고치기·합치기·'업무 아님'을 한 뒤
확정하거나, '나중에 확인'으로 제안 그대로 둔다. 고친 내용은 업무 화면의 교정과 같은
기능으로 남는다. 나누기는 문서를 골라야 하므로 업무 화면에서 한다.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ...ai import classify_units
from ...core import charter, structure_review
from .. import theme
from ..widgets import muted_label, view_title

COLUMNS = ("업무", "세운 단서", "문서", "자료량")


class StructureReviewDialog(QDialog):
    """닫힐 때 changed가 참이면 업무 목록이 바뀐 것이다(다음 분석이 바뀐 업무를 다시 본다)."""

    handover_folder_requested = Signal()

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.changed = False
        self.setWindowTitle("업무 체계 확인")
        self.resize(720, 560)
        layout = QVBoxLayout(self)
        layout.setSpacing(theme.SP_MD)
        layout.addWidget(view_title("업무 목록을 한 번 확인하세요"))
        layout.addWidget(muted_label(
            "분장표·공문·폴더·본문에서 찾은 업무입니다. 이름이 어색하거나 한 일이 둘로 갈렸으면 "
            "여기서 고치세요. 확인하지 않아도 분석은 제안대로 끝까지 진행됩니다. "
            "업무를 나누려면 업무 화면에서 문서를 골라 나누세요."))

        # 분장표에 과 전체 담당자가 섞여 있고 전임자 이름을 모르면, 누구의 업무인지 여기서 고른다.
        self.owner_row = QHBoxLayout()
        self.owner_row.addWidget(muted_label(
            "업무분장표에 여러 담당자의 업무가 함께 있습니다. 전임자를 고르면 다음 분석부터 그 사람의 업무만 씁니다."), 1)
        self.owner = QComboBox()
        self.owner.addItem("담당자 고르기", "")
        for name in charter.owners(db):
            self.owner.addItem(name, name)
        self.owner.currentIndexChanged.connect(lambda _index: self.choose_owner(self.owner.currentData()))
        self.owner_row.addWidget(self.owner)
        layout.addLayout(self.owner_row)
        self._sync_owner()

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        layout.addWidget(self.table, 1)

        edits = QHBoxLayout()
        self.rename_button = QPushButton("이름 고치기")
        self.rename_button.clicked.connect(self._rename)
        self.merge_button = QPushButton("다른 업무에 합치기")
        self.merge_button.clicked.connect(self._merge)
        self.not_task_button = QPushButton("업무 아님")
        self.not_task_button.clicked.connect(self._not_a_task)
        for button in (self.rename_button, self.merge_button, self.not_task_button):
            edits.addWidget(button)
        edits.addStretch(1)
        # 사용자가 업무 분장을 적었다면 자료가 어느 항목으로 나뉘었는지 확인한다.
        self.classification_button = QPushButton("자료 분류 확인…")
        self.classification_button.setVisible(bool(classify_units.assignments(db)))
        self.classification_button.clicked.connect(self._open_classification)
        edits.addWidget(self.classification_button)
        layout.addLayout(edits)

        foot = QHBoxLayout()
        foot.addStretch(1)
        self.later_button = QPushButton("나중에 확인")
        self.later_button.clicked.connect(self._later)
        foot.addWidget(self.later_button)
        self.confirm_button = QPushButton("이대로 확정")
        self.confirm_button.setObjectName("Primary")
        self.confirm_button.clicked.connect(self._confirm)
        foot.addWidget(self.confirm_button)
        layout.addLayout(foot)
        self.refresh()

    # ── 목록 ────────────────────────────────────────────────────────
    def refresh(self) -> None:
        self.rows = structure_review.rows(self.db)
        most = max((row.doc_count for row in self.rows), default=0) or 1
        self.table.setRowCount(len(self.rows))
        for index, row in enumerate(self.rows):
            name = QTableWidgetItem(row.name + (" (참고 묶음)" if row.bundle else ""))
            name.setData(Qt.ItemDataRole.UserRole, row.task_id)
            self.table.setItem(index, 0, name)
            self.table.setItem(index, 1, QTableWidgetItem(row.basis))
            count = QTableWidgetItem(f"{row.doc_count:,}건")
            count.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(index, 2, count)
            bar = QProgressBar()
            bar.setRange(0, most)
            bar.setValue(row.doc_count)
            bar.setTextVisible(False)
            bar.setFixedHeight(8)
            bar.setFixedWidth(120)
            self.table.setCellWidget(index, 3, bar)
        self._sync_buttons()

    def _selected(self) -> structure_review.Row | None:
        index = self.table.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) and self.table.selectedItems() else None

    def select_task(self, task_id: int) -> None:
        for index, row in enumerate(self.rows):
            if row.task_id == task_id:
                self.table.selectRow(index)
                return

    def _sync_buttons(self) -> None:
        picked = self._selected() is not None
        for button in (self.rename_button, self.merge_button, self.not_task_button):
            button.setEnabled(picked)

    # ── 교정 (업무 화면과 같은 기능) ────────────────────────────────
    def _rename(self) -> None:
        row = self._selected()
        if row is None:
            return
        name, ok = QInputDialog.getText(self, "이름 고치기", "업무 이름", text=row.name)
        self.rename(row.task_id, name if ok else "")

    def rename(self, task_id: int, name: str) -> bool:
        name = " ".join(name.split())
        current = self.db.task(task_id)
        if not name or current is None or name == current["name"]:
            return False
        if not self.db.rename_task(task_id, name):
            QMessageBox.information(self, "이름 고치기", f"'{name}' 업무가 이미 있습니다. 다른 이름을 쓰세요.")
            return False
        self._edited()
        return True

    def _merge(self) -> None:
        row = self._selected()
        if row is None:
            return
        others = [other for other in self.rows if other.task_id != row.task_id]
        if not others:
            return
        choice, ok = QInputDialog.getItem(
            self, "다른 업무에 합치기",
            f"'{row.name}'을(를) 어느 업무에 합칠까요?\n문서가 모두 그쪽으로 옮겨집니다.",
            [other.name for other in others], 0, False)
        if ok:
            self.merge(row.task_id, next(o.task_id for o in others if o.name == choice))

    def merge(self, source_id: int, target_id: int) -> bool:
        if not self.db.merge_tasks(source_id, target_id):
            return False
        self._edited()
        return True

    def _not_a_task(self) -> None:
        row = self._selected()
        if row is None:
            return
        answer = QMessageBox.question(
            self, "업무 아님",
            f"'{row.name}'을(를) 업무 목록에서 내릴까요?\n문서는 그대로 남고, 다시 분석해도 이 판단은 유지됩니다.")
        if answer == QMessageBox.StandardButton.Yes:
            self.not_a_task(row.task_id)

    def not_a_task(self, task_id: int) -> None:
        self.db.mark_not_a_task(task_id)
        self._edited()

    def _sync_owner(self) -> None:
        shown = charter.needs_owner_choice(self.db)
        for index in range(self.owner_row.count()):
            widget = self.owner_row.itemAt(index).widget()
            if widget is not None:
                widget.setVisible(shown)

    def choose_owner(self, name: str) -> None:
        """전임자를 정한다. 분장 항목은 다음 분석이 그 사람의 행으로 다시 뽑는다."""
        if not name:
            return
        charter.set_predecessor(self.db, name)
        self.changed = True
        self._sync_owner()

    def _open_classification(self) -> None:
        from .classification_review import ClassificationReviewDialog
        dialog = ClassificationReviewDialog(self.db, self)
        dialog.exec()
        if dialog.changed:
            self.changed = True

    def _edited(self) -> None:
        self.changed = True
        self.refresh()

    # ── 마치기 ──────────────────────────────────────────────────────
    def _confirm(self) -> None:
        structure_review.confirm(self.db)
        answer = QMessageBox.question(
            self, "사본 인계 폴더",
            "확정한 업무 체계대로 사본 인계 폴더를 지금 만들까요?\n"
            "원본은 그대로 두고, 업무·연도별로 사본만 복사합니다. 나중에 인수인계 화면에서 만들 수도 있습니다.")
        if answer == QMessageBox.StandardButton.Yes:
            self.handover_folder_requested.emit()
        self.accept()

    def _later(self) -> None:
        structure_review.later(self.db)
        self.reject()
