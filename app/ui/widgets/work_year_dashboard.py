"""모든 업무와 열두 달을 함께 펼치는 연간 일정판."""
from datetime import date

from PySide6.QtCore import Qt, Signal, QRectF, QEvent
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout, QHBoxLayout,
    QHeaderView, QAbstractItemView, QStyledItemDelegate, QStyle, QSizePolicy,
)
from ...core.work_schedule import select
from .. import theme
from ..views.cycle_format import parse_months


class _ScheduleCell(QStyledItemDelegate):
    """기호와 농도로 근거를 구분하고, 월별 활동을 작은 띠로 보여 준다."""

    def paint(self, painter, option, index):
        painter.save()
        painter.setFont(option.font)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        rect = option.rect
        painter.fillRect(rect, QColor(theme.BG if index.row() % 2 == 0 else theme.BAND))
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            painter.fillRect(rect, QColor(theme.PRIMARY_SOFT))
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        text = index.data() or ''
        if index.column() == 0:
            painter.setPen(QColor(theme.PRIMARY if selected else theme.TEXT))
            area = rect.adjusted(14, 0, -10, 0)
            painter.drawText(area, Qt.AlignmentFlag.AlignVCenter,
                             option.fontMetrics.elidedText(text, Qt.TextElideMode.ElideRight, area.width()))
        elif text != '—':
            confirmed = '✓' in text
            pattern = text == '~'
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.PRIMARY if confirmed else theme.PRIMARY_SOFT))
            painter.drawRoundedRect(QRectF(rect.adjusted(3, 7, -3, -7)), 5, 5)
            painter.setPen(QColor(theme.TEXT_ON_PRIMARY if confirmed else theme.TEXT_MUTED if pattern else theme.PRIMARY))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        else:
            painter.setPen(QColor(theme.TEXT_DISABLED))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, '·')
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setPen(QPen(QColor(theme.PRIMARY), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(rect.adjusted(1, 1, -1, -1)), 4, 4)
        painter.restore()


class WorkYearDashboard(QFrame):
    open_task = Signal(int)
    open_month = Signal(int, int, object, object)

    def __init__(self, cycles, current_year=None, parent=None, *, tasks=None, marks=()):
        super().__init__(parent)
        self.setObjectName('WorkYearDashboard')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.current_year = current_year
        tasks = tasks if tasks is not None else list({c['task_id']: dict(id=c['task_id'], name=c['task_name']) for c in cycles}.values())
        self._tasks = list(tasks)
        self._marks = list(marks)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        title = QLabel('연간 일정')
        title.setObjectName('SectionTitle')
        heading.addWidget(title)
        subtitle = QLabel(f'{len(tasks)}개 업무 · ' + (f'{current_year}년 기록' if current_year else '전체 기록'))
        subtitle.setObjectName('Small')
        heading.addWidget(subtitle)
        heading.addStretch()
        layout.addLayout(heading)
        self.table = QTableWidget(len(tasks), 13)
        self.table.setObjectName('YearScheduleTable')
        self.table.setAccessibleName('업무별 연간 일정 · 셀을 누르면 일정·진행으로 이동')
        today = date.today()
        self.table.setHorizontalHeaderLabels(['업무'] + [f'{m}월' for m in range(1, 13)])
        if current_year in (None, today.year):
            self.table.horizontalHeaderItem(today.month).setText(f'{today.month}월 •')
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setFrameShape(QFrame.Shape.NoFrame)
        self.table.setCursor(Qt.CursorShape.PointingHandCursor)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(20)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.setItemDelegate(_ScheduleCell(self.table))
        cycle_map = {c['task_id']: c for c in cycles}
        unknown = 0
        for row, task in enumerate(tasks):
            name = QTableWidgetItem(task['name'])
            name.setToolTip(task['name'])
            self.table.setItem(row, 0, name)
            cycle = cycle_map.get(task['id'])
            months = set(range(1, 13)) if cycle and cycle['kind'] == 'monthly' else set(parse_months(cycle['months']) or []) if cycle else set()
            if not months and not select(self._marks, task['id'], current_year):
                unknown += 1
                name.setToolTip(task['name'] + '\n시기 미확인 · 업무 목록에 포함하여 표시합니다')
            for month in range(1, 13):
                found = select(self._marks, task['id'], current_year, month)
                symbols = ''.join(s for kind, s in (('confirmed', '✓'), ('record', '◇')) if any(m.kind == kind for m in found))
                cell = QTableWidgetItem(symbols or ('~' if month in months else '—'))
                cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                cell.setToolTip('\n'.join(m.text for m in found) or ('과거 반복 패턴 · 올해 기한 아님' if month in months else '확인된 일정 없음'))
                self.table.setItem(row, month, cell)
        layout.addWidget(self.table)
        legend = QLabel('✓ 확인된 기한    ◇ 과거 기록    ~ 반복 시기    · 일정 미확인')
        legend.setObjectName('Small')
        legend.setWordWrap(True)
        layout.addWidget(legend)
        note = QLabel(f'시기 미확인 {unknown}개도 모두 표시합니다. 월을 누르면 근거와 진행 상황을 볼 수 있습니다.' if tasks else '검색 조건에 맞는 업무가 없습니다.')
        note.setObjectName('Small')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table.cellClicked.connect(self._open)
        self.table.cellActivated.connect(self._open)
        self._fit_table()

    def _fit_table(self):
        # 고정 높이로 잘라 숨기지 않고 모든 행을 펼친다. 월은 남은 폭을 균등 분할한다.
        row_height = max(32, self.table.fontMetrics().height() + 12)
        self.table.verticalHeader().setDefaultSectionSize(row_height)
        self.table.setFixedHeight(self.table.horizontalHeader().sizeHint().height() + row_height * len(self._tasks) + 2)
        self.table.setColumnWidth(0, max(120, min(240, int(self.table.viewport().width() * .26))))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_table()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(self, 'table'):
            self._fit_table()

    def _open(self, row, column):
        task_id = self._tasks[row]['id']
        if column == 0:
            self.open_task.emit(task_id)
        else:
            found = select(self._marks, task_id, self.current_year, column)
            case_id = next((m.case_id for m in found if m.case_id), None)
            self.open_month.emit(task_id, column, self.current_year, case_id)
