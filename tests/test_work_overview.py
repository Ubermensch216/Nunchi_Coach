"""연간 일정의 전체 행 노출과 자료에 근거한 카드 개요를 검증한다."""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication
from app.core import reconstruction as rc, workstate as ws
from app.core.task_description import describe
from app.core.workcard import Card
from app.core.work_schedule import Mark
from app.ui import theme
from app.ui.widgets import WorkYearDashboard


@pytest.fixture(scope='module')
def qapp():
    app = QApplication.instance() or QApplication([])
    theme.apply(app)
    return app


@pytest.mark.parametrize('count,width', [(7, 760), (18, 1100), (32, 760)])
def test_schedule_exposes_every_row_and_month_without_inner_scrolling(qapp, count, width):
    tasks = [dict(id=i, name=f'업무 {i}') for i in range(count)]
    widget = WorkYearDashboard([], tasks=tasks)
    widget.resize(width, widget.sizeHint().height())
    widget.show()
    qapp.processEvents()
    table = widget.table
    assert table.rowCount() == count
    assert table.verticalScrollBar().maximum() == 0
    assert table.horizontalScrollBar().maximum() == 0
    assert table.visualItemRect(table.item(count - 1, 12)).bottom() <= table.viewport().height()
    assert table.visualItemRect(table.item(0, 12)).right() <= table.viewport().width()
    widget.close()


def test_schedule_preserves_evidence_kinds_and_navigation(qapp):
    found = [Mark(1, 2026, 10, '확인한 기한', 'confirmed', 9),
             Mark(1, 2024, 10, '과거 기록', 'record')]
    widget = WorkYearDashboard([dict(task_id=1, kind='monthly', months='')],
                               tasks=[dict(id=1, name='실적보고')], marks=found)
    assert widget.table.item(0, 10).text() == '✓◇'
    assert widget.table.item(0, 9).text() == '~'
    received = []
    widget.open_month.connect(lambda *args: received.append(args))
    widget._open(0, 10)
    assert received == [(1, 10, None, 9)]
    widget.close()


def item(kind, text, basis=ws.STATED):
    return dict(kind=kind, content=text, basis_state=basis, human_state='none')


def test_description_uses_analyzed_purpose_and_output_instead_of_file_count():
    task = dict(name='월간실적보고', description='‘월간실적보고’ 폴더의 문서 24건입니다.', origin='ai')
    card = Card(1, task['name'], todo=[item(rc.PURPOSE, '월별 부서 실적을 취합해 목표 대비 달성 현황을 파악합니다'),
                                     item(rc.OUTPUT, '월간 실적 보고서')])
    text = describe(task, card)
    assert '월별 부서 실적을 취합' in text and '월간 실적 보고서' in text
    assert '24건' not in text


def test_description_cleans_historical_filenames_and_explains_steps():
    task = dict(name='월간실적보고', description='문서 24건입니다.', origin='ai')
    card = Card(1, task['name'], todo=[item(rc.OUTPUT, '2025_월간실적보고_실적취합_송부.xlsx')],
                precedent_steps=[dict(label='부서 실적 요청'), dict(label='실적 취합')])
    text = describe(task, card)
    assert '과거 자료에 따르면 부서 실적 요청 → 실적 취합' in text
    assert '‘실적취합’' in text and '.xlsx' not in text
    card.todo = [item(rc.OUTPUT, '2024_수질통계_분기수질통계_송부.csv')]
    task['name'] = '수질통계'
    assert '‘분기수질통계’' in describe(task, card)
    card.todo = [item(rc.OUTPUT, '2025년 수질통계 수정 보고서')]
    assert '‘2025년 수질통계 수정 보고서’' in describe(task, card)


def test_description_preserves_user_edits_and_does_not_invent_missing_purpose():
    task = dict(name='실적보고', description='문서 3건의 실적을 취합하는 업무입니다.', origin='user')
    assert describe(task, Card(1, task['name'])) == task['description']
    task.update(description='문서 3건입니다.', origin='ai')
    card = Card(1, task['name'], todo=[item(rc.PURPOSE, '충돌한 목적', ws.CONFLICT)])
    assert '근거가 부족' in describe(task, card)
    assert '충돌한 목적' not in describe(task, card)
