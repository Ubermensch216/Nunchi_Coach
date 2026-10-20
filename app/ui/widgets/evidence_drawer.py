"""근거 서랍 — Claim → Evidence → Original의 가운데 단계 (계획서 §14·§29).

AI가 무언가를 주장하면 사용자는 그것을 확인할 수 있어야 한다. 그런데
원본을 바로 여는 것은 확인이 아니라 **이탈**이다 — 한글이 뜨는 데 몇 초가
걸리고, 문서를 열면 화면을 떠나며, 돌아오면 맥락을 잃는다. 그래서 근거를
앱 안에서 먼저 보여주고, 원본 열기는 그다음 선택으로 둔다.

    주장          "…9월에 접수했다[1]"
      ↓ [1] 클릭
    근거          이 서랍 — 파일명 · 위치 · 인용문
      ↓ [원본 열기]
    원본          한글·엑셀에서 실제 문서

질문 화면만의 위젯이 아니다. 업무 설명·주기·처리 순서·최신본 후보도 모두
같은 서랍을 쓴다(계획서 §29) — 그래야 "근거 기반"이 UI 자체에 박힌다.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import icons, theme
from .common import Card, ElidedLabel, clear_layout, friendly_locator, muted_label, section_title


@dataclass(slots=True)
class Evidence:
    """서랍에 넣을 근거 하나. DB 행이나 Citation을 그대로 넘기지 않는다 —
    호출하는 화면마다 자료 모양이 다르므로 여기서 한 겹 끊는다."""

    label: str            # 파일명
    locator: str = ""     # "3쪽", "'집계' A1:E3" 등
    snippet: str = ""     # 실제 인용문
    path: str = ""        # 원본 경로
    note: str = ""        # "왜 이것이 근거인가" (선택)
    mark: int | None = None   # 답변의 [n] 번호. 있으면 highlight()로 짚을 수 있다.


class EvidenceDrawer(QFrame):
    """공용 근거 뷰. 기본은 서랍, embedded=True이면 폭 제한 없는 탭 본문이다."""

    open_original = Signal(str)   # 원본 경로
    closed = Signal()
    all_requested = Signal()

    def __init__(self, parent: QWidget | None = None, *, embedded: bool = False):
        super().__init__(parent)
        self._embedded = embedded
        self.setObjectName("EvidencePage" if embedded else "EvidenceDrawer")
        if not embedded:
            self.setFixedWidth(theme.DRAWER_W)
            self.hide()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(theme.SP_LG, theme.SP_MD, theme.SP_LG, theme.SP_LG)
        outer.setSpacing(theme.SP_MD)

        head = QHBoxLayout()
        head.setSpacing(theme.SP_SM)
        self._title = section_title("근거")
        head.addWidget(self._title)
        head.addStretch(1)
        self.all_button = None
        if embedded:
            self.setAccessibleName("업무 근거")
            self.all_button = QPushButton("전체 근거")
            self.all_button.setObjectName("Quiet")
            self.all_button.clicked.connect(self.all_requested.emit)
            self.all_button.hide()
            head.addWidget(self.all_button)
        else:
            close = QPushButton()
            close.setObjectName("IconButton")
            close.setIcon(icons.icon("close", theme.TEXT_MUTED, 16))
            close.setFixedSize(28, 28)
            close.setCursor(Qt.CursorShape.PointingHandCursor)
            close.setToolTip("닫기")
            close.setAccessibleName("근거 닫기")
            close.clicked.connect(self.dismiss)
            head.addWidget(close)
        outer.addLayout(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        self.column = QVBoxLayout(body)
        self.column.setContentsMargins(0, 0, theme.SP_SM, 0)
        self.column.setSpacing(theme.SP_MD)
        self.column.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self._scroll = scroll
        self._entries: dict[int, QWidget] = {}

    # ── 조작 ────────────────────────────────────────────────────────
    def show_evidence(self, items: list[Evidence], title: str = "근거", *, contextual: bool = False) -> None:
        """근거를 채우고 연다. 빈 목록이면 그렇다고 말한다 — 조용히 닫지 않는다."""
        clear_layout(self.column)
        self._entries = {}
        self._title.setText(f"{title} {len(items)}건" if items else title)
        if self.all_button is not None:
            self.all_button.setVisible(contextual)

        if not items:
            self.column.addWidget(
                muted_label("이 업무에 연결된 근거를 찾지 못했습니다." if self._embedded and not contextual else "이 주장에 연결된 근거를 찾지 못했습니다.")
            )
        for index, item in enumerate(items):
            if index:
                self.column.addWidget(_separator())
            widget = self._entry(item)
            self.column.addWidget(widget)
            if item.mark:
                self._entries[item.mark] = widget

        if not self._embedded:
            self.show()

    def highlight(self, mark: int) -> bool:
        """그 번호의 근거를 강조하고 보이는 곳으로 스크롤한다.

        상주 패널에서는 [n]을 눌러도 패널이 새로 열리지 않는다 — 이미 다
        보이고 있다. 대신 "이것"이라고 짚어 줘야 사용자가 눈으로 찾는
        수고를 하지 않는다.
        """
        found = False
        for key, widget in self._entries.items():
            on = key == mark
            widget.setProperty("picked", on)
            # 속성으로 스타일을 바꿨으면 다시 칠하라고 알려야 한다.
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            if on:
                self._scroll.ensureWidgetVisible(widget)
                found = True
        return found

    def dismiss(self) -> None:
        self.hide()
        self.closed.emit()

    # ── 구성 ────────────────────────────────────────────────────────
    def _entry(self, item: Evidence) -> QWidget:
        return evidence_entry(item, self.open_original.emit)


def evidence_entry(item: Evidence, on_open=None, *, show_name: bool = True) -> QWidget:
    """근거 한 건: 파일명 · 위치 · 이유 · 인용문 · 원본 열기.

    서랍과 업무 자료 화면이 같은 모양으로 그린다. 문서 줄 안에 펴 보일 때는
    파일명이 이미 위에 있으므로 show_name=False로 뺀다.
    """
    holder = QFrame()
    holder.setObjectName("EvidenceEntry")
    holder.setProperty("picked", False)
    column = QVBoxLayout(holder)
    column.setContentsMargins(theme.SP_SM, theme.SP_SM, theme.SP_SM, theme.SP_SM)
    column.setSpacing(theme.SP_XS)

    # 파일명은 띄어쓰기가 없어 줄바꿈이 듣지 않는다 — 접지 않으면 서랍
    # 밖으로 잘려 나간다(실제 자료의 긴 파일명에서 확인).
    if show_name:
        name = ElidedLabel(f"[{item.mark}] {item.label}" if item.mark else item.label)
        name.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600;")
        name.setToolTip(item.path or item.label)
        column.addWidget(name)

    if item.locator:
        column.addWidget(muted_label(friendly_locator(item.locator), small=True))
    if item.note:
        column.addWidget(muted_label(item.note, small=True))

    if item.snippet:
        quote = muted_label(f"“{item.snippet}”")
        quote.setObjectName("EvidenceQuote")
        column.addWidget(quote)
    else:
        # 인용문을 못 만든 경우를 빈칸으로 두지 않는다 — 사용자가
        # "왜 아무것도 없지?"를 겪지 않게 이유를 밝힌다.
        column.addWidget(
            muted_label("이 문서에서 인용문을 뽑지 못했습니다.", small=True)
        )

    if item.path and on_open is not None:
        open_button = QPushButton("원본 열기")
        open_button.clicked.connect(lambda _=False, p=item.path: on_open(p))
        column.addWidget(open_button, alignment=Qt.AlignmentFlag.AlignLeft)
    return holder


class EvidencePanel(Card):
    """업무 자료 화면 위에 펴는 '이 주장의 근거' 판. 닫으면 전체 자료로 돌아간다.

    근거는 따로 모아 세지 않는다 — 어느 문서의 어느 대목인지가 문서 줄에 붙어
    있고, 이 판은 안내·일정 화면의 '근거 N건'을 눌렀을 때 그 대목만 먼저 보인다.
    """

    open_original = Signal(str)
    all_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAccessibleName("선택한 항목의 근거")
        head = QHBoxLayout()
        head.setSpacing(theme.SP_SM)
        self._title = section_title("근거")
        head.addWidget(self._title)
        head.addStretch(1)
        self.all_button = QPushButton("전체 자료 보기")
        self.all_button.setObjectName("Quiet")
        self.all_button.clicked.connect(self.all_requested.emit)
        head.addWidget(self.all_button)
        self.body.addLayout(head)
        self._entries = QVBoxLayout()
        self._entries.setSpacing(theme.SP_MD)
        self.body.addLayout(self._entries)
        self.hide()

    def show_evidence(self, items: list[Evidence], title: str = "근거") -> None:
        clear_layout(self._entries)
        self._title.setText(f"{title} {len(items)}건" if items else title)
        if not items:
            self._entries.addWidget(muted_label("이 주장에 연결된 근거를 찾지 못했습니다."))
        for index, item in enumerate(items):
            if index:
                self._entries.addWidget(_separator())
            self._entries.addWidget(evidence_entry(item, self.open_original.emit))
        self.show()


def _separator() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    line.setFixedHeight(1)
    return line
