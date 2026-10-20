"""실제 Qt 폰트·크기·카드 높이와 수동 렌더링의 일관성을 검증한다."""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtGui import QFont, QFontInfo, QPainter, QImage
from PySide6.QtWidgets import QApplication, QLabel, QWidget, QVBoxLayout, QStyleOptionViewItem

from app.ui import theme
from app.ui.widgets import FlowGrid, WorkFolderCard, WorkYearDashboard


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    theme.apply(app, 'medium', 'light')


@pytest.mark.parametrize('size', theme.TEXT_SIZE_ORDER)
def test_text_roles_use_one_family_real_weights_and_consistent_sizes(qapp, size):
    theme.apply(qapp, size, 'light')
    holder = QWidget()
    column = QVBoxLayout(holder)
    roles = [('ViewTitle', 'title', 700), ('SectionTitle', 'section', 600),
             ('FolderCardTitle', 'lead', 600), ('NotebookCardTitle', 'lead', 600),
             ('FolderCardDesc', 'body', 400), ('Muted', 'body', 400), ('Small', 'small', 400)]
    for name, role, weight in roles:
        label = QLabel('한글 제목과 업무 설명 123')
        label.setObjectName(name)
        column.addWidget(label)
    holder.show()
    qapp.processEvents()
    try:
        for label, (_, role, weight) in zip(holder.findChildren(QLabel), roles):
            actual = QFontInfo(label.font())
            assert actual.family() == theme.font_family()
            assert label.font().pointSizeF() == pytest.approx(theme.text_px(role, size) * .75)
            assert actual.weight() == weight
            assert label.font().styleStrategy() & QFont.StyleStrategy.PreferAntialias
        assert qapp.font().pointSizeF() == pytest.approx(theme.text_px('body', size) * .75)
    finally:
        holder.close()


@pytest.mark.parametrize('size', theme.TEXT_SIZE_ORDER)
def test_card_description_and_month_numbers_fit_and_follow_settings(qapp, size):
    theme.apply(qapp, size, 'light')
    grid = FlowGrid(min_card_width=300, max_columns=3)
    for index in range(3):
        card = WorkFolderCard(index, '월간실적보고', '부서별 실적을 취합하여 목표 달성 현황을 정리하고 월간 보고서를 작성하는 업무입니다. ' * (index + 1),
                              span_text='자료 24건 · 2024~2025', months=list(range(1, 13)), next_action='부서별 실적 취합')
        grid.add_card(card)
    grid.resize(1000, grid.minimumHeight())
    grid.show()
    for _ in range(8):
        qapp.processEvents()
    try:
        for card in grid._cards:
            desc = card.findChild(QLabel, 'FolderCardDesc')
            assert desc.height() >= desc.heightForWidth(desc.width())
            for label in card.month_strip.findChildren(QLabel):
                assert label.font().pointSizeF() == pytest.approx(theme.text_px('small', size) * .75)
                assert label.height() >= label.fontMetrics().height()
            assert desc.geometry().bottom() < card.month_strip.y()
    finally:
        grid.close()


def test_schedule_delegate_uses_table_font_in_every_column(qapp):
    theme.apply(qapp, 'large', 'light')
    widget = WorkYearDashboard([], tasks=[dict(id=1, name='실적보고')])
    delegate = widget.table.itemDelegate()
    option = QStyleOptionViewItem()
    option.font = widget.table.font()
    option.rect.setRect(0, 0, 200, 40)
    class PainterSpy:
        def __init__(self, painter):
            self.painter = painter
            self.fonts = []
        def __getattr__(self, name):
            return getattr(self.painter, name)
        def drawText(self, *args):
            self.fonts.append(self.painter.font())
            self.painter.drawText(*args)
    surface = QImage(200, 40, QImage.Format.Format_ARGB32_Premultiplied)
    surface.fill(0)
    painter = QPainter(surface)
    spy = PainterSpy(painter)
    try:
        for column in (0, 1, 12):
            delegate.paint(spy, option, widget.table.model().index(0, column))
        assert len(spy.fonts) == 3
        assert all(font == option.font for font in spy.fonts)
    finally:
        painter.end()
        widget.close()
