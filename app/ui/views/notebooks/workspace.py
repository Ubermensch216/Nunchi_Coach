"""노트북 작업 화면 — 왼쪽부터 출처 | 대화 | 스튜디오.

    ← 내 노트북   ▣ 노트북 이름 ✎                           [◧] [◨]
    ┌──────────┬──────────────────────────┬──────────┐
    │ 출처     │ 대화                     │ 스튜디오 │
    │ 자료 고르기│ 질문과 답이 쌓인다       │ 메모·산출물│
    │ 원문 읽기 │ [질문 입력]              │          │
    └──────────┴──────────────────────────┴──────────┘

좌우 판은 경계를 끌어 폭을 조절하고 접을 수 있다. 접힌 판은 제자리에 좁은
띠를 남기고, 그 띠의 단추로 다시 편다 — 머리줄 오른쪽의 단추도 같은 일을 한다.
폭과 접힘은 노트북마다 기억한다 — 자료를 오래 읽는 노트북과 정리 위주인
노트북은 쓰는 모양이 다르다.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ....core import notebook
from ... import theme
from ...widgets import Badge, UnknownBlock
from .chat_panel import ChatPanel
from .home import cover
from .panel import Rail, icon_button, set_icon
from .sources_panel import SourcesPanel
from .studio_panel import StudioPanel

DEFAULT_SIZES = [340, 720, 340]
SIDE_MIN_W = 260
# 판의 이름과 붙은 쪽. 그림(icons.py panel-left-*, panel-right-*)이 이 쪽을 따른다.
PANELS = {"sources": ("출처", "left"), "studio": ("스튜디오", "right")}
CHAT_MIN_W = 420


class NotebookWorkspace(QWidget):
    back = Signal()

    def __init__(self, db, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self.notebook_id: int | None = None
        self.setObjectName("Canvas")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setObjectName("ViewHeader")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar = QHBoxLayout(header)
        bar.setContentsMargins(theme.SP_LG, theme.SP_SM, theme.SP_LG, theme.SP_SM)
        bar.setSpacing(theme.SP_SM)
        home = QPushButton("← 내 노트북")
        home.setObjectName("Quiet")
        home.setCursor(Qt.CursorShape.PointingHandCursor)
        home.clicked.connect(self.back.emit)
        bar.addWidget(home)
        self.cover_slot = QHBoxLayout()
        bar.addLayout(self.cover_slot)
        self.title = QPushButton("")
        self.title.setObjectName("CardTitle")
        self.title.setCursor(Qt.CursorShape.PointingHandCursor)
        self.title.setToolTip("눌러서 이름을 바꿉니다")
        self.title.clicked.connect(self._rename)
        bar.addWidget(self.title)
        self.rename_button = icon_button("edit", "노트북 이름 바꾸기", "이름 변경")
        self.rename_button.clicked.connect(self._rename)
        bar.addWidget(self.rename_button)
        self.archived_badge = Badge("보관됨")
        bar.addWidget(self.archived_badge)
        bar.addStretch(1)
        self.toggles: dict[str, QPushButton] = {}
        for key in PANELS:
            button = icon_button(f"panel-{PANELS[key][1]}-close", "", PANELS[key][0])
            button.setCheckable(True)
            button.clicked.connect(lambda checked, k=key: self._set_visible(k, checked))
            bar.addWidget(button)
            self.toggles[key] = button
        root.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(theme.SP_LG, theme.SP_MD, theme.SP_LG, theme.SP_MD)
        body.setSpacing(theme.SP_SM)
        self.banner = UnknownBlock("")
        self.banner.hide()
        body.addWidget(self.banner)

        self.sources = SourcesPanel(db)
        self.chat = ChatPanel(db)
        self.studio = StudioPanel(db)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        for panel, floor in ((self.sources, SIDE_MIN_W), (self.chat, CHAT_MIN_W), (self.studio, SIDE_MIN_W)):
            panel.setMinimumWidth(floor)
            self.splitter.addWidget(panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes(DEFAULT_SIZES)
        self.splitter.splitterMoved.connect(lambda *_: self._save_layout())
        # 접힌 판의 자리. 판이 사라진 바로 그곳에 펴는 단추가 남는다.
        self.rails = {key: Rail(*PANELS[key]) for key in PANELS}
        for key, rail in self.rails.items():
            rail.expand_requested.connect(lambda k=key: self._set_visible(k, True))
            rail.hide()
        board = QHBoxLayout()
        board.setSpacing(theme.SP_SM)
        board.addWidget(self.rails["sources"])
        board.addWidget(self.splitter, 1)
        board.addWidget(self.rails["studio"])
        body.addLayout(board, 1)
        root.addLayout(body, 1)

        self.sources.collapse_requested.connect(lambda: self._set_visible("sources", False))
        self.studio.collapse_requested.connect(lambda: self._set_visible("studio", False))
        self.sources.selection_changed.connect(self._selection_changed)
        self.chat.citation_requested.connect(self.show_citation)
        self.chat.note_saved.connect(self._note_saved)

    # ── 열기 ────────────────────────────────────────────────────────
    def open(self, notebook_id: int) -> None:
        row = notebook.require(self.db, notebook_id, active=False)
        self.notebook_id = notebook_id
        notebook.touch(self.db, notebook_id, opened=True)
        editable = not row["archived"]
        self._sync_header(row)
        self.sources.set_notebook(notebook_id, editable)
        self.chat.set_notebook(notebook_id, editable)
        self.studio.set_notebook(notebook_id, editable)
        self._restore_layout()

    def close_notebook(self) -> None:
        self.notebook_id = None
        self.chat.set_notebook(None, False)
        self.sources.set_notebook(None, False)
        self.studio.set_notebook(None, False)

    def refresh(self) -> None:
        if self.notebook_id is None:
            return
        row = self.db.con.execute("SELECT * FROM notebooks WHERE id=?", (self.notebook_id,)).fetchone()
        if row is None:           # 다른 곳에서 지워졌다
            self.back.emit()
            return
        editable = not row["archived"]
        self._sync_header(row)
        if editable != self.chat.editable:
            self.sources.set_notebook(self.notebook_id, editable)
            self.chat.set_notebook(self.notebook_id, editable)
            self.studio.set_notebook(self.notebook_id, editable)
            return
        self.sources.refresh()
        self.chat.refresh()
        self.studio.refresh()

    def _sync_header(self, row) -> None:
        while self.cover_slot.count():
            widget = self.cover_slot.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.cover_slot.addWidget(cover(row["title"], row["icon"], 28))
        self.title.setText(row["title"])
        self.title.setEnabled(not row["archived"])
        self.rename_button.setVisible(not row["archived"])
        self._paint_icons()
        self.archived_badge.setVisible(bool(row["archived"]))
        if row["archived"]:
            self.banner.setMessage("보관한 노트북입니다. 자료와 대화는 읽을 수 있지만 질문·수정은 "
                                   "보관을 해제한 뒤에 할 수 있습니다(홈의 ••• 메뉴).")
        self.banner.setVisible(bool(row["archived"]))

    def _rename(self) -> None:
        row = notebook.require(self.db, self.notebook_id, active=False)
        title, ok = QInputDialog.getText(self, "이름 변경", "노트북 이름", text=row["title"])
        if not ok:
            return
        try:
            notebook.rename(self.db, self.notebook_id, title)
        except ValueError as exc:
            QMessageBox.information(self, "이름 변경", str(exc))
        self.refresh()

    # ── 판 사이의 연결 ──────────────────────────────────────────────
    def _selection_changed(self) -> None:
        # 근거가 달라졌다. 돌고 있는 질문은 옛 선택으로 답하므로 멈춘다.
        self.chat._runner.cancel()
        self.studio._studio.cancel()
        self.chat.refresh()
        self.studio.refresh(force=True)

    def show_citation(self, citation) -> None:
        self._set_visible("sources", True)
        self.sources.show_citation(citation)

    def _note_saved(self) -> None:
        self._set_visible("studio", True)
        self.studio.select_latest()

    # ── 배치 기억 ───────────────────────────────────────────────────
    def _show_panel(self, key: str, visible: bool) -> None:
        panel = self.sources if key == "sources" else self.studio
        panel.setVisible(visible)
        self.rails[key].setVisible(not visible)
        self.toggles[key].setChecked(visible)
        self._paint_icons()

    def _set_visible(self, key: str, visible: bool) -> None:
        self._show_panel(key, visible)
        self._save_layout()

    def _paint_icons(self) -> None:
        """그림은 색을 그릴 때 받는다. 접힘이 바뀌거나 테마가 갈리면 다시 칠한다."""
        set_icon(self.rename_button, "edit")
        for key, (label, side) in PANELS.items():
            shown = self.toggles[key].isChecked()
            # 단추는 '누르면 일어날 일'을 그린다 — 펴져 있으면 접는 그림, 접혀 있으면 펴는 그림.
            set_icon(self.toggles[key], f"panel-{side}-{'close' if shown else 'open'}")
            self.toggles[key].setToolTip(f"{label} 판 {'접기' if shown else '펴기'}")
            self.rails[key].repaint_icons()
        self.sources.repaint_icons()
        self.studio.repaint_icons()

    def _restore_layout(self) -> None:
        saved = notebook.layout(self.db, self.notebook_id)
        sizes = saved.get("sizes")
        if not (isinstance(sizes, list) and len(sizes) == 3 and all(isinstance(n, int) and n > 0 for n in sizes)):
            sizes = DEFAULT_SIZES
        for key in PANELS:
            self._show_panel(key, saved.get(key, True) is not False)
        self.splitter.setSizes(sizes)

    def _save_layout(self) -> None:
        if self.notebook_id is None:
            return
        saved = notebook.layout(self.db, self.notebook_id)
        sizes = self.splitter.sizes()
        # 접은 판의 폭은 0으로 나온다. 다시 폈을 때 쓸 직전 폭을 지킨다.
        previous = saved.get("sizes") if isinstance(saved.get("sizes"), list) else DEFAULT_SIZES
        merged = [size or (previous[i] if i < len(previous) else DEFAULT_SIZES[i])
                  for i, size in enumerate(sizes)]
        notebook.save_layout(self.db, self.notebook_id, {
            "sizes": merged,
            "sources": self.sources.isVisibleTo(self),
            "studio": self.studio.isVisibleTo(self),
        })
