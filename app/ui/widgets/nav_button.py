"""메뉴 제목과 건수를 구분하되 클릭·키보드 영역은 하나로 유지한다."""
from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QStyle, QStyleOptionButton, QStylePainter

from .. import theme


class NavButton(QPushButton):
    def __init__(self, title, parent=None):
        super().__init__(title, parent)
        self.setObjectName('NavTab')
        self.count = 0
        row = QHBoxLayout(self)
        row.setContentsMargins(theme.SP_LG, theme.SP_SM, theme.SP_LG, theme.SP_SM)
        row.setSpacing(theme.SP_SM)
        self.title_label = QLabel(title)
        self.title_label.setObjectName('NavTitle')
        self.count_badge = QLabel()
        self.count_badge.setObjectName('NavCountBadge')
        self.count_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count_badge.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.count_badge.setMinimumWidth(22)
        for label in (self.title_label, self.count_badge):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            row.addWidget(label)
        self.count_badge.hide()
        self.toggled.connect(self._refresh_labels)
        self._refresh_labels()

    def _refresh_labels(self, *_):
        state = 'disabled' if not self.isEnabled() else 'selected' if self.isChecked() else 'normal'
        for label in (self.title_label, self.count_badge):
            label.setProperty('nav_state', state)
            label.style().unpolish(label)
            label.style().polish(label)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange and hasattr(self, 'title_label'):
            self._refresh_labels()

    def set_count(self, count):
        self.count = max(0, int(count))
        self.count_badge.setText(str(self.count))
        self.count_badge.setVisible(self.count > 0)
        self.setAccessibleName(f'{self.text()}, {self.count}건' if self.count else self.text())
        self.count_badge.setAccessibleName(f'{self.count}건')
        self.updateGeometry()

    def sizeHint(self):
        return super().sizeHint().expandedTo(self.layout().sizeHint())

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ''  # 제목·배지는 별도의 라벨로 그린다. 기본 텍스트 중복을 막는다.
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)
