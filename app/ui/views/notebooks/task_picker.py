"""눈치코치가 파악한 업무에서 노트북 자료를 고르는 창.

    ▾ ☑ 행정사무감사 자료 제출      ◐ 자료에서 추정 · 문서 12건
        ☑ 업무 정리본 — 눈치코치가 정리한 설명·주기·처리 순서·확인할 것
        ☑ 2025 행정사무감사 요구자료 제출.hwpx
        ☐ 2024 행정사무감사 계획.pdf

업무를 고르면 정리본과 그 업무의 문서가 함께 골라지고, 펼쳐서 하나씩 뺄 수 있다.
이 창은 더하기만 한다 — 이미 넣은 것은 고른 채로 잠겨 있고, 빼는 일은 출처
목록의 '연결 해제'가 한다.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ....core import notebook_tasks
from ... import theme
from ...widgets import muted_label

BRIEF_LABEL = "업무 정리본 — 눈치코치가 정리한 설명·주기·처리 순서·확인할 것"
_KIND = Qt.ItemDataRole.UserRole          # "brief" | "doc"
_KEY = Qt.ItemDataRole.UserRole + 1       # 업무 번호 | 문서 번호
_CHECKED, _UNCHECKED = Qt.CheckState.Checked, Qt.CheckState.Unchecked


class TaskSourceDialog(QDialog):
    def __init__(self, db, linked_docs: set[int], linked_tasks: set[int],
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("눈치코치가 파악한 업무에서 고르기")
        layout = QVBoxLayout(self)
        layout.setSpacing(theme.SP_SM)
        layout.addWidget(muted_label(
            "업무를 고르면 눈치코치가 정리한 내용(업무 정리본)과 그 업무의 문서가 노트북 자료로 "
            "들어갑니다. 정리본은 원문이 아니라 자료에서 읽어 정리한 글입니다 — 펼쳐서 필요한 것만 "
            "고를 수 있습니다."))

        row = QHBoxLayout()
        row.setSpacing(theme.SP_SM)
        self.search = QLineEdit()
        self.search.setPlaceholderText("업무 이름이나 파일 이름으로 찾기")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._narrow)
        row.addWidget(self.search, 1)
        self.all_button = QPushButton("모든 업무 선택")
        self.all_button.setObjectName("Quiet")
        self.all_button.clicked.connect(lambda: self._check_visible(True))
        row.addWidget(self.all_button)
        self.none_button = QPushButton("선택 해제")
        self.none_button.setObjectName("Quiet")
        self.none_button.clicked.connect(lambda: self._check_visible(False))
        row.addWidget(self.none_button)
        layout.addLayout(row)

        self.tree = QTreeWidget()
        self.tree.setObjectName("SourceList")
        self.tree.setHeaderHidden(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.tree, 1)

        self.summary = muted_label("", small=True)
        layout.addWidget(self.summary)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("자료로 추가")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.resize(640, 560)

        tasks = notebook_tasks.choices(db)
        self.tree.blockSignals(True)
        for task in tasks:
            self._add_task(task, linked_docs, linked_tasks)
        self.tree.blockSignals(False)
        self.tree.itemChanged.connect(lambda *_: self._sync())
        if not tasks:
            self.tree.hide()
            self.search.setEnabled(False)
            self.all_button.hide()
            self.none_button.hide()
            self.summary.setText("아직 파악한 업무가 없습니다. 자료 폴더의 분석이 끝나면 여기에 나타납니다.")
        self._sync()

    def _add_task(self, task: dict, linked_docs: set[int], linked_tasks: set[int]) -> None:
        top = QTreeWidgetItem(self.tree)
        top.setText(0, f"{task['name']}    {task['state']} · 문서 {len(task['documents'])}건")
        top.setToolTip(0, "\n".join(part for part in (task["name"], task["description"]) if part))
        top.setFlags(top.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
        children = [("brief", task["id"], BRIEF_LABEL, task["id"] in linked_tasks)]
        children += [("doc", doc["id"], doc["filename"], doc["id"] in linked_docs)
                     for doc in task["documents"]]
        for kind, key, text, linked in children:
            child = QTreeWidgetItem(top)
            child.setText(0, text + ("  ·  이미 추가함" if linked else ""))
            child.setToolTip(0, text)
            child.setData(0, _KIND, kind)
            child.setData(0, _KEY, key)
            child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            child.setCheckState(0, _CHECKED if linked else _UNCHECKED)
            if linked:
                child.setDisabled(True)

    def _children(self, visible_only: bool = False):
        for index in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(index)
            if visible_only and top.isHidden():
                continue
            for position in range(top.childCount()):
                yield top.child(position)

    def _check_visible(self, checked: bool) -> None:
        self.tree.blockSignals(True)
        for child in self._children(visible_only=True):
            if not child.isDisabled():
                child.setCheckState(0, _CHECKED if checked else _UNCHECKED)
        self.tree.blockSignals(False)
        # 신호를 막은 동안에는 업무 줄의 세 갈래 표시가 따라오지 않는다.
        for index in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(index)
            states = {top.child(i).checkState(0) for i in range(top.childCount())}
            self.tree.blockSignals(True)
            top.setCheckState(0, states.pop() if len(states) == 1 else Qt.CheckState.PartiallyChecked)
            self.tree.blockSignals(False)
        self.tree.viewport().update()
        self._sync()

    def _narrow(self, text: str) -> None:
        needle = text.strip().lower()
        for index in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(index)
            names = [top.text(0)] + [top.child(i).text(0) for i in range(top.childCount())]
            top.setHidden(bool(needle) and not any(needle in name.lower() for name in names))

    def picked(self) -> tuple[list[int], list[int]]:
        """(정리본을 넣을 업무, 연결할 문서). 이미 넣은 것은 빼고 돌려준다."""
        tasks: list[int] = []
        documents: list[int] = []
        for child in self._children():
            if child.isDisabled() or child.checkState(0) != _CHECKED:
                continue
            key = child.data(0, _KEY)
            target = tasks if child.data(0, _KIND) == "brief" else documents
            if key not in target:
                target.append(key)
        return tasks, documents

    def _sync(self) -> None:
        tasks, documents = self.picked()
        if self.tree.isHidden():
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            return
        parts = [text for text in (f"업무 정리본 {len(tasks)}건" if tasks else "",
                                   f"문서 {len(documents)}건" if documents else "") if text]
        self.summary.setText(" · ".join(parts) + "을 추가합니다." if parts
                             else "노트북에 넣을 업무를 고르세요.")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(parts))
