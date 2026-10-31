"""업무노트 — 관련 자료만 모아 묻고, 정리하고, 문서로 남기는 작업 공간.

홈(카드 목록)과 작업 화면(출처 | 대화 | 스튜디오) 두 장을 오간다.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from ....core import notebook
from .home import NotebookHome
from .workspace import NotebookWorkspace

__all__ = ["NotebooksView", "NotebookHome", "NotebookWorkspace"]


class NotebooksView(QWidget):
    go_task = Signal(int)
    def __init__(self, db, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        root.addWidget(self.stack)
        self.home = NotebookHome(db)
        self.workspace = NotebookWorkspace(db)
        self.stack.addWidget(self.home)
        self.stack.addWidget(self.workspace)
        self.home.opened.connect(self.open_notebook)
        self.workspace.back.connect(self.show_home)
        self.workspace.go_task.connect(self.go_task.emit)
        # 셸의 종료 절차가 안쪽 작업도 기다린다(ui/shell.py shutdown).
        self._runner = self.workspace.chat._runner
        self._warmup = self.workspace.chat._warmup
        self._ingest = self.workspace.sources._ingest
        self._studio = self.workspace.studio._studio

    @property
    def notebook_id(self) -> int | None:
        return self.workspace.notebook_id

    def open_notebook(self, notebook_id: int) -> None:
        self.workspace.open(notebook_id)
        self.stack.setCurrentWidget(self.workspace)

    def show_home(self) -> None:
        self.workspace.close_notebook()
        self.home.refresh()
        self.stack.setCurrentWidget(self.home)

    def refresh(self) -> None:
        notebook.refresh_states(self.db)
        if self.stack.currentWidget() is self.workspace and self.workspace.notebook_id is not None:
            self.workspace.refresh()
        else:
            self.home.refresh()
