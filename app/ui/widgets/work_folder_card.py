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

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeyEvent, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..widgets.common import Badge, ElidedLabel, muted_label
from ..widgets.task_card import MonthStrip, color_for


class WorkFolderCard(QFrame):
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
        self.cover_color = color_for(name)
        self._hovered = False
        self._pressed = False

        self.setObjectName("WorkFolderCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
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
        tab_indicator = QFrame()
        tab_indicator.setFixedSize(28, 8)
        tab_indicator.setStyleSheet(
            f"background-color: {self.cover_color}; border-radius: 4px;"
        )
        top_row.addWidget(tab_indicator)

        top_row.addStretch(1)

        if review_state and review_state != "inferred":
            top_row.addWidget(Badge.state(review_state))
        elif confidence and confidence != "high":
            top_row.addWidget(Badge("묶음 확인 필요", "attention"))

        outer.addLayout(top_row)

        # 2. 업무명 (크고 굵게, 최대 2줄)
        title_label = QLabel(name)
        title_label.setObjectName("FolderCardTitle")
        title_label.setWordWrap(True)
        title_font = QFont(self.font())
        title_font.setPointSize(13)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setStyleSheet("color: #1E293B; line-height: 1.3;")
        outer.addWidget(title_label)

        # 3. 한 줄 설명 (무슨 일인가)
        desc_text = description.strip() if description else "자료에서 분석 중인 업무입니다."
        desc_label = ElidedLabel(desc_text)
        desc_label.setObjectName("FolderCardDesc")
        desc_label.setStyleSheet("color: #475569; font-size: 12px;")
        outer.addWidget(desc_label)

        # 4. 주요 결과물 / 다음 행동 (있을 경우)
        if deliverable or next_action:
            action_text = deliverable or next_action
            action_label = ElidedLabel(f"핵심: {action_text}")
            action_label.setObjectName("FolderCardAction")
            action_label.setStyleSheet("color: #0F766E; font-size: 11px; font-weight: 500;")
            outer.addWidget(action_label)

        # 5. 월간 스트립 (언제 하는 일인가)
        outer.addSpacing(2)
        outer.addWidget(MonthStrip(months, accent=self.cover_color))

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

        hint_label = QLabel("열기 →")
        hint_label.setStyleSheet(
            f"color: {self.cover_color}; font-size: 11px; font-weight: 600;"
        )
        bottom_row.addWidget(hint_label)

        outer.addLayout(bottom_row)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            self.update()
            if self.rect().contains(event.pos()):
                self.opened.emit(self.task_id)
        super().mouseReleaseEvent(event)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self._pressed = False
        self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.opened.emit(self.task_id)
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect()

        # 배경 및 테두리 스타일
        if self._pressed:
            bg_color = QColor("#F1F5F9")
            border_color = QColor(self.cover_color)
            border_width = 2
        elif self._hovered or self.hasFocus():
            bg_color = QColor("#FFFFFF")
            border_color = QColor(self.cover_color)
            border_width = 1.5
        else:
            bg_color = QColor("#FAFAFA")
            border_color = QColor("#E2E8F0")
            border_width = 1

        painter.setBrush(bg_color)
        painter.setPen(Qt.PenStyle.NoPen)
        # 둥근 모서리 배경
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 10, 10)

        # 테두리
        from PySide6.QtGui import QPen
        pen = QPen(border_color, border_width)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 10, 10)

        painter.end()
        super().paintEvent(event)
