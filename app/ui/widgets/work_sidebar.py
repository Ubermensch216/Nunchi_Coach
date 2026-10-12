"""좌측 업무 목록 사이드바 위젯 (개편계획서 §13.2 및 §13.4).

업무 상세 작업 공간(2-패널)의 좌측 패널:
- 상단: '← 업무 홈' 복귀 버튼
- 업무 목록 (선택된 업무: 옅은 배경 + 왼쪽 강조선 + 굵은 글자)
- 독립 스크롤, 폭 조절 지원
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QSignalBlocker
from PySide6.QtWidgets import QFrame, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget


class WorkSidebar(QFrame):
    """업무 상세 화면 좌측 네비게이션 패널."""

    task_selected = Signal(int)
    back_to_home = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("WorkSidebar")
        self.setMinimumWidth(200)
        self._tasks: list[dict] = []
        self._current_task_id: int | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 14, 10, 14)
        layout.setSpacing(10)

        # 1. 홈 복귀 버튼
        self.btn_home = QPushButton("←  업무 홈")
        self.btn_home.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_home.clicked.connect(self.back_to_home.emit)
        layout.addWidget(self.btn_home)

        self.search = QLineEdit()
        self.search.setPlaceholderText("업무 검색")
        self.search.setAccessibleName("좌측 업무 검색")
        self.search.textChanged.connect(self._populate_list)
        layout.addWidget(self.search)

        # 2. 업무 목록
        self.list_widget = QListWidget()
        self.list_widget.setWordWrap(True)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_widget.setAccessibleName("업무 목록 · 방향키로 선택")
        self.list_widget.currentItemChanged.connect(lambda item, previous: self._on_item_clicked(item) if item else None)
        layout.addWidget(self.list_widget, 1)

    def set_tasks(self, tasks: list[dict], selected_task_id: int | None = None) -> None:
        changed = self._tasks != [dict(t) for t in tasks]
        self._tasks = [dict(t) for t in tasks]
        self._current_task_id = selected_task_id
        if changed:
            self._populate_list()
        else:
            self.select_task(selected_task_id)

    def select_task(self, task_id: int) -> None:
        self._current_task_id = task_id
        blocker = QSignalBlocker(self.list_widget)
        self.list_widget.setCurrentRow(-1)
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == task_id:
                self.list_widget.setCurrentItem(item)
                break

    def _populate_list(self, *_args) -> None:
        scroll = self.list_widget.verticalScrollBar().value()
        blocker = QSignalBlocker(self.list_widget)
        self.list_widget.clear()
        query = self.search.text().strip().casefold()
        for t in sorted(self._tasks, key=lambda t: t["id"]):
            if query not in (t["name"] + " " + (t.get("description") or "")).casefold():
                continue
            name = t["name"] if "name" in t.keys() else str(t)
            task_id = t["id"] if "id" in t.keys() else None

            item = QListWidgetItem(name)
            item.setToolTip(name)
            item.setData(Qt.ItemDataRole.UserRole, task_id)
            self.list_widget.addItem(item)

            if task_id == self._current_task_id:
                self.list_widget.setCurrentItem(item)

        self.list_widget.verticalScrollBar().setValue(scroll)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        task_id = item.data(Qt.ItemDataRole.UserRole)
        if task_id is not None and task_id != self._current_task_id:
            self._current_task_id = task_id
            self.task_selected.emit(task_id)
