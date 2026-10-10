"""좌측 업무 목록 사이드바 위젯 (개편계획서 §13.2 및 §13.4).

업무 상세 작업 공간(2-패널)의 좌측 패널:
- 상단: '← 업무 홈' 복귀 버튼
- 업무 검색 필터창
- 업무 목록 (선택된 업무: 옅은 배경 + 왼쪽 강조선 + 굵은 글자)
- 독립 스크롤, 폭 조절 지원
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..widgets.common import muted_label
from .task_card import color_for


class WorkSidebar(QFrame):
    """업무 상세 화면 좌측 네비게이션 패널."""

    task_selected = Signal(int)
    back_to_home = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("WorkSidebar")
        self.setMinimumWidth(200)
        self.setMaximumWidth(320)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.setStyleSheet(
            """
            #WorkSidebar {
                background: #F8FAFC;
                border-right: 1px solid #E2E8F0;
            }
            QListWidget {
                background: transparent;
                border: none;
                outline: none;
            }
            QListWidget::item {
                padding: 10px 12px;
                border-radius: 6px;
                margin: 2px 6px;
                color: #334155;
                font-size: 13px;
            }
            QListWidget::item:hover {
                background: #EDF2F7;
            }
            QListWidget::item:selected {
                background: #E0E7FF;
                color: #1E1B4B;
                font-weight: 700;
                border-left: 4px solid #4F46E5;
            }
            """
        )

        self._tasks: list[dict] = []
        self._current_task_id: int | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 14, 10, 14)
        layout.setSpacing(10)

        # 1. 홈 복귀 버튼
        self.btn_home = QPushButton("←  업무 홈")
        self.btn_home.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_home.setStyleSheet(
            """
            QPushButton {
                text-align: left;
                padding: 6px 8px;
                font-weight: 600;
                font-size: 12px;
                color: #475569;
                border: none;
                background: transparent;
            }
            QPushButton:hover {
                color: #0F172A;
            }
            """
        )
        self.btn_home.clicked.connect(self.back_to_home.emit)
        layout.addWidget(self.btn_home)

        # 2. 검색 입력창
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("업무 검색...")
        self.search_input.setStyleSheet(
            """
            QLineEdit {
                padding: 6px 10px;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                background: #FFFFFF;
                font-size: 12px;
            }
            QLineEdit:focus {
                border-color: #6366F1;
            }
            """
        )
        self.search_input.textChanged.connect(self._filter_list)
        layout.addWidget(self.search_input)

        # 3. 업무 목록
        self.list_widget = QListWidget()
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.list_widget, 1)

    def set_tasks(self, tasks: list[dict], selected_task_id: int | None = None) -> None:
        self._tasks = tasks
        self._current_task_id = selected_task_id
        self._populate_list(self.search_input.text())

    def select_task(self, task_id: int) -> None:
        self._current_task_id = task_id
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == task_id:
                self.list_widget.setCurrentItem(item)
                break

    def _populate_list(self, filter_text: str = "") -> None:
        self.list_widget.clear()
        query = filter_text.strip().lower()

        for t in self._tasks:
            name = t["name"] if "name" in t.keys() else str(t)
            task_id = t["id"] if "id" in t.keys() else None
            if query and query not in name.lower():
                continue

            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, task_id)
            self.list_widget.addItem(item)

            if task_id == self._current_task_id:
                self.list_widget.setCurrentItem(item)

    def _filter_list(self, text: str) -> None:
        self._populate_list(text)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        task_id = item.data(Qt.ItemDataRole.UserRole)
        if task_id is not None:
            self._current_task_id = task_id
            self.task_selected.emit(task_id)
