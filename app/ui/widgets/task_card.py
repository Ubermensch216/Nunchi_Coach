"""업무 카드 — 업무 화면의 기본 단위.

한 화면에서 여러 업무를 조망하는 것이 목적이므로, 카드 하나가 3초 안에
읽혀야 한다. 그래서 정보를 네 층으로 못 박고 그 이상 넣지 않는다.

  1. 색 띠   — 업무를 구분하는 시각 앵커 (같은 업무는 항상 같은 색)
  2. 이름·규모 — 무슨 업무이고 얼마나 큰가
  3. 월 스트립 — 언제 하는 일인가 (12칸을 한 줄로 압축)
  4. 상태 칩  — 신뢰도·확인 필요 여부

색은 장식이 아니라 인덱스다. 업무 이름을 해시해 고정 팔레트에서 고르므로
같은 업무는 재실행해도 같은 색을 갖는다. 다만 색만으로 뜻을 전하지는
않는다 — 신뢰도·상태는 항상 기호와 글자를 함께 쓴다(PRD §18.4).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import theme

# 업무 구분용 팔레트. 채도를 낮춰 공직 문서 환경에 어울리게 한다.
#
# **상태 색(빨강·주황·초록)과 겹치는 색은 쓰지 않는다.** 예전 팔레트에는
# 오류의 빨강, 확인 필요의 주황, 확인됨의 초록이 그대로 들어 있어서, 띠가
# 빨간 카드는 '문제 있는 업무'로 읽혔다. 색이 인덱스로만 읽히려면 이 화면에서
# 뜻을 가진 색과 떨어져 있어야 한다 — 청·남·보라·청록·회청 계열만 쓴다.
# 밝은 벌과 어두운 벌 양쪽에서 띠로 보이는 중간 밝기로 골랐다.
TASK_COLORS = [
    "#3F74B8",  # 청
    "#5A62C4",  # 남보라
    "#8259B8",  # 보라
    "#2B8FA3",  # 청록
    "#5F7F96",  # 회청
    "#A0589A",  # 자주
    "#3D8C8C",  # 옥색
    "#7482A0",  # 청회
]

STRIP_CELL_W = 14
STRIP_H = 10


def color_for(name: str) -> str:
    """업무 이름에서 고정 색을 뽑는다. 같은 이름은 항상 같은 색."""
    return TASK_COLORS[sum(ord(c) for c in name) % len(TASK_COLORS)]


def month_legend() -> str:
    """카드 아래 열두 칸이 무엇인지 말한다.

    일정 화면의 격자에는 범례가 붙어 있는데(timeline_grid.legend_text) 업무
    카드의 월 스트립에는 없었다. 같은 뜻의 그림이 한 화면에서는 설명되고
    다른 화면에서는 설명되지 않으면, 사용자는 두 그림이 다른 것이라고
    생각하거나 둘 다 안 믿는다.

    범례는 판마다 붙이지 않고 카드 묶음 아래 한 줄만 둔다 — 카드가 여덟
    장이면 같은 설명이 여덟 번 붙는다.
    """
    return "카드 아래 열두 칸은 1월부터 12월까지입니다   ■ 이 업무를 하는 달   □ 아닌 달"


class MonthStrip(QWidget):
    """12개월을 한 줄로 압축해 '언제 하는 일인가'를 즉시 보여준다.

    글자(▉·)로 그리면 글꼴에 따라 칸 너비가 흔들리고 다른 본문 글자와
    섞여 읽힌다. 칸을 위젯으로 그리면 12칸의 틀이 언제나 같은 자리에
    있으므로, 채워진 구간이 몇 월인지 눈으로 셀 수 있다. 연속한 달은
    이어 붙여 하나의 기간으로 보이게 한다 — 일정 화면의 막대와 같은 말이다.

    반복 주기를 모르는 업무는 빈 틀만 그린다. 없는 것을 지어내지 않는다.
    """

    def __init__(
        self,
        months: list[int] | None,
        accent: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("MonthStrip")
        self.months = sorted(set(months or []))
        active = set(self.months)

        self.setFixedHeight(STRIP_H)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setToolTip(
            "1월부터 12월까지. 채워진 칸이 이 업무를 하는 달입니다."
            if active else "반복 주기를 아직 찾지 못했습니다."
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        radius = STRIP_H // 2
        for month in range(1, 13):
            cell = QLabel()
            cell.setFixedSize(STRIP_CELL_W, STRIP_H)
            if month not in active:
                # 빈 칸도 틀의 일부다. 양 끝만 둥글려 12칸이 하나의 띠로 보인다.
                left = radius if month == 1 else 0
                right = radius if month == 12 else 0
                cell.setStyleSheet(
                    f"background: {theme.SURFACE_ALT};"
                    f"border-top-left-radius: {left}px;"
                    f"border-bottom-left-radius: {left}px;"
                    f"border-top-right-radius: {right}px;"
                    f"border-bottom-right-radius: {right}px;"
                )
            else:
                left = radius if month - 1 not in active else 0
                right = radius if month + 1 not in active else 0
                cell.setStyleSheet(
                    f"background: {accent};"
                    f"border-top-left-radius: {left}px;"
                    f"border-bottom-left-radius: {left}px;"
                    f"border-top-right-radius: {right}px;"
                    f"border-bottom-right-radius: {right}px;"
                )
            row.addWidget(cell)
        row.addStretch(1)


class TaskCard(QFrame):
    """클릭하면 상세로 들어가는 업무 카드."""

    opened = Signal(int)

    def __init__(
        self,
        task_id: int,
        name: str,
        description: str | None,
        span_text: str,
        months: list[int] | None,
        cycle_text: str | None,
        confidence: str,
        review_state: str,
        reading_count: int,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._task_id = task_id
        self.setObjectName("TaskCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        # Ignored 정책 + 낮은 최소 폭: 카드가 자기 내용 폭을 주장하면
        # 좁은 창에서 그리드가 밀려 잘린다. 폭 결정권은 그리드에 준다.
        self.setMinimumWidth(200)
        self.setToolTip(f"{name} — 눌러서 자세히 보기")

        accent = color_for(name)
        # 누를 수 있는 카드다. 마우스로는 테두리가 서고, 키보드로도 닿는다 —
        # Tab으로 옮겨 Enter·Space로 연다(예전에는 마우스로만 열 수 있었다).
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"{name} 업무 열기")
        self.setStyleSheet(
            f"""
            QFrame#TaskCard {{
                background: {theme.BG};
                border: 1px solid {theme.BORDER};
                border-left: 4px solid {accent};
                border-radius: {theme.RADIUS}px;
            }}
            QFrame#TaskCard:hover {{
                border-color: {theme.BORDER_STRONG};
                border-left: 4px solid {accent};
            }}
            QFrame#TaskCard:focus {{
                border: 2px solid {theme.PRIMARY};
                border-left: 4px solid {accent};
            }}
            """
        )

        column = QVBoxLayout(self)
        column.setContentsMargins(theme.SP_LG, theme.SP_MD, theme.SP_LG, theme.SP_MD)
        column.setSpacing(theme.SP_SM)

        column.addWidget(_title(name))
        column.addWidget(_meta(span_text))

        if description:
            column.addWidget(_description(description))

        column.addWidget(MonthStrip(months, accent))
        column.addWidget(_cycle_line(cycle_text))
        column.addLayout(_footer(confidence, review_state, reading_count))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 — Qt 규약
        if event.button() == Qt.MouseButton.LeftButton:
            self.opened.emit(self._task_id)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 — Qt 규약
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.opened.emit(self._task_id)
            return
        super().keyPressEvent(event)


# ── 구성 요소 ───────────────────────────────────────────────────────

def _title(name: str) -> QLabel:
    """제목은 검정으로 둔다.

    한때 업무 색으로 칠했지만, 카드 여덟 장이 여덟 색 제목으로 늘어서면
    이름을 읽는 일 자체가 느려진다. 색은 왼쪽 띠와 월 스트립이 맡고,
    글자는 가장 읽기 쉬운 상태로 남긴다.
    """
    label = QLabel(name)
    label.setWordWrap(True)
    # 크기는 전역 스타일시트가 정한다. 여기서 px를 못 박으면 '글자 크게'를
    # 골라도 카드 제목만 그대로 남는다.
    label.setObjectName("SectionTitle")
    return label


def _meta(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Small")
    return label


def _description(text: str) -> QLabel:
    """설명은 두 줄까지만. 카드마다 높이가 들쭉날쭉하면 격자가 어수선해진다."""
    label = QLabel(_clip(text, 58))
    label.setObjectName("Muted")
    label.setWordWrap(True)
    label.setToolTip(text)
    label.setMinimumHeight(theme.FS_BODY * 2 + 6)
    # 줄바꿈 라벨도 자기 '한 줄 폭'을 최소 폭으로 주장한다. 그대로 두면
    # 카드가 좁아지지 못해 그리드 열 수 계산이 무너진다.
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
    return label


def _cycle_line(cycle_text: str | None) -> QWidget:
    from .. import icons

    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(theme.SP_XS)
    label = QLabel(cycle_text or "반복 주기 미확인")
    label.setObjectName("Small")
    if cycle_text:
        mark = icons.label("repeat", theme.TEXT_MUTED, 14)
        if mark is not None:
            row.addWidget(mark)
    else:
        label.setStyleSheet(f"color: {theme.TEXT_SUBTLE};")
    row.addWidget(label)
    row.addStretch(1)
    return box


def _footer(confidence: str, review_state: str, reading_count: int) -> QHBoxLayout:
    from ...core import status
    from .common import Badge

    row = QHBoxLayout()
    row.setSpacing(theme.SP_SM)

    # 뱃지는 예외만 색으로 세운다. 처음에는 모든 카드가 '확인 필요'라서, 그것을
    # 주황으로 칠하면 화면 전체가 주황이 되고 정작 조심할 카드가 묻힌다.
    #   · 확인함        — 초록. 사람이 손댄 것이 눈에 띄어야 진도가 보인다.
    #   · 확인 필요     — 회색. 기본 상태다. 말은 하되 소리치지 않는다.
    #   · 느슨한 묶음   — 주황. 같은 업무가 아닐 수 있다는 진짜 경고다.
    # 묶음이 단단한 것은 적지 않는다 — 문제가 없다는 말을 카드마다 읽게 하지 않는다.
    # 확인된 업무는 그렇다고 말해 준다. 인수인계 진행도(§18)에서 세는 것과
    # 카드에서 보이는 것이 같아야 사용자가 진도를 신뢰한다.
    if review_state == status.CONFIRMED:
        row.addWidget(Badge(f"{status.symbol(status.CONFIRMED)} 확인함", "ok"))
    else:
        row.addWidget(Badge(f"{status.symbol(review_state)} 확인 필요", "neutral"))
    if confidence != "high":
        note, kind = _CONFIDENCE.get(confidence, _CONFIDENCE["low"])
        row.addWidget(Badge(note, kind))
    row.addStretch(1)

    if reading_count:
        from .. import icons

        tip = f"먼저 읽을 문서 {reading_count}건"
        mark = icons.label("documents", theme.TEXT_MUTED, 14)
        if mark is not None:
            mark.setToolTip(tip)
            row.addWidget(mark)
        count = QLabel(str(reading_count))
        count.setObjectName("Small")
        count.setToolTip(tip)
        row.addWidget(count)
    return row


# 카드에서는 짧게 쓴다 — 목록에서 문장을 읽게 하면 조망이 안 된다.
_CONFIDENCE = {
    "high": ("● 단단함", "ok"),
    "medium": ("묶음 보통", "neutral"),
    "low": ("느슨한 묶음", "attention"),
}


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
