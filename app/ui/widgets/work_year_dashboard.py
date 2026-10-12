"""기록과 확인된 기한을 구분하는 연간 표."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout, QHeaderView, QAbstractItemView
from ...core.work_schedule import select
from ..views.cycle_format import parse_months


class WorkYearDashboard(QFrame):
    open_task = Signal(int)
    open_month = Signal(int, int, object, object)

    def __init__(self, cycles, current_year=None, parent=None, *, tasks=None, marks=()):
        super().__init__(parent)
        self.setObjectName('WorkYearDashboard')
        self.current_year = current_year
        tasks = tasks if tasks is not None else [dict(id=c['task_id'], name=c['task_name']) for c in cycles]
        self._tasks = list(tasks)
        self._marks = marks
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('연간 일정 · ' + (f'{current_year}년 자료' if current_year else '전체 기록')))
        legend = QLabel('✓ 확인된 수행 건 기한  ·  ◇ 과거 문서 기록  ·  ~ 반복 패턴  ·  — 시기 미상')
        legend.setWordWrap(True)
        layout.addWidget(legend)
        self.table = QTableWidget(len(tasks), 13)
        self.table.setAccessibleName('업무별 연간 일정 · 셀을 누르면 일정·진행으로 이동')
        self.table.setHorizontalHeaderLabels(['업무'] + [f'{m}월' for m in range(1, 13)])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 200)
        for column in range(1, 13):
            self.table.setColumnWidth(column, 64)
        cycle_map = {c['task_id']: c for c in cycles}
        for row, task in enumerate(tasks):
            name = QTableWidgetItem(task['name'])
            name.setToolTip(task['name'])
            self.table.setItem(row, 0, name)
            cycle = cycle_map.get(task['id'])
            months = set(range(1, 13)) if cycle and cycle['kind'] == 'monthly' else set(parse_months(cycle['months']) or []) if cycle else set()
            for month in range(1, 13):
                found = select(marks, task['id'], current_year, month)
                symbols = ''.join(s for kind, s in (('confirmed', '✓'), ('record', '◇')) if any(m.kind == kind for m in found))
                cell = QTableWidgetItem(symbols or ('~' if month in months else '—'))
                cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                cell.setToolTip('\n'.join(m.text for m in found) or ('과거 반복 패턴 · 올해 기한 아님' if month in months else '확인된 일정 없음'))
                self.table.setItem(row, month, cell)
        self.table.setMinimumHeight(150)
        self.table.setMaximumHeight(240)
        self.table.cellClicked.connect(self._open)
        self.table.cellActivated.connect(self._open)
        layout.addWidget(self.table)

    def _open(self, row, column):
        task_id = self._tasks[row]['id']
        if column == 0:
            self.open_task.emit(task_id)
        else:
            found = select(self._marks, task_id, self.current_year, column)
            case_id = next((m.case_id for m in found if m.case_id), None)
            self.open_month.emit(task_id, column, self.current_year, case_id)
