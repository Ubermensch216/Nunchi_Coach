"""오늘 — 눈치코치가 먼저 챙긴 일 (계획서 U3).

질문하기 전에 지금 챙길 일을 먼저 보여 주는 화면이다. 세 가지를 한 화면에 둔다.

    먼저 챙긴 일      제안 카드. 왜 추천했는지 · 어떤 문서를 근거로 했는지 · 무엇을 실행하는지
    진행 중인 수행 건  올해 준비 중인 일과 체크리스트 진행, 승인 대기
    빠진 단계 후보    과거 처리 순서와 대조해 올해 자료에 보이지 않는 단계 (과거 관행)

지키는 것
  · 과거 반복에서 나온 제안은 ‘과거 반복업무 · 올해 일정 미확인’으로 보인다. 올해의
    일정처럼 읽히는 날짜를 쓰지 않는다.
  · 제안을 받아들여도 승인 없이는 아무것도 확정하지 않는다 — 준비 작업 창이 같은 규칙을
    따른다.
  · 아무것도 없을 때는 없다고 말하고, 무엇을 살폈는지를 함께 적는다.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QMenu, QPushButton, QVBoxLayout, QWidget

from ...db import Database
from ...proactive import briefing, recommend
from .. import theme
from ..widgets import (
    Badge, Card, EmptyState, ListRow, Page, SubPanel, center_column, doc_icon, header_margins,
    muted_label, open_original, section_title, view_title,
)

TILES = (("cases", "진행 중인 수행 건"), ("new", "새 제안"), ("due", "확인할 기한"), ("completed", "끝난 실행"))


class TodayView(QWidget):
    open_task = Signal(int)
    changed = Signal()
    go_tasks = Signal()

    def __init__(self, db: Database, parent: QWidget | None = None, today: date | None = None):
        super().__init__(parent)
        self.db = db
        self._today = today
        self.setObjectName("Canvas")
        self.setAutoFillBackground(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QWidget()
        header.setObjectName("ViewHeader")
        header.setAutoFillBackground(True)
        head_row, head_column = center_column(theme.CONTENT_MAX_W)
        head_column.setSpacing(theme.SP_SM)
        frame = QVBoxLayout(header)
        frame.setContentsMargins(*header_margins(theme.SP_XL))
        frame.addLayout(head_row)
        back = QPushButton("← 전체 업무")
        back.setObjectName("Quiet")
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.clicked.connect(self.go_tasks.emit)
        head_column.addWidget(back, 0, Qt.AlignmentFlag.AlignLeft)
        head_column.addWidget(view_title("챙길 일"))
        self.lead = muted_label("묻기 전에 먼저 챙깁니다. 제안은 과거 자료에서 읽은 것이며, 올해의 의무가 아닙니다.")
        head_column.addWidget(self.lead)
        outer.addWidget(header)

        self.page = Page(max_width=theme.CONTENT_MAX_W)
        outer.addWidget(self.page, 1)
        self.refresh()

    def today(self) -> date:
        return self._today or date.today()

    # ── 그리기 ──────────────────────────────────────────────────────
    def refresh(self) -> None:
        data = briefing.build(self.db, self.today())
        self.data = data
        column = self.page.reset()
        column.addLayout(self._tiles(data))
        if data.empty:
            scanned = f"마지막으로 살핀 날: {data.scanned_on}. " if data.scanned_on else ""
            column.addWidget(EmptyState(
                "지금 먼저 챙길 일이 없습니다",
                scanned + "과거 반복 주기와 올해 등록된 문서를 살폈습니다. 챙길 일이 생기면 여기에 먼저 나타납니다."))
            return
        if data.cards or data.hidden:
            column.addWidget(section_title("눈치코치가 먼저 챙긴 일"))
            for card in data.cards:
                column.addWidget(self._card(card))
            if data.hidden:
                column.addWidget(muted_label(f"그 밖에 새 제안 {data.hidden}건이 더 있습니다. "
                                             "위 제안을 정리하면 이어서 보입니다.", small=True))
        if data.cases:
            column.addWidget(section_title("진행 중인 수행 건"))
            for row in data.cases:
                column.addWidget(self._case(row))
        if data.gaps:
            column.addWidget(section_title("빠진 단계 후보 (과거 관행)"))
            for gap in data.gaps:
                column.addWidget(self._gap(gap))
        column.addStretch(1)

    def _tiles(self, data) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(theme.SP_MD)
        for key, label in TILES:
            tile = Card()
            tile.setObjectName("Card")
            tile.body.setSpacing(0)
            number = muted_label(str(data.tiles[key]), wrap=False)
            number.setStyleSheet("font-size: 26px; font-weight: 700;")
            tile.body.addWidget(number)
            tile.body.addWidget(muted_label(label, small=True, wrap=False))
            row.addWidget(tile, 1)
        return row

    def _card(self, card: briefing.Card) -> QWidget:
        widget = Card(tone="primary" if card.certainty == "official" else "")
        head = QHBoxLayout()
        head.setSpacing(theme.SP_SM)
        head.addWidget(Badge(briefing.CERTAINTY_LABEL[card.certainty],
                             "accent" if card.certainty == "official" else "neutral"))
        title = QPushButton(card.title)
        title.setObjectName("CardTitle")
        title.setCursor(Qt.CursorShape.PointingHandCursor)
        title.setToolTip("눌러서 업무 상세로 갑니다")
        title.clicked.connect(lambda _=False, t=card.task_id: self.open_task.emit(t))
        head.addWidget(title)
        head.addStretch(1)
        widget.body.addLayout(head)
        for line in card.why:
            widget.body.addWidget(muted_label(line))
        if card.evidence:
            panel = SubPanel()
            panel.body.addWidget(muted_label("근거 문서", small=True))
            for doc in card.evidence:
                link = doc_icon(QPushButton(doc["filename"]))
                link.setObjectName("Link")
                link.setCursor(Qt.CursorShape.PointingHandCursor)
                link.clicked.connect(lambda _=False, d=doc["doc_id"]: self._open_document(d))
                panel.body.addWidget(link, 0, Qt.AlignmentFlag.AlignLeft)
            widget.body.addWidget(panel)
        panel = SubPanel()
        panel.body.addWidget(muted_label("받아들이면 하는 일", small=True))
        for line in card.actions:
            panel.body.addWidget(muted_label(line, small=True))
        widget.body.addWidget(panel)

        buttons = QHBoxLayout()
        buttons.setSpacing(theme.SP_SM)
        start = QPushButton("준비 작업 시작")
        start.setCursor(Qt.CursorShape.PointingHandCursor)
        start.clicked.connect(lambda _=False, c=card: self.start(c.rec_id))
        later = QPushButton("나중에")
        later.setObjectName("Quiet")
        later.setToolTip(f"{recommend.SNOOZE_DAYS}일 뒤에 다시 알립니다")
        later.clicked.connect(lambda _=False, c=card: self.snooze(c.rec_id))
        decline = QPushButton("거절 ▾")
        decline.setObjectName("Quiet")
        menu = QMenu(decline)
        for reason in recommend.DISMISS_REASONS:
            menu.addAction(reason, lambda r=reason, c=card: self.dismiss(c.rec_id, r))
        decline.setMenu(menu)
        for button in (start, later, decline):
            buttons.addWidget(button)
        buttons.addStretch(1)
        widget.body.addLayout(buttons)
        return widget

    def _case(self, row: briefing.CaseRow) -> QWidget:
        widget = Card(tone="attention" if row.waiting else "")
        head = QHBoxLayout()
        title = QPushButton(row.title)
        title.setObjectName("CardTitle")
        title.setCursor(Qt.CursorShape.PointingHandCursor)
        title.clicked.connect(lambda _=False, t=row.task_id: self.open_task.emit(t))
        head.addWidget(title)
        if row.waiting:
            head.addWidget(Badge("승인 대기", "attention"))
        head.addStretch(1)
        open_button = QPushButton("준비 작업 열기")
        open_button.setObjectName("Quiet")
        open_button.clicked.connect(lambda _=False, r=row: self._open_workbox(r.task_id, None, None))
        head.addWidget(open_button)
        widget.body.addLayout(head)
        progress = f"체크리스트 {row.done}/{row.total}" if row.total else "체크리스트 아직 없음"
        widget.body.addWidget(muted_label(f"{row.basis} · {row.due} · {progress}", small=True))
        return widget

    def _gap(self, gap: dict) -> QWidget:
        row = ListRow()
        row.row.addWidget(muted_label(f"{gap['month']}월 · {gap['label']}", wrap=False))
        row.row.addWidget(muted_label(gap["why"], small=True), 1)
        return row

    # ── 응답 ────────────────────────────────────────────────────────
    def start(self, rec_id: int) -> None:
        """제안을 받아들이고 준비 작업 창을 연다. 실행은 창이 하고, 승인은 창이 묻는다."""
        rec = self.db.recommendation(rec_id)
        _, run_id = recommend.accept(self.db, rec_id, self.today())
        self.changed.emit()
        self._open_workbox(rec["task_id"], rec["target_year"], run_id)
        self.refresh()

    def snooze(self, rec_id: int) -> None:
        recommend.snooze(self.db, rec_id, self.today())
        self.changed.emit()
        self.refresh()

    def dismiss(self, rec_id: int, reason: str) -> None:
        recommend.dismiss(self.db, rec_id, reason)
        self.changed.emit()
        self.refresh()

    def _open_workbox(self, task_id: int, year: int | None, run_id: int | None) -> None:
        from .workbox import WorkboxDialog

        dialog = WorkboxDialog(self, self.db, task_id, today=self.today(), run_id=run_id, year=year)
        dialog.exec()
        self.changed.emit()
        self.refresh()

    def _open_document(self, doc_id: int) -> None:
        row = self.db.document(doc_id)
        if row is not None:
            open_original(self, self.db, row["path"])
