"""업무 홈·상세의 실제 자료 연결, 맥락 격리와 실행 수명을 검증한다."""
import json
import os
import threading
import time
from datetime import date

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QComboBox, QPushButton, QScrollArea

from app.core import reconstruction as rc, task_manual, work_schedule
from app.db import Database
from app.ui import theme
from app.ui.views.tasks import TasksView, DETAIL_TABS
from app.ui.views.workbox import WorkboxDialog
from app.ui.views.work_workspace import STATE_KEY
from app.ui.widgets import WorkFolderCard


@pytest.fixture(scope='module')
def qapp():
    app = QApplication.instance() or QApplication([])
    theme.apply(app)
    return app


def seed(db, task_id=1, docs=3):
    db.con.execute('INSERT INTO tasks(id,name,description,confidence) VALUES(?,?,?,?)',
                   (task_id, f'업무 {task_id}', f'업무 {task_id} 설명', 'high'))
    source = db.add_source(db.path.parent / '자료')
    for index in range(docs):
        name = f'{task_id}_자료{index:03}.txt'
        doc_id = db.upsert_document(source, dict(path=str(db.path.parent / name), filename=name,
                    ext='.txt', hash=f'h{task_id}-{index}', parse_status='ok', eff_year=2024,
                    eff_date='2024-06-17', eff_month=6, eff_date_kind='body', eff_precision='day'))
        db.con.execute('INSERT INTO task_docs(task_id,doc_id) VALUES(?,?)', (task_id, doc_id))
    return task_id


@pytest.fixture
def project(tmp_path, qapp):
    db = Database(tmp_path / 'p.db')
    db.init()
    seed(db)
    seed(db, 2)
    widget = TasksView(db)
    widget.resize(1280, 800)
    widget.show()
    settle(qapp)
    yield db, widget
    widget.stop()
    widget.close()
    widget.deleteLater()
    settle(qapp)
    db.close()


def settle(app):
    for _ in range(3):
        app.processEvents()


def choose(widget, name):
    return next(c for c in widget.findChildren(QComboBox) if c.accessibleName() == name)


def words(widget):
    return '\n'.join(label.text() for label in widget.findChildren(QLabel))


def test_home_connects_all_tasks_and_keyboard_card(project, qapp):
    db, view = project
    assert view.dashboard.table.rowCount() == 2
    assert view.dashboard.table.item(0, 6).text() == '—'
    cards = view.findChildren(WorkFolderCard)
    assert len(cards) == 2
    cards[0].setFocus()
    QTest.keyClick(cards[0], Qt.Key.Key_Return)
    settle(qapp)
    assert view._task_id == cards[0].task_id
    assert view._tab == 'guide'
    assert list(dict(DETAIL_TABS).values()) == ['업무 안내', '일정·진행', '업무 자료', '매뉴얼', '근거']


def test_home_search_filters_cards_and_schedule_without_reordering(project):
    _, view = project
    view.home_search.setText('업무 2')
    assert view.dashboard.table.rowCount() == 1
    assert [card.task_id for _, card in view._home_cards if not card.isHidden()] == [2]
    view.home_search.clear()
    assert [card.task_id for _, card in view._home_cards] == [1, 2]


def test_home_shows_and_searches_analyzed_card_description(project, qapp):
    db, view = project
    db.con.execute("UPDATE tasks SET description='폴더의 문서 3건입니다.' WHERE id=1")
    db.add_task_item(1, rc.PURPOSE, '부서 실적을 취합해 목표 달성률을 파악합니다')
    db.add_task_item(1, rc.OUTPUT, '월간 실적 보고서')
    view.refresh(force=True)
    settle(qapp)
    card = next(card for task, card in view._home_cards if task['id'] == 1)
    assert '목표 달성률' in words(card)
    assert '월간 실적 보고서' in words(card)
    view.home_search.setText('목표 달성률')
    assert view.dashboard.table.rowCount() == 1
    assert [card.task_id for _, card in view._home_cards if not card.isHidden()] == [1]


def test_sidebar_search_preserves_current_task_and_keyboard_switch(project, qapp):
    _, view = project
    view.open_task(1)
    settle(qapp)
    view.sidebar.search.setText('업무 2')
    assert view._task_id == 1
    assert view.sidebar.list_widget.currentItem() is None
    view.sidebar.list_widget.setFocus()
    QTest.keyClick(view.sidebar.list_widget, Qt.Key.Key_Down)
    settle(qapp)
    assert view._task_id == 2
    assert view.sidebar.list_widget.currentItem().data(Qt.ItemDataRole.UserRole) == 2


def test_manual_shares_real_content_and_keeps_conflict(project, qapp):
    db, view = project
    item_id = db.add_task_item(1, rc.OUTPUT, '검증할 실제 결과물')
    conflict = db.add_task_item(1, rc.PROCEDURE, 'A자료: 제출 후 검토 / B자료: 검토 후 제출')
    db.con.execute("UPDATE task_items SET basis_state='conflict' WHERE id=?", (conflict,))
    note = db.add_task_item(1, rc.NOTE, '담당자가 보탠 실제 메모')
    db.con.execute("INSERT INTO evidence(owner_kind,owner_id,doc_id,doc_hash,locator,quote) VALUES('item',?,1,'h1-0','1쪽','실제 결과물 원문')", (item_id,))
    view.open_task(1)
    settle(qapp)
    guide, manual = words(view.pages['guide']), words(view.pages['manual'])
    assert 'user-' not in guide and 'user-' not in manual
    for text in ('검증할 실제 결과물', '자료가 서로 다릅니다', '담당자가 보탠 실제 메모', '준비물·관련 서식'):
        assert text in guide and text in manual
    proof_button = next(b for b in view.pages['guide'].findChildren(QPushButton) if b.text().startswith('근거'))
    proof_button.click()
    assert view._tab == 'evidence'
    assert view.stack.currentWidget() is view.evidence_page
    assert '실제 결과물 원문' in words(view.evidence_page)
    assert item_id in {i['id'] for i in db.task_items(1)}
    assert note in {i['id'] for i in db.task_items(1)}


def test_guide_shows_full_workflow_and_recipient_after_two_outputs(project, qapp):
    db, view = project
    db.con.execute("UPDATE tasks SET name='행정사무감사', description='문서 18건입니다.' WHERE id=1")
    for content in ('감사 제출자료', '증빙 목록', '추가 질의답변'):
        db.add_task_item(1, rc.OUTPUT, content)
    db.add_task_item(1, rc.RECIPIENT, '최종 수신처 의회사무국')
    view.open_task(1)
    settle(qapp)
    guide = words(view.pages['guide'])
    assert all(text in guide for text in ('지방의회', '감사 대상 기간', '감사 실시 기간',
                                         '내부 취합 마감까지', '최종 제출기한 전',
                                         '감사 진행 중·종료 후', '추가 질의답변', '최종 수신처 의회사무국'))
    assert '폴더의 문서' not in guide


def test_schedule_retains_same_date_for_different_tasks(project):
    db, _ = project
    for task_id in (1, 2):
        db.add_task_item(task_id, rc.DEADLINE, '2024-06-17', slot='동일 제출기한')
    found = work_schedule.marks(db)
    assert {m.task_id for m in found} == {1, 2}
    assert all(m.year == 2024 and m.kind == 'record' for m in found)


def test_home_date_opens_matching_year_month_case(project, qapp):
    db, view = project
    case_id, _ = db.open_case(1, 2026, '하반기')
    db.set_case_due(case_id, '2026-10-17', 'user')
    view.refresh(force=True)
    settle(qapp)
    view.home_year.setCurrentIndex(view.home_year.findData(2026))
    view.dashboard._open(0, 10)
    settle(qapp)
    assert view._task_id == 1 and view._tab == 'work'
    assert view._state()['year'] == 2026 and view._state()['month'] == 10
    assert view._state()['case'] == case_id
    assert '확인된 기한' in words(view.pages['work'])


def test_viewing_does_not_create_case_or_run(project, qapp):
    db, view = project
    view.open_task(1)
    view._switch('work')
    settle(qapp)
    assert db.cases() == [] and db.runs() == []


def test_task_switch_restores_tab_filters_and_scroll(project, qapp):
    db, view = project
    for index in range(25):
        db.add_task_item(1, rc.NOTE, f'상세 설명 {index}' * 10)
    view.open_task(1, 'manual')
    settle(qapp)
    scroll = view.pages['manual'].findChild(QScrollArea).verticalScrollBar()
    assert scroll.maximum() > 300
    scroll.setValue(300)
    view.open_task(2)
    settle(qapp)
    view.open_task(1)
    settle(qapp)
    assert view._tab == 'manual' and scroll.value() == 300
    view._switch('docs')
    settle(qapp)
    search = view.pages['docs'].findChild(QLineEdit)
    search.setText('자료001')
    view.open_task(2)
    settle(qapp)
    view.open_task(1)
    settle(qapp)
    assert view.pages['docs'].findChild(QLineEdit).text() == '자료001'
    view._switch('manual')
    settle(qapp)
    assert scroll.value() == 300


def test_rapid_switch_discards_previous_restore_and_closes_evidence(project, qapp):
    _, view = project
    view.open_task(1, 'manual')
    view.open_task(2, 'docs')
    view.open_task(1, 'work')
    settle(qapp)
    assert view._task_id == 1 and view._tab == 'work'
    assert view.stack.currentWidget() is view.pages['work']
    assert view.stack.currentWidget() is not view.evidence_page
    assert '2_자료' not in words(view.pages['docs'])


def test_refresh_preserves_search_focus_then_applies_new_data(project, qapp):
    db, view = project
    view.open_task(1, 'docs')
    settle(qapp)
    search = view.pages['docs'].findChild(QLineEdit)
    search.setFocus()
    search.setText('자료001')
    db.add_task_item(1, rc.NOTE, '새 분석 결과')
    view.refresh()
    assert view.pages['docs'].findChild(QLineEdit) is search
    assert search.text() == '자료001'
    assert view._refresh_timer.isActive()
    search.clearFocus()
    view._refresh_timer.start(0)
    settle(qapp)
    assert '새 분석 결과' in words(view.pages['manual'])
    assert view.pages['docs'].findChild(QLineEdit).text() == '자료001'


def test_removed_task_returns_home_without_stale_details(project, qapp):
    db, view = project
    view.open_task(1)
    settle(qapp)
    db.con.execute('UPDATE tasks SET merged_into=2 WHERE id=1')
    view.refresh()
    settle(qapp)
    assert view._task_id is None and view.stack.currentWidget() is view.list_page
    assert '1' not in view._ui_state['tasks']


def test_splitter_can_collapse_and_restore(project, qapp):
    _, view = project
    view.open_task(1)
    settle(qapp)
    view._toggle_sidebar()
    assert view.splitter.sizes()[0] == 0
    view._toggle_sidebar()
    assert view.splitter.sizes()[0] >= 200


def test_state_survives_reopen_and_is_project_local(project, qapp, tmp_path):
    db, view = project
    view.open_task(1, 'docs')
    settle(qapp)
    choose(view.pages['work'], '업무 일정 기록 연도').setCurrentIndex(1)
    view.stop()
    reopened = TasksView(db)
    reopened.open_task(1)
    settle(qapp)
    assert reopened._tab == 'docs' and reopened._state()['year'] == date.today().year
    reopened.close()
    reopened.deleteLater()
    other = Database(tmp_path / 'other.db')
    other.init()
    seed(other)
    isolated = TasksView(other)
    isolated.open_task(1)
    settle(qapp)
    assert isolated._tab == 'guide' and isolated._state().get('year') is None
    isolated.close()
    isolated.deleteLater()
    settle(qapp)
    other.close()


def test_documents_are_scoped_and_all_pages_accessible(tmp_path, qapp):
    db = Database(tmp_path / 'docs.db')
    db.init()
    seed(db, docs=105)
    seed(db, 2)
    view = TasksView(db)
    view.open_task(1, 'docs')
    settle(qapp)
    page = view.pages['docs']
    assert len([b for b in page.findChildren(QPushButton) if b.text().startswith('1_자료')]) == 100
    next(b for b in page.findChildren(QPushButton) if b.text() == '다음 자료').click()
    assert len([b for b in page.findChildren(QPushButton) if b.text().startswith('1_자료')]) == 5
    assert not any(b.text().startswith('2_자료') for b in page.findChildren(QPushButton))
    view.stop()
    view.close()
    view.deleteLater()
    settle(qapp)
    db.close()


def test_workbox_rejects_foreign_case(project):
    db, _ = project
    case_id, _ = db.open_case(2, 2024)
    with pytest.raises(ValueError, match='다른 업무'):
        WorkboxDialog(None, db, 1, case_id=case_id)


def test_run_survives_hiding_and_task_switch(project, qapp):
    db, view = project
    release = threading.Event()
    started = threading.Event()
    cancelled = []
    class FakeRunner:
        def __init__(self, database, should_stop, on_step):
            self.db, self.should_stop = database, should_stop
        def execute(self, run_id):
            started.set()
            release.wait(5)
            cancelled.append(self.should_stop())
            self.db.set_run_state(run_id, 'completed')
            return 'completed'
    dialog = WorkboxDialog(view, db, 1, runner_factory=FakeRunner, keep_running_on_close=True)
    view._workboxes[(1, date.today().year, None)] = dialog
    try:
        dialog.start()
        assert started.wait(2)
        run_id = db.runs(task_id=1)[0]['id']
        dialog.close()
        view.open_task(2)
        settle(qapp)
        assert dialog._job.running
        assert db.runs(task_id=2) == []
        release.set()
        limit = time.monotonic() + 5
        while dialog._job.running and time.monotonic() < limit:
            settle(qapp)
            time.sleep(0.01)
        settle(qapp)
        assert cancelled == [False]
        assert db.run(run_id)['state'] == 'completed'
        assert db.run(run_id)['task_id'] == 1
    finally:
        release.set()
        dialog._job.stop()


def test_home_scroll_and_search_survive_reopen(project, qapp):
    db, view = project
    for task_id in range(3, 18):
        seed(db, task_id, docs=0)
    view.refresh(force=True)
    settle(qapp)
    scroll = view.list_page.findChild(QScrollArea).verticalScrollBar()
    scroll.setValue(280)
    view.stop()
    reopened = TasksView(db)
    reopened.resize(1280, 800)
    reopened.show()
    settle(qapp)
    assert reopened.list_page.findChild(QScrollArea).verticalScrollBar().value() == 280
    reopened._show_schedule()
    settle(qapp)
    assert reopened.list_page.findChild(QScrollArea).verticalScrollBar().value() == 0
    reopened.stop()
    reopened.close()
    reopened.deleteLater()
    settle(qapp)


def test_workbox_year_and_case_scope_excludes_other_runs(project):
    from app.actions import catalog
    db, view = project
    _, old_id = catalog.prepare(db, 1, 2024)
    _, new_id = catalog.prepare(db, 1, 2026)
    dialog = WorkboxDialog(None, db, 1, year=2024)
    assert dialog._current_run()['id'] == old_id
    dialog.year.setValue(2026)
    assert dialog._current_run()['id'] == new_id
    case_id, _ = db.open_case(1, 2026, '별도 회차')
    scoped = WorkboxDialog(None, db, 1, year=2026, case_id=case_id)
    assert scoped._current_run() is None
    assert not scoped.year.isEnabled()
    dialog.close()
    scoped.close()


def test_reopening_selected_case_reuses_running_service(project, qapp):
    from app.actions import catalog
    db, view = project
    view._open_workbox(1)
    dialog = next(iter(view._workboxes.values()))
    _, run_id = catalog.prepare(db, 1, date.today().year)
    dialog._run_id = run_id
    dialog._case_id = db.run(run_id)['case_id']
    view.open_task(1, 'work')
    settle(qapp)
    case = choose(view.pages['work'], '업무 수행 건')
    case.setCurrentIndex(case.findData(dialog._case_id))
    view._open_workbox(1)
    assert len(view._workboxes) == 1
    assert next(iter(view._workboxes.values())) is dialog
    dialog.close()


def test_manual_preserves_changed_source_warning(project, qapp):
    db, view = project
    item_id = db.add_task_item(1, rc.OUTPUT, '원문에서 복원한 결과물')
    db.add_evidence('item', item_id, 1, 'old-hash', '2쪽', '이전 판의 문장', '이전자료.txt', '이전자료.txt')
    view.open_task(1, 'manual')
    settle(qapp)
    button = next(b for b in view.pages['manual'].findChildren(QPushButton) if b.text().startswith('근거'))
    button.click()
    assert '원문이 바뀌었거나 사라졌습니다' in words(view.evidence_page)


def test_evidence_tab_uses_full_width_and_keeps_tasks_separate(project, qapp):
    db, view = project
    item_id = db.add_task_item(1, rc.OUTPUT, '결과물')
    db.add_evidence('item', item_id, 1, 'h1-0', '2쪽', '첫 업무의 고유한 원문', '자료.txt', '자료.txt')
    view.open_task(1, 'evidence')
    settle(qapp)
    assert view.stack.currentWidget() is view.evidence_page
    assert view.evidence_page.width() == view.stack.width()
    assert '첫 업무의 고유한 원문' in words(view.evidence_page)
    assert not hasattr(view, 'drawer')
    assert not any(button.objectName() == 'Link' and button.text().startswith('근거 ')
                   for button in view.head.parentWidget().findChildren(QPushButton))
    view.open_task(2, 'evidence')
    settle(qapp)
    assert '첫 업무의 고유한 원문' not in words(view.evidence_page)
    view.open_task(1)
    settle(qapp)
    assert view._tab == 'evidence'
    assert '첫 업무의 고유한 원문' in words(view.evidence_page)


def seed_reading(db):
    for task_id in (1, 2):
        for rank, doc in enumerate(db.task_documents(task_id), 1):
            db.con.execute('INSERT INTO task_reading(task_id,doc_id,ordinal,reason) VALUES(?,?,?,?)',
                           (task_id, doc['id'], rank, f'업무 {task_id}의 추천 이유 {rank}'))
            db.replace_sections(doc['id'], [('paragraph', 1, '1문단', f'업무 {task_id}의 문서 본문 {rank}')])


def test_reading_button_moves_into_first_steps_without_footer_shortcuts(project, qapp):
    db, view = project
    seed_reading(db)
    view.open_task(1, 'guide')
    settle(qapp)
    button = view.reading_button
    assert button.parentWidget().findChildren(QLabel)[0].text() == '처음 맡으면 할 일'
    buttons = view.pages['guide'].findChildren(QPushButton)
    assert [b.text() for b in buttons].count('먼저 읽을 문서') == 1
    assert not {'일정·진행', '업무 자료', '매뉴얼'} & {b.text() for b in buttons}
    assert view.reading_panel.isHidden()
    button.click()
    settle(qapp)
    assert view._tab == 'guide' and view.stack.currentWidget() is view.pages['guide']
    assert view.reading_panel.isVisible() and button.isChecked()
    assert view.reading_panel.geometry().left() >= view.stack.geometry().right()
    assert '업무 1의 추천 이유 1' in words(view.reading_panel)
    button.click()
    assert view.reading_panel.isHidden() and not button.isChecked()
    button.click()
    close = view.reading_panel.close_button
    assert close.text() == '' and not close.icon().isNull()
    close.click()
    assert view.reading_panel.isHidden() and not button.isChecked()


def test_reading_panel_previews_and_marks_documents_without_leaving_guide(project, qapp):
    db, view = project
    seed_reading(db)
    view.open_task(1, 'guide')
    settle(qapp)
    view.reading_button.click()
    panel = view.reading_panel
    filename = next(b for b in panel.list_page.findChildren(QPushButton) if b.text() == '1_자료000.txt')
    filename.click()
    assert view._tab == 'guide'
    assert panel.stack.currentWidget() is panel.preview
    assert '업무 1의 문서 본문 1' in words(panel.preview)
    assert '업무 1의 추천 이유 1' in words(panel.preview)
    panel.back_button.click()
    assert panel.stack.currentWidget() is panel.list_page
    read = next(b for b in panel.list_page.findChildren(QPushButton) if b.text() == '읽음 표시')
    read.click()
    settle(qapp)
    assert db.task_reading(1)[0]['doc_id'] in db.reading_marks()
    assert panel.isVisible() and view.reading_button.isChecked() and view._tab == 'guide'
    assert any(b.text() == '읽음' for b in panel.list_page.findChildren(QPushButton))
    view.open_task(2, 'guide')
    settle(qapp)
    assert panel.isHidden() and not view.reading_button.isChecked()
    view.reading_button.click()
    assert '업무 2의 추천 이유 1' in words(panel.list_page)
    assert '업무 1의 추천 이유' not in words(panel.list_page)
    view._switch('docs')
    assert panel.isHidden()


def test_reading_panel_handles_empty_list_and_closes_on_home(project, qapp):
    _, view = project
    view.open_task(1, 'guide')
    settle(qapp)
    view.reading_button.click()
    assert '추천할 문서를 고르지 못했습니다.' in words(view.reading_panel)
    view.back()
    settle(qapp)
    assert view.reading_panel.isHidden()


def test_reading_panel_reflows_guide_bullets_in_a_narrow_window(project, qapp):
    from app.ui.widgets.work_brief import BulletList

    db, view = project
    seed_reading(db)
    theme.apply(qapp, 'large', 'dark')
    try:
        view.resize(1120, 800)
        view.open_task(1, 'guide')
        settle(qapp)
        view.reading_button.click()
        for _ in range(10):
            qapp.processEvents()
        for bullets in view.pages['guide'].findChildren(BulletList):
            grid = bullets.layout()
            for row in range(1, grid.rowCount()):
                previous = grid.itemAtPosition(row - 1, 1).widget()
                current = grid.itemAtPosition(row, 1).widget()
                assert previous.geometry().bottom() < current.geometry().top()
        assert view.reading_panel.list_page.findChild(QScrollArea).horizontalScrollBar().maximum() == 0
        assert view.reading_panel.width() >= 300 and view.stack.width() >= 400
    finally:
        theme.apply(qapp, 'medium', 'light')


def test_evidence_link_can_return_to_all_evidence_and_handle_empty_context(project, qapp):
    _, view = project
    view.open_task(1, 'guide')
    settle(qapp)
    assert view.evidence_page.isHidden()
    view._show_evidence([], '누락된 항목의 근거')
    settle(qapp)
    assert view._tab == 'evidence'
    assert '이 주장에 연결된 근거를 찾지 못했습니다.' in words(view.evidence_page)
    assert view.evidence_page.all_button.isVisible()
    view.evidence_page.all_button.click()
    assert not view.evidence_page.all_button.isVisible()
    assert '1_자료000.txt' in words(view.evidence_page)


def test_year_case_and_month_restore_per_task(project, qapp):
    db, view = project
    case_id, _ = db.open_case(1, 2024, '상반기')
    view.open_task(1, 'work')
    settle(qapp)
    year = choose(view.pages['work'], '업무 일정 기록 연도')
    year.setCurrentIndex(year.findData(2024))
    month = choose(view.pages['work'], '업무 일정 월')
    month.setCurrentIndex(month.findData(6))
    case = choose(view.pages['work'], '업무 수행 건')
    case.setCurrentIndex(case.findData(case_id))
    view.open_task(2, 'work')
    settle(qapp)
    assert view._state()['case'] is None and view._state()['year'] is None
    view.open_task(1)
    settle(qapp)
    assert choose(view.pages['work'], '업무 일정 기록 연도').currentData() == 2024
    assert choose(view.pages['work'], '업무 일정 월').currentData() == 6
    assert choose(view.pages['work'], '업무 수행 건').currentData() == case_id


@pytest.mark.parametrize('raw', ['{bad-json', '[]', '{"splitter":"bad","tasks":[],"home":3}'])
def test_invalid_saved_state_falls_back_to_defaults(project, qapp, raw):
    db, _ = project
    db.set_meta(STATE_KEY, raw)
    reopened = TasksView(db)
    reopened.open_task(1)
    settle(qapp)
    assert reopened._tab == 'guide'
    reopened.stop()
    reopened.close()
    reopened.deleteLater()


def test_background_connection_updates_keep_current_task(project, qapp):
    db, view = project
    view.open_task(2, 'manual')
    settle(qapp)
    other = Database(db.path)
    other.add_task_item(1, rc.NOTE, 'A업무 뒤늦은 결과')
    other.add_task_item(2, rc.NOTE, 'B업무 새 결과')
    other.close()
    view.refresh()
    settle(qapp)
    assert view._task_id == 2 and view._tab == 'manual'
    assert 'A업무 뒤늦은 결과' not in words(view.pages['manual'])
    assert 'B업무 새 결과' in words(view.pages['manual'])


def test_reset_guide_replaces_pending_scroll_restore(project, qapp):
    db, view = project
    for i in range(15):
        db.add_task_item(1, rc.NOTE, f"담당자 메모 {i}")
    view.open_task(1, "guide")
    settle(qapp)
    scroll = view.pages["guide"].findChild(QScrollArea).verticalScrollBar()
    scroll.setValue(200)
    assert scroll.value() == 200
    view._switch("manual")
    settle(qapp)
    view._reset_guide()
    settle(qapp)
    assert view._tab == "guide" and scroll.value() == 0
    scroll.setValue(200)
    view._reset_guide()
    settle(qapp)
    assert scroll.value() == 0


def test_due_date_year_and_case_year_remain_distinct(project, qapp):
    db, view = project
    case_id, _ = db.open_case(1, 2026, "연말 회차")
    db.set_case_due(case_id, "2027-01-05", "user")
    view._open_schedule(1, 1, 2027, case_id)
    settle(qapp)
    assert view._state()["year"] == 2027 and view._state()["case"] == case_id
    assert "5일" in words(view.pages["work"])
    view._open_workbox(1)
    dialog = next(iter(view._workboxes.values()))
    assert dialog.year.value() == 2026 and dialog._case_id == case_id
    assert db.runs() == []
    dialog.close()


@pytest.mark.parametrize("close_first", [True, False])
def test_destroying_workspace_cancels_pending_restore(project, qapp, close_first):
    from PySide6.QtCore import QCoreApplication, QEvent
    db, _ = project
    pending = TasksView(db)
    pending.open_task(1, "manual")
    assert pending._restore_timer.isActive()
    if close_first:
        pending.close()
        assert not pending._restore_timer.isActive()
    pending.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    settle(qapp)


def test_manual_tab_shows_notebook_version_state_without_creating_one(project, qapp):
    from app.core import manual_versions
    db, view = project
    view.open_task(1, 'manual')
    settle(qapp)
    assert '아직 업무노트에 넣은 판이 없습니다' in words(view.pages['manual'])
    assert manual_versions.latest(db, 1) is None          # 열람만으로 판본을 만들지 않는다
    db.con.execute("INSERT INTO manual_versions(task_id,version,digest,status) VALUES (1,3,'old','ok')")
    view.refresh(force=True)
    settle(qapp)
    text = words(view.pages['manual'])
    assert '판본 3' in text and '업무 내용이 바뀌었습니다' in text
    assert '새 판을 업무노트에 넣기' in [b.text() for b in view.pages['manual'].findChildren(QPushButton)]
    assert '판본·업무노트' not in words(view.pages['guide'])    # 안내에는 이력 카드를 싣지 않는다


def _linked_project(project):
    from app.core.knowledge import projection
    db, view = project
    db.con.execute("INSERT INTO task_items(task_id,kind,slot,content,basis_state,applicability) "
                   "VALUES (1,'recipient','recipient','복지과','stated','past')")
    projection.project_task(db, 1)
    return db, view


def test_link_card_is_hidden_until_flag_and_decisions_do_not_touch_sources(project, qapp):
    from app.core.knowledge import review
    db, view = _linked_project(project)
    view.open_task(1, 'manual')
    settle(qapp)
    assert '연결 근거' not in words(view.pages['manual'])       # 기본은 꺼짐 — 인계 v2 전 내부 사용
    db.set_meta(review.FLAG, '1')
    view.refresh(force=True)
    settle(qapp)
    text = words(view.pages['manual'])
    assert '연결 근거' in text and '제출처 · 복지과' in text and '확인 전' in text
    before = [tuple(r) for r in db.con.execute('SELECT * FROM task_items ORDER BY id')]
    buttons = {b.text(): b for b in view.pages['manual'].findChildren(QPushButton)}
    buttons['아님'].click()
    settle(qapp)
    assert '아니라고 거절함' in words(view.pages['manual'])
    assert [tuple(r) for r in db.con.execute('SELECT * FROM task_items ORDER BY id')] == before
    next(b for b in view.pages['manual'].findChildren(QPushButton) if b.text() == '되돌리기').click()
    settle(qapp)
    assert '확인 전' in words(view.pages['manual'])
