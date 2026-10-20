"""업무 카드 — 업무 화면의 기본 단위.

한 화면에서 여러 업무를 조망하는 것이 목적이므로, 카드 하나가 3초 안에
읽혀야 한다. 그래서 정보를 세 층으로 못 박고 그 이상 넣지 않는다.

  1. 이름과 한 줄 설명 — 무슨 업무인가
  2. 월 스트립         — 언제 하는 일인가 (12칸을 한 줄로 압축, 하는 달은 숫자로)
  3. 규모 한 줄        — 17건 · 2022~2025

상태 표시는 **예외에만** 붙인다. 처음 연 프로젝트는 모든 업무가 '확인 전'이라
카드마다 '확인 필요'를 달면 아홉 장이 똑같이 주황색이 되고, 정작 묶음이
느슨해 살펴봐야 할 업무가 묻힌다. 그래서 표시는 셋뿐이다 — 이번 달에 걸린
업무, 사람이 확인한 업무, 묶음이 느슨해 확인이 필요한 업무.

카드마다 다른 색을 칠하지 않는다. 색은 강조색 하나로 '하는 달'만 말한다.
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

STRIP_CELL_W = 14
# 칸 안에 달 숫자를 적으므로 글자 한 줄이 들어갈 높이다.
STRIP_H = 22
DESCRIPTION_CHARS = 44


def color_for(name: str) -> str:
    """업무 ID·이름에 관계없이 현재 테마의 공통 강조색을 사용한다."""
    return theme.PRIMARY


class MonthStrip(QWidget):
    """12개월을 한 줄로 압축해 '언제 하는 일인가'를 즉시 보여준다.

    칸을 위젯으로 그리면 12칸의 틀이 언제나 같은 자리에 있으므로, 채워진
    구간이 몇 월인지 눈으로 셀 수 있다. 연속한 달은 이어 붙여 하나의 기간으로
    보이게 한다 — 일정 화면의 막대와 같은 말이다.

    채운 칸에는 그 달의 숫자를 적는다. 예전에는 띠 아래에 '매년 9~11월'을
    따로 적어 같은 말을 그림과 글로 두 번 했고, 띠만 봐서는 몇 월인지 칸을
    세어야 했다.

    반복 주기를 모르는 업무는 빈 틀만 그린다. 없는 것을 지어내지 않는다.
    """

    def __init__(
        self,
        months: list[int] | None,
        accent: str | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("MonthStrip")
        self.months = sorted(set(months or []))
        self._accent = accent

        self.setFixedHeight(STRIP_H)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(0)
        self._cells: list[QLabel] = []

        for _ in range(1, 13):
            cell = QLabel()
            cell.setObjectName("MonthStripCell")
            cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell.setFixedHeight(STRIP_H)
            cell.setMinimumWidth(STRIP_CELL_W // 2)
            cell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._cells.append(cell)
            self._row.addWidget(cell, 1)

        self._apply_styles()

    def _apply_styles(self) -> None:
        active = set(self.months)
        accent = self._accent or theme.PRIMARY
        height = max(STRIP_H, theme.text_px("small") + 8)
        self.setFixedHeight(height)
        radius = height // 2
        for idx, cell in enumerate(self._cells):
            cell.setFixedHeight(height)
            month = idx + 1
            if month in active:
                cell.setText(str(month))
                left = radius if month - 1 not in active else 0
                right = radius if month + 1 not in active else 0
                fill = accent
                text_color = theme.TEXT_ON_PRIMARY
            else:
                cell.setText("")
                left = radius if month == 1 else 0
                right = radius if month == 12 else 0
                fill = theme.SURFACE_ALT
                text_color = "transparent"

            cell.setStyleSheet(
                f"background: {fill}; color: {text_color};"
                f"border-top-left-radius: {left}px;"
                f"border-bottom-left-radius: {left}px;"
                f"border-top-right-radius: {right}px;"
                f"border-bottom-right-radius: {right}px;"
            )

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() in (event.Type.PaletteChange, event.Type.StyleChange):
            self._apply_styles()
        super().changeEvent(event)


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
        reading_count: int = 0,
        now: bool = False,
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
        column = QVBoxLayout(self)
        column.setContentsMargins(20, theme.SP_LG, 20, theme.SP_LG)
        column.setSpacing(theme.SP_SM)

        column.addLayout(_head(name, confidence, review_state, now))
        column.addWidget(_description(description or ""))
        column.addSpacing(theme.SP_SM)
        column.addWidget(_year_row(months, cycle_text))
        span = QLabel(span_text)
        span.setObjectName("Small")
        column.addWidget(span)
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

def _head(name: str, confidence: str, review_state: str, now: bool) -> QHBoxLayout:
    """이름과, 있을 때만 붙는 표시."""
    from ...core import status
    from .common import Badge

    row = QHBoxLayout()
    row.setSpacing(theme.SP_SM)
    label = QLabel(name)
    label.setWordWrap(True)
    # 크기는 전역 스타일시트가 정한다. 여기서 px를 못 박으면 '글자 크게'를
    # 골라도 카드 제목만 그대로 남는다.
    label.setObjectName("SectionTitle")
    row.addWidget(label, 1)

    if now:
        mark = Badge("이번 달", "accent")
        mark.setToolTip("지난 자료로 보면 이번 달에 하는 업무입니다")
        row.addWidget(mark, 0, Qt.AlignmentFlag.AlignVCenter)
    # 진행도에서 세는 것과 카드에서 보이는 것이 같아야 사용자가 진도를 믿는다.
    if review_state == status.CONFIRMED:
        row.addWidget(
            Badge(f"{status.symbol(status.CONFIRMED)} 확인함", "ok"),
            0, Qt.AlignmentFlag.AlignVCenter,
        )
    elif review_state in (status.WEAK, status.UNKNOWN):
        mark = Badge(f"{status.symbol(status.WEAK)} 확인 필요", "attention")
        mark.setToolTip("같은 업무로 묶을 근거가 부족합니다")
        row.addWidget(mark, 0, Qt.AlignmentFlag.AlignVCenter)
    return row


def _description(text: str) -> QLabel:
    """설명은 두 줄까지만. 카드마다 높이가 들쭉날쭉하면 격자가 어수선해진다.

    높이를 두 줄로 못 박는다. 줄바꿈 라벨에 높이를 맡기면 폭을 모르는 동안
    서너 줄을 요구하고, 격자는 그 값으로 카드 높이를 잡아 버린다.
    """
    label = QLabel(_clip(text, DESCRIPTION_CHARS))
    label.setObjectName("Muted")
    label.setWordWrap(True)
    label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
    label.setToolTip(text)
    label.ensurePolished()      # 스타일시트의 글자 크기가 적용된 뒤에 잰다
    label.setFixedHeight(label.fontMetrics().lineSpacing() * 2 + 2)
    # 줄바꿈 라벨도 자기 '한 줄 폭'을 최소 폭으로 주장한다. 그대로 두면
    # 카드가 좁아지지 못해 그리드 열 수 계산이 무너진다.
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
    return label


def _year_row(months: list[int] | None, cycle_text: str | None) -> QWidget:
    """달력 아이콘 + 월 스트립. 아이콘이 '이 띠는 한 해'라고 말한다.

    주기를 글로 다시 적지 않는다 — 띠의 숫자가 이미 말한다. 띠가 못 담는
    것(매월 12일의 '12일')은 풍선말에 둔다. 주기를 모를 때만 글로 그렇다고
    적는다. 빈 띠만 두면 못 찾은 것인지 덜 그려진 것인지 알 수 없다.
    """
    from .. import icons

    box = QWidget()
    column = QVBoxLayout(box)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(theme.SP_XS)
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(theme.SP_SM)
    tip = (
        f"연간 일정 · {cycle_text}\n1월부터 12월까지이고, 숫자가 적힌 칸이 "
        "이 업무를 하는 달입니다."
        if cycle_text else "반복 주기를 아직 찾지 못했습니다."
    )
    mark = icons.label("calendar", theme.TEXT_MUTED, 16)
    if mark is not None:
        mark.setToolTip(tip)
        row.addWidget(mark, 0, Qt.AlignmentFlag.AlignVCenter)
    strip = MonthStrip(months)
    strip.setToolTip(tip)
    row.addWidget(strip, 1)
    column.addLayout(row)
    if not cycle_text:
        label = QLabel("반복 주기 미확인")
        label.setObjectName("Small")
        label.setStyleSheet(f"color: {theme.TEXT_SUBTLE};")
        column.addWidget(label)
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
    if confidence == "low":
        row.addWidget(Badge("느슨한 묶음", "attention"))
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
