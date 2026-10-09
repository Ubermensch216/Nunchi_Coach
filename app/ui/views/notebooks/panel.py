"""작업 화면의 세 판(출처·대화·스튜디오)이 함께 쓰는 틀."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from ... import icons, theme
from ...widgets import divider, section_title

ICON_PX = 18


def set_icon(button: QPushButton, name: str, color: str | None = None) -> None:
    """글자 없는 단추의 그림을 갈아 끼운다. 색은 그릴 때 정해지므로 테마가 바뀌면 다시 부른다."""
    art = icons.icon(name, color or theme.TEXT_MUTED, button.iconSize().width())
    button.setIcon(art)
    # QtSvg가 없는 환경 — 그림 대신 이름으로 물러선다.
    button.setText(button.accessibleName() if art.isNull() else "")


def icon_button(name: str, tooltip: str, label: str = "", color: str | None = None) -> QPushButton:
    """그림만 있는 단추. 무엇을 하는지는 툴팁과 접근성 이름(label)이 말한다."""
    button = QPushButton()
    button.setObjectName("IconTool")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setToolTip(tooltip)
    button.setAccessibleName(label or tooltip)
    button.setIconSize(QSize(ICON_PX, ICON_PX))
    set_icon(button, name, color)
    if not button.text():
        button.setFixedSize(ICON_PX + 14, ICON_PX + 14)
    return button


class Panel(QFrame):
    """머리줄(이름 + 접기)과 본문으로 된 흰 판. side는 이 판이 붙은 쪽(left|right)이다."""

    collapse_requested = Signal()

    def __init__(self, title: str, parent: QWidget | None = None, side: str = "left"):
        super().__init__(parent)
        self.setObjectName("NotebookPanel")
        self.side = side
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.head = QHBoxLayout()
        self.head.setContentsMargins(theme.SP_LG, theme.SP_MD, theme.SP_SM, theme.SP_MD)
        self.head.setSpacing(theme.SP_SM)
        self.head.addWidget(section_title(title))
        self.head.addStretch(1)
        self.fold = icon_button(f"panel-{side}-close", f"{title} 판 접기", f"{title} 접기")
        self.fold.clicked.connect(self.collapse_requested.emit)
        self.head.addWidget(self.fold)
        outer.addLayout(self.head)
        outer.addWidget(divider())

        self.body = QVBoxLayout()
        self.body.setContentsMargins(theme.SP_LG, theme.SP_MD, theme.SP_LG, theme.SP_LG)
        self.body.setSpacing(theme.SP_SM)
        outer.addLayout(self.body, 1)

    def repaint_icons(self) -> None:
        set_icon(self.fold, f"panel-{self.side}-close")


class Rail(QFrame):
    """접힌 판이 남기는 좁은 띠. 판이 있던 바로 그 자리에 펴는 단추를 세운다."""

    expand_requested = Signal()

    def __init__(self, title: str, side: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("NotebookPanel")
        self.side = side
        self.setFixedWidth(ICON_PX + 14 + theme.SP_XS * 2 + 2)
        column = QVBoxLayout(self)
        column.setContentsMargins(theme.SP_XS, theme.SP_MD, theme.SP_XS, theme.SP_MD)
        self.unfold = icon_button(f"panel-{side}-open", f"{title} 판 펴기", f"{title} 펴기")
        self.unfold.clicked.connect(self.expand_requested.emit)
        column.addWidget(self.unfold, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addStretch(1)

    def repaint_icons(self) -> None:
        set_icon(self.unfold, f"panel-{self.side}-open")
