"""제목과 숫자의 시각·접근성 구분 및 하나의 메뉴 클릭 영역을 검증한다."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.ui import theme
from app.ui.widgets.nav_button import NavButton


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    theme.apply(app, 'medium', 'light')


@pytest.mark.parametrize('count,size,mode', [(4,'medium','light'), (120,'large','dark')])
def test_count_is_separate_and_badge_click_and_keyboard_activate_menu(qapp, count, size, mode):
    theme.apply(qapp, size, mode)
    button = NavButton('챙길 일')
    button.setCheckable(True)
    button.set_count(count)
    button.resize(button.sizeHint())
    button.show()
    qapp.processEvents()
    clicked = []
    button.clicked.connect(lambda checked: clicked.append(checked))
    try:
        assert button.text() == button.title_label.text() == '챙길 일'
        assert button.count_badge.text() == str(count)
        assert button.title_label.geometry().right() < button.count_badge.geometry().left()
        assert button.count_badge.geometry().right() < button.width()
        assert str(count) + '건' in button.accessibleName()
        pos = button.count_badge.geometry().center()
        QTest.mouseClick(button, Qt.MouseButton.LeftButton, pos=pos)
        assert clicked == [True]
        assert button.title_label.palette().color(button.title_label.foregroundRole()) == QColor(theme.PRIMARY)
        button.setFocus()
        QTest.keyClick(button, Qt.Key.Key_Space)
        assert clicked == [True, False]
        button.set_count(0)
        assert button.count_badge.isHidden() and button.text() == '챙길 일'
    finally:
        button.close()
