"""큰 폴더형 업무 카드 위젯 (개편계획서 §13.1 및 §14).

홈 화면의 기본 구성 단위로, 큼직한 폴더 형태를 통해 업무의 의미가 한눈에 보이게 한다.
정보 위계:
  1. 업무명 (크고 선명하게, 최대 2줄 표시)
  2. 한 줄 설명 (무슨 일인가)
  3. 주요 결과물 / 시기 / 다음 행동
  4. 보조 정보 (자료 수, 분석/확인 상태)

마우스 한 번 클릭 또는 키보드 Enter/Space로 열린다.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..widgets.common import Badge, ElidedLabel, WrapLabel, _WrapFrame, muted_label
from ..widgets.task_card import MonthStrip, color_for


class WorkFolderCard(_WrapFrame):
    """큼직한 묶음 형태의 업무 카드."""

    opened = Signal(int)

    def __init__(
        self,
        task_id: int,
        name: str,
        description: str = "",
        deliverable: str = "",
        span_text: str = "",
        months: list[int] | None = None,
        cycle_text: str | None = None,
        next_action: str = "",
        review_state: str = "",
        confidence: str = "high",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.task_id = task_id
        self.name = name
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self.setObjectName("WorkFolderCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.setMinimumWidth(280)
        self.setMinimumHeight(210)
        self.setAccessibleName(f"업무 {name}")

        self._build_ui(
            name=name,
            description=description,
            deliverable=deliverable,
            span_text=span_text,
            months=months,
            cycle_text=cycle_text,
            next_action=next_action,
            review_state=review_state,
            confidence=confidence,
        )

    @property
    def cover_color(self) -> str:
        return color_for(str(self.task_id))

    def _build_ui(
        self,
        name: str,
        description: str,
        deliverable: str,
        span_text: str,
        months: list[int] | None,
        cycle_text: str | None,
        next_action: str,
        review_state: str,
        confidence: str,
    ) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(10)

        # 1. 상단 탭/표지 라인과 보조 배지
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        # 폴더 컬러 인디케이터 (작은 탭 형태의 색 판)
        self.tab_indicator = QFrame()
        self.tab_indicator.setFixedSize(28, 8)
        self.tab_indicator.setStyleSheet(
            f"background-color: {self.cover_color}; border-radius: 4px;"
        )
        top_row.addWidget(self.tab_indicator)

        top_row.addStretch(1)

        if review_state and review_state != "inferred":
            top_row.addWidget(Badge.state(review_state))
        elif confidence and confidence != "high":
            top_row.addWidget(Badge("묶음 확인 필요", "attention"))

        outer.addLayout(top_row)

        # 2. 업무명 (크고 굵게, 최대 2줄)
        title_label = WrapLabel(name)
        title_label.setObjectName("FolderCardTitle")
        title_label.setWordWrap(True)
        outer.addWidget(title_label)

        # 3. 한 줄 설명 (무슨 일인가)
        desc_text = description.strip() if description else "자료에서 분석 중인 업무입니다."
        self.description = desc_text
        desc_label = WrapLabel(desc_text)
        desc_label.setTextFormat(Qt.TextFormat.PlainText)
        desc_label.setToolTip(desc_text)
        desc_label.setWordWrap(True)
        desc_label.setObjectName("FolderCardDesc")
        outer.addWidget(desc_label)

        # 4. 주요 결과물 / 다음 행동 (있을 경우)
        for caption, text in (("결과물", deliverable), ("다음 행동", next_action)):
            if text:
                action_label = ElidedLabel(f"{caption}: {text}")
                action_label.setObjectName("FolderCardAction")
                outer.addWidget(action_label)

        # 5. 월간 스트립 (언제 하는 일인가)
        outer.addSpacing(2)
        self.month_strip = MonthStrip(months)
        outer.addWidget(self.month_strip)

        outer.addStretch(1)

        # 6. 하단 메타 정보 (규모 / 주기 요약)
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(6)

        meta_parts = []
        if cycle_text:
            meta_parts.append(cycle_text)
        if span_text:
            meta_parts.append(span_text)
        meta_summary = " · ".join(meta_parts) if meta_parts else "자료 분석 중"

        meta_label = muted_label(meta_summary, small=True)
        bottom_row.addWidget(meta_label)
        bottom_row.addStretch(1)

        self.hint_label = QLabel("열기 →")
        self.hint_label.setObjectName("FolderCardHint")
        self.hint_label.setStyleSheet(
            f"color: {self.cover_color};"
        )
        bottom_row.addWidget(self.hint_label)

        outer.addLayout(bottom_row)

    def _set_pressed(self, pressed: bool) -> None:
        # 누른 순간의 눌림은 모션이 아니라 정적 상태다 — 동작 줄이기에서도 그대로 보인다.
        if self.property("pressed") != pressed:
            self.setProperty("pressed", pressed)
            self.style().unpolish(self)
            self.style().polish(self)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._set_pressed(True)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._set_pressed(False)
        if event.button() == Qt.MouseButton.LeftButton:
            if self.rect().contains(event.position().toPoint()):
                self.opened.emit(self.task_id)
        super().mouseReleaseEvent(event)

    def neighbor(self, key: int) -> "WorkFolderCard | None":
        """방향키·Home·End로 옮겨 갈 같은 격자의 카드. 보이는 카드만 대상이다."""
        parent = self.parentWidget()
        if parent is None:
            return None
        cards = [c for c in parent.findChildren(WorkFolderCard)
                 if c.parentWidget() is parent and c.isVisibleTo(parent)]
        cards.sort(key=lambda c: (c.y(), c.x()))
        if self not in cards:
            return None
        index = cards.index(self)
        if key == Qt.Key.Key_Home:
            return cards[0]
        if key == Qt.Key.Key_End:
            return cards[-1]
        if key == Qt.Key.Key_Left:
            return cards[index - 1] if index > 0 else None
        if key == Qt.Key.Key_Right:
            return cards[index + 1] if index + 1 < len(cards) else None
        # 위·아래는 다음 줄에서 가로 위치가 가장 가까운 카드
        down = key == Qt.Key.Key_Down
        rows = [c for c in cards if (c.y() > self.y() if down else c.y() < self.y())]
        if not rows:
            return None
        row_y = min(c.y() for c in rows) if down else max(c.y() for c in rows)
        return min((c for c in rows if c.y() == row_y), key=lambda c: abs(c.x() - self.x()))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.opened.emit(self.task_id)
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up,
                           Qt.Key.Key_Down, Qt.Key.Key_Home, Qt.Key.Key_End):
            target = self.neighbor(event.key())
            if target is not None:
                target.setFocus(Qt.FocusReason.TabFocusReason)
                event.accept()
                return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._set_pressed(False)
        super().focusOutEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() in (event.Type.PaletteChange, event.Type.StyleChange):
            self.tab_indicator.setStyleSheet(
                f"background-color: {self.cover_color}; border-radius: 4px;"
            )
            self.hint_label.setStyleSheet(
                f"color: {self.cover_color};"
            )
        super().changeEvent(event)
