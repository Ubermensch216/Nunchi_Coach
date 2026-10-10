"""연간 일정 대시보드 위젯 (개편계획서 §3.1, §3.3 및 §13.1).

업무 홈에서 업무 카드들과 함께 연간 업무 흐름을 조망할 수 있는 컴팩트한 대시보드.
- 가로축: 1~12월
- 세로축: 업무 목록
- 확인된 기한 / 준비 권고 / 과거 반복 패턴 구분 표시
- 업무명 클릭 시 해당 업무의 '일정·진행'으로 바로 진입
"""

from __future__ import annotations

from datetime import date
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..widgets.common import muted_label
from .task_card import color_for

MONTH_NAMES = [f"{m}월" for m in range(1, 13)]
NAME_COL_W = 160
SLOT_H = 26


class WorkYearDashboard(QFrame):
    """업무 홈용 연간 일정 대시보드 위젯."""

    open_task = Signal(int)

    def __init__(
        self,
        cycles: list,
        current_year: int | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("WorkYearDashboard")
        self.setStyleSheet(
            """
            #WorkYearDashboard {
                background: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-radius: 8px;
            }
            """
        )
        self.current_year = current_year or date.today().year
        self.current_month = date.today().month

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        # 1. 헤더 (타이틀 + 연도 안내 + 범례)
        head_row = QHBoxLayout()
        head_row.setSpacing(8)

        title = QLabel(f"연간 일정 대시보드 ({self.current_year}년)")
        title.setStyleSheet("font-weight: 700; font-size: 13px; color: #1E293B;")
        head_row.addWidget(title)

        head_row.addStretch(1)

        legend = muted_label(
            "범례: ■ 반복 주기/확인된 시기  │  │ 이번 달  │  · 일정 없음",
            small=True,
        )
        head_row.addWidget(legend)
        layout.addLayout(head_row)

        # 2. 월 헤더 행
        header_row = QFrame()
        header_row.setFixedHeight(24)
        header_row.setStyleSheet("border-bottom: 1px solid #E2E8F0;")
        h_layout = QHBoxLayout(header_row)
        h_layout.setContentsMargins(4, 0, 4, 0)
        h_layout.setSpacing(0)

        col_name = QLabel("업무")
        col_name.setFixedWidth(NAME_COL_W)
        col_name.setStyleSheet("font-size: 11px; font-weight: 600; color: #64748B;")
        h_layout.addWidget(col_name)

        for m in range(1, 13):
            m_label = QLabel(f"{m}월")
            m_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if m == self.current_month:
                m_label.setStyleSheet(
                    "font-size: 11px; font-weight: 700; color: #0284C7; background: #E0F2FE; border-radius: 4px;"
                )
            else:
                m_label.setStyleSheet("font-size: 11px; color: #64748B;")
            h_layout.addWidget(m_label, 1)

        layout.addWidget(header_row)

        # 3. 업무별 행
        if not cycles:
            empty = muted_label("확인된 연간 일정이 없습니다. 업무가 분석되면 자동으로 표시됩니다.")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(empty)
        else:
            rows_container = QVBoxLayout()
            rows_container.setSpacing(2)
            for row in cycles:
                rows_container.addWidget(self._create_task_row(row))
            layout.addLayout(rows_container)

    def _create_task_row(self, row) -> QWidget:
        task_id = row["task_id"] if "task_id" in row.keys() else row.get("task_id")
        task_name = row["task_name"] if "task_name" in row.keys() else (row["name"] if "name" in row.keys() else "업무")
        active_months = self._parse_months(row)

        line = QFrame()
        line.setFixedHeight(SLOT_H)
        line.setStyleSheet(
            "QFrame:hover { background: #F8FAFC; border-radius: 4px; }"
        )
        l_layout = QHBoxLayout(line)
        l_layout.setContentsMargins(4, 0, 4, 0)
        l_layout.setSpacing(0)

        # 업무명 버튼
        btn = QPushButton(task_name)
        btn.setObjectName("Link")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedWidth(NAME_COL_W)
        btn.setStyleSheet(
            "text-align: left; font-size: 12px; font-weight: 500; color: #1E293B; border: none; background: transparent;"
        )
        btn.clicked.connect(lambda _=False, tid=task_id: self.open_task.emit(tid))
        l_layout.addWidget(btn)

        # 1~12월 셀
        accent = color_for(task_name)
        for m in range(1, 13):
            cell = QLabel()
            cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
            is_active = m in active_months
            is_now_month = m == self.current_month

            if is_active:
                cell.setText("●")
                cell.setStyleSheet(
                    f"color: {accent}; font-size: 11px;"
                    + (" background: #F0F9FF;" if is_now_month else "")
                )
            else:
                cell.setText("·")
                cell.setStyleSheet(
                    "color: #CBD5E1; font-size: 11px;"
                    + (" background: #F0F9FF;" if is_now_month else "")
                )

            l_layout.addWidget(cell, 1)

        return line

    def _parse_months(self, row) -> set[int]:
        from ..views.cycle_format import parse_months
        kind = row["kind"] if "kind" in row.keys() else row.get("kind")
        if kind == "monthly":
            return set(range(1, 13))
        raw = row["months"] if "months" in row.keys() else row.get("months")
        if not raw:
            return set()
        return set(parse_months(raw) or [])
