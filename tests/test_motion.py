"""전환 효과·동작 줄이기·카드 키보드 이동 (개편계획서 §14, UI06)."""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QWidget

from app.db import Database
from app.ui import motion, theme
from app.ui.views.settings import SettingsDialog
from app.ui.views.tasks import TasksView
from app.ui.widgets import WorkFolderCard

from test_work_workspace import seed, settle


@pytest.fixture(scope='module')
def qapp():
    app = QApplication.instance() or QApplication([])
    theme.apply(app)
    return app


@pytest.fixture
def view(tmp_path, qapp):
    db = Database(tmp_path / 'p.db')
    db.init()
    for n in (1, 2, 3, 4):
        seed(db, n)
    widget = TasksView(db)
    widget.resize(1280, 800)
    widget.show()
    settle(qapp)
    yield widget
    widget.stop()
    widget.close()
    widget.deleteLater()
    settle(qapp)
    db.close()


def test_reduced_mode_creates_no_effect(qapp):
    motion.set_mode('on')
    page = QWidget()
    controller = motion.MotionController()
    assert controller.reveal(page, 'enter') is False
    assert page.graphicsEffect() is None and not controller.running
    assert motion.duration('enter') == 0


def test_full_mode_reveals_then_cleans_up(qapp):
    motion.set_mode('off')
    page = QWidget()
    controller = motion.MotionController()
    assert controller.reveal(page, 'select') is True
    assert page.graphicsEffect() is not None and controller.running
    QTest.qWait(motion.DURATIONS['select'] + 150)
    assert page.graphicsEffect() is None and not controller.running


def test_rapid_reveal_leaves_only_latest_widget_with_effect(qapp):
    motion.set_mode('off')
    first, second = QWidget(), QWidget()
    controller = motion.MotionController()
    controller.reveal(first, 'tab')
    controller.reveal(second, 'tab')
    assert first.graphicsEffect() is None
    assert second.graphicsEffect() is not None
    controller.cancel()
    assert second.graphicsEffect() is None


def test_reveal_survives_destroyed_target(qapp):
    motion.set_mode('off')
    page = QWidget()
    controller = motion.MotionController()
    controller.reveal(page, 'tab')
    page.deleteLater()
    QApplication.sendPostedEvents(None, 52)
    controller.cancel()   # 이미 사라진 위젯이어도 오류 없이 정리된다


def test_mode_values_and_system_default(qapp, monkeypatch):
    assert motion.normalize('on') == 'on' and motion.normalize('x') == 'system'
    motion.set_mode('system')
    monkeypatch.setattr(motion, 'system_prefers_reduced', lambda: True)
    assert motion.is_reduced() is True
    monkeypatch.setattr(motion, 'system_prefers_reduced', lambda: False)
    assert motion.is_reduced() is False
    motion.set_mode('off')
    monkeypatch.setattr(motion, 'system_prefers_reduced', lambda: True)
    assert motion.is_reduced() is False   # 앱에서 끈 쪽이 시스템보다 우선한다


def test_user_navigation_reveals_but_data_refresh_does_not(view, qapp):
    motion.set_mode('off')
    view.open_task(1)
    assert view.motion.running and view.stack.currentWidget().graphicsEffect() is not None
    QTest.qWait(motion.DURATIONS['enter'] + 150)
    assert not view.motion.running
    view.refresh(force=True)
    assert not view.motion.running   # 자료 갱신은 움직임 없이 값만 바꾼다
    view._switch('guide')
    assert view.motion.running
    view._switch('docs')   # 연속 전환은 이전 효과를 정리한다
    assert view.pages['guide'].graphicsEffect() is None


def test_reduced_navigation_never_animates(view, qapp):
    motion.set_mode('on')
    view.open_task(2)
    view._switch('work')
    assert not view.motion.running
    assert view.stack.currentWidget().graphicsEffect() is None
    assert view._task_id == 2 and view._tab == 'work'


def test_settings_toggle_persists_and_applies(tmp_path, qapp):
    db = Database(tmp_path / 's.db')
    db.init()
    motion.set_mode('off')
    dialog = SettingsDialog(db)
    box = next(c for c in dialog.findChildren(QCheckBox) if c.accessibleName() == '애니메이션 줄이기')
    assert not box.isChecked()
    box.setChecked(True)
    assert db.get_meta('reduce_motion') == 'on' and motion.is_reduced()
    box.setChecked(False)
    assert db.get_meta('reduce_motion') == 'off' and not motion.is_reduced()
    motion.set_mode(None)
    assert motion.configure(db) == 'off'
    dialog.deleteLater()
    db.close()


def test_card_arrow_keys_move_focus_between_cards(view, qapp):
    cards = sorted(view.findChildren(WorkFolderCard), key=lambda c: (c.y(), c.x()))
    assert len(cards) == 4
    cards[0].setFocus()
    QTest.keyClick(cards[0], Qt.Key.Key_Right)
    assert QApplication.focusWidget() is cards[1]
    QTest.keyClick(cards[1], Qt.Key.Key_End)
    assert QApplication.focusWidget() is cards[-1]
    QTest.keyClick(cards[-1], Qt.Key.Key_Home)
    assert QApplication.focusWidget() is cards[0]
    QTest.keyClick(cards[0], Qt.Key.Key_Left)   # 맨 앞에서는 머문다
    assert QApplication.focusWidget() is cards[0]
    QTest.keyClick(cards[0], Qt.Key.Key_Return)
    settle(qapp)
    assert view._task_id == cards[0].task_id


def test_card_vertical_move_picks_nearest_column(view, qapp):
    view.resize(760, 800)   # 한 줄에 카드가 한 장씩 들어갈 때까지 좁힌다
    settle(qapp)
    cards = sorted(view.findChildren(WorkFolderCard), key=lambda c: (c.y(), c.x()))
    if len({c.y() for c in cards}) < 2:
        pytest.skip('창이 좁아도 한 줄 배치 — 세로 이동을 만들 수 없음')
    top = cards[0]
    below = top.neighbor(Qt.Key.Key_Down)
    assert below is not None and below.y() > top.y()
    assert below.neighbor(Qt.Key.Key_Up).y() < below.y()


def test_card_pressed_state_is_static_and_clears(view, qapp):
    card = view.findChildren(WorkFolderCard)[0]
    QTest.mousePress(card, Qt.MouseButton.LeftButton)
    assert card.property('pressed') is True
    QTest.mouseRelease(card, Qt.MouseButton.LeftButton)
    assert card.property('pressed') is False


def test_settings_toggle_for_link_review_persists(tmp_path, qapp):
    from app.core.knowledge import review
    db = Database(tmp_path / 's.db')
    db.init()
    dialog = SettingsDialog(db)
    box = next(c for c in dialog.findChildren(QCheckBox) if c.accessibleName() == '연결 근거 확인 보이기')
    assert not box.isChecked() and not review.enabled(db)
    box.setChecked(True)
    assert review.enabled(db)
    box.setChecked(False)
    assert not review.enabled(db)
    dialog.deleteLater()
    db.close()
