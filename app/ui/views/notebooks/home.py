"""노트북 홈 — 노트북을 카드로 늘어놓고 하나를 골라 들어간다.

    내 노트북                          [검색] [격자|목록] [+ 새 노트북]
    ┌───────────┐ ┌───────────┐ ┌───────────┐
    │ ▣       •••│ │ ▣       •••│ │ ▣       •••│
    │ 이름       │ │ 이름       │ │ 이름       │
    │ 날짜·자료 N│ │ 날짜·자료 N│ │ 날짜·자료 N│
    └───────────┘ └───────────┘ └───────────┘

표지는 이모지가 아니라 이름 첫 글자를 담은 색 판이다. 이모지는 OS가 제 색으로
칠하고 글꼴에 따라 줄 높이를 흔든다(widgets/common.py doc_icon과 같은 이유).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ....core import notebook
from ... import theme
from ...widgets import (
    MORE,
    Badge,
    ElidedLabel,
    EmptyState,
    FlowGrid,
    ListRow,
    Page,
    center_column,
    header_margins,
    muted_label,
    view_title,
)

# core/notebook.py ICONS의 이름 → 표지 색. 상태 색과 겹치지 않는 계열만 쓴다.
COVER_COLORS = {
    "blue": "#3F74B8",
    "indigo": "#5A62C4",
    "violet": "#8259B8",
    "teal": "#2B8FA3",
    "slate": "#5F7F96",
    "plum": "#A0589A",
}
COVER_LABELS = {
    "blue": "청", "indigo": "남보라", "violet": "보라",
    "teal": "청록", "slate": "회청", "plum": "자주",
}
CARD_MIN_W = 240
CARD_H = 148
GRID, LIST = "grid", "list"


def cover(title: str, icon: str, size: int = 40) -> QLabel:
    """이름 첫 글자를 담은 색 판."""
    mark = QLabel((title.strip() or "?")[0])
    mark.setObjectName("NotebookCover")
    mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
    mark.setFixedSize(size, size)
    color = COVER_COLORS.get(icon) or COVER_COLORS["blue"]
    mark.setStyleSheet(
        f"background: {color}; color: #FFFFFF; border-radius: {theme.RADIUS}px; "
        f"font-weight: 700; font-size: {int(size * 0.45)}px;"
    )
    return mark


def card_meta(row) -> str:
    """`2026. 8. 21. · 자료 2건`."""
    stamp = (row["touched_at"] or row["created_at"] or "")[:10]
    pieces = stamp.split("-")
    when = f"{pieces[0]}. {int(pieces[1])}. {int(pieces[2])}." if len(pieces) == 3 else ""
    return " · ".join(part for part in (when, f"자료 {row['source_count']}건") if part)


class NotebookCard(QFrame):
    """노트북 한 권. 판 전체가 여는 자리이고 •••만 따로 메뉴를 연다."""

    opened = Signal(int)
    menu_requested = Signal(int, object)   # (노트북 id, 메뉴를 띄울 단추)

    def __init__(self, row, parent: QWidget | None = None):
        super().__init__(parent)
        self.notebook_id = row["id"]
        self.setObjectName("NotebookCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedHeight(CARD_H)
        self.setAccessibleName(f"노트북 {row['title']}")

        column = QVBoxLayout(self)
        column.setContentsMargins(theme.SP_LG, theme.SP_LG, theme.SP_MD, theme.SP_LG)
        column.setSpacing(theme.SP_SM)

        top = QHBoxLayout()
        top.setSpacing(theme.SP_SM)
        top.addWidget(cover(row["title"], row["icon"]))
        if row["archived"]:
            top.addWidget(Badge("보관됨"), 0, Qt.AlignmentFlag.AlignVCenter)
        top.addStretch(1)
        self.more = QPushButton(MORE)
        self.more.setObjectName("IconButton")
        self.more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more.setToolTip("이름 변경 · 표지 색 · 복제 · 보관 · 삭제")
        self.more.setAccessibleName(f"{row['title']} 메뉴")
        self.more.clicked.connect(lambda: self.menu_requested.emit(self.notebook_id, self.more))
        top.addWidget(self.more, 0, Qt.AlignmentFlag.AlignTop)
        column.addLayout(top)
        column.addStretch(1)

        title = ElidedLabel(row["title"])
        title.setObjectName("NotebookCardTitle")
        column.addWidget(title)
        column.addWidget(muted_label(card_meta(row), small=True, wrap=False))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 — Qt 규약
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.opened.emit(self.notebook_id)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 — Qt 규약
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.opened.emit(self.notebook_id)
            return
        super().keyPressEvent(event)


class NotebookHome(QWidget):
    opened = Signal(int)

    def __init__(self, db, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self._mode = self.db.get_meta("notebook_home_mode") or GRID
        self._signature = None
        self.cards: list[NotebookCard] = []
        self.setObjectName("Canvas")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setObjectName("ViewHeader")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        band = QVBoxLayout(header)
        band.setContentsMargins(*header_margins(theme.SP_LG, theme.SP_LG))
        row, column = center_column(theme.CONTENT_MAX_W)
        band.addLayout(row)

        bar = QHBoxLayout()
        bar.setSpacing(theme.SP_SM)
        bar.addWidget(view_title("내 노트북"))
        bar.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("노트북 검색")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(240)
        self.search.textChanged.connect(self.refresh)
        bar.addWidget(self.search)
        self.archived = QCheckBox("보관한 노트북 포함")
        self.archived.toggled.connect(self.refresh)
        bar.addWidget(self.archived)
        self.mode_button = QPushButton()
        self.mode_button.setObjectName("Quiet")
        self.mode_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mode_button.clicked.connect(self._toggle_mode)
        bar.addWidget(self.mode_button)
        self.create_button = QPushButton("+ 새 노트북")
        self.create_button.setObjectName("Primary")
        self.create_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.create_button.clicked.connect(self._create)
        bar.addWidget(self.create_button)
        column.addLayout(bar)
        root.addWidget(header)

        self.page = Page()
        root.addWidget(self.page, 1)
        self.refresh()

    # ── 그리기 ──────────────────────────────────────────────────────
    def rows(self):
        needle = self.search.text().strip().lower()
        return [row for row in notebook.overview(self.db, self.archived.isChecked())
                if needle in row["title"].lower()]

    def refresh(self, *_):
        self.mode_button.setText("목록으로 보기" if self._mode == GRID else "격자로 보기")
        rows = self.rows()
        # 분석이 도는 동안 셸이 1.5초마다 부른다. 달라진 것이 없으면 카드를
        # 다시 짓지 않는다 — 열려 있던 메뉴와 초점이 사라진다.
        signature = (self._mode, self.search.text(), tuple(
            (r["id"], r["title"], r["icon"], r["archived"], r["source_count"], r["touched_at"]) for r in rows))
        if signature == self._signature:
            return
        self._signature = signature
        column = self.page.reset()
        self.cards = []
        if not rows:
            if self.search.text().strip():
                column.addWidget(EmptyState("찾는 노트북이 없습니다", "검색어를 바꾸거나 지워 보세요."))
            else:
                column.addWidget(EmptyState(
                    "아직 노트북이 없습니다",
                    "노트북은 관련 자료만 모아 묻고 정리하는 작업 공간입니다. "
                    "자료를 고르고, 그 자료만 근거로 대화하고, 정리한 내용을 문서로 남깁니다.",
                    "+ 새 노트북", self._create))
            return
        if self._mode == GRID:
            grid = FlowGrid(min_card_width=CARD_MIN_W)
            for row in rows:
                card = NotebookCard(row)
                card.opened.connect(self.opened.emit)
                card.menu_requested.connect(self._menu)
                grid.add_card(card)
                self.cards.append(card)
            # 위로 붙인다. 남는 세로 공간을 격자가 받으면 줄 사이가 벌어진다.
            column.addWidget(grid, 0, Qt.AlignmentFlag.AlignTop)
            return
        for row in rows:
            column.addWidget(self._list_row(row))

    def _list_row(self, row) -> QWidget:
        line = ListRow()
        line.setCursor(Qt.CursorShape.PointingHandCursor)
        line.row.addWidget(cover(row["title"], row["icon"], 28))
        name = QPushButton(row["title"])
        name.setObjectName("RowTitle")
        name.setCursor(Qt.CursorShape.PointingHandCursor)
        name.clicked.connect(lambda _=False, key=row["id"]: self.opened.emit(key))
        line.row.addWidget(name)
        if row["archived"]:
            line.row.addWidget(Badge("보관됨"))
        line.row.addStretch(1)
        line.row.addWidget(muted_label(card_meta(row), small=True, wrap=False))
        more = QPushButton(MORE)
        more.setObjectName("IconButton")
        more.setCursor(Qt.CursorShape.PointingHandCursor)
        more.setAccessibleName(f"{row['title']} 메뉴")
        more.clicked.connect(lambda _=False, key=row["id"], anchor=more: self._menu(key, anchor))
        line.row.addWidget(more)
        return line

    def _toggle_mode(self):
        self._mode = LIST if self._mode == GRID else GRID
        self.db.set_meta("notebook_home_mode", self._mode)
        self.refresh()

    # ── 동작 ────────────────────────────────────────────────────────
    def _safe(self, action):
        try:
            action()
        except (ValueError, OSError) as exc:
            QMessageBox.information(self, "노트북", str(exc))
        self.refresh()

    def _create(self):
        # 이름을 먼저 묻지 않는다. 무엇을 담을지는 자료를 넣어 봐야 정해진다 —
        # 기본 이름으로 바로 열고, 이름은 작업 화면 머리줄에서 고친다.
        try:
            key = notebook.create(self.db, notebook.untitled(self.db))
        except ValueError as exc:
            QMessageBox.information(self, "새 노트북", str(exc))
            return
        self.refresh()
        self.opened.emit(key)

    def _menu(self, notebook_id: int, anchor: QWidget):
        row = notebook.require(self.db, notebook_id, active=False)
        menu = QMenu(self)
        menu.addAction("열기", lambda: self.opened.emit(notebook_id))
        if not row["archived"]:
            menu.addAction("이름 변경", lambda: self._rename(notebook_id))
        colors = menu.addMenu("표지 색")
        for key in notebook.ICONS:
            action = colors.addAction(COVER_LABELS[key],
                                      lambda k=key: self._safe(lambda: notebook.set_icon(self.db, notebook_id, k)))
            action.setCheckable(True)
            action.setChecked(row["icon"] == key)
        menu.addAction("복제", lambda: self._safe(lambda: notebook.duplicate(self.db, notebook_id)))
        menu.addSeparator()
        menu.addAction("보관 해제" if row["archived"] else "보관",
                       lambda: self._safe(lambda: notebook.archive(self.db, notebook_id, not row["archived"])))
        menu.addAction("삭제…", lambda: self._delete(notebook_id))
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _rename(self, notebook_id: int):
        row = notebook.require(self.db, notebook_id)
        title, ok = QInputDialog.getText(self, "이름 변경", "노트북 이름", text=row["title"])
        if ok:
            self._safe(lambda: notebook.rename(self.db, notebook_id, title))

    def _delete(self, notebook_id: int):
        row = notebook.require(self.db, notebook_id, active=False)
        counts = self.db.con.execute(
            "SELECT (SELECT COUNT(*) FROM notebook_messages WHERE notebook_id=?) AS messages, "
            "(SELECT COUNT(*) FROM notebook_notes WHERE notebook_id=?) AS notes, "
            "(SELECT COUNT(*) FROM knowledge_cards c JOIN notebook_notes n ON n.id=c.note_id "
            " WHERE n.notebook_id=? AND c.state='published') AS published",
            (notebook_id, notebook_id, notebook_id)).fetchone()
        detail = f"대화 {counts['messages']}건과 메모 {counts['notes']}건이 함께 지워집니다."
        if counts["published"]:
            detail += f" 업무 가이드에 게시한 지식 {counts['published']}건도 사라집니다."
        answer = QMessageBox.question(
            self, "노트북 삭제",
            f"‘{row['title']}’ 노트북을 지울까요?\n\n{detail}\n"
            "연결한 원문 파일과 분석 결과는 지우지 않습니다. 되돌릴 수 없습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self._safe(lambda: notebook.delete(self.db, notebook_id))
