"""업무 안내를 유지하면서 추천 문서와 원문 대목을 읽는 오른쪽 패널."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QStackedWidget, QVBoxLayout

from .. import icons, theme
from .common import Page, section_title
from .evidence_drawer import EvidenceDrawer


class ReadingSidecar(QFrame):
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('ReadingSidecar')
        self.setAccessibleName('먼저 읽을 문서 패널')
        self.setMinimumWidth(300)
        self.doc_id = None
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(theme.SP_MD, theme.SP_MD, theme.SP_MD, theme.SP_MD)
        self.back_button = self._icon_button('arrow-left', '문서 목록으로 돌아가기')
        self.back_button.clicked.connect(self.show_list)
        head.addWidget(self.back_button)
        head.addWidget(section_title('먼저 읽을 문서'), 1)
        self.close_button = self._icon_button('close', '먼저 읽을 문서 닫기')
        self.close_button.clicked.connect(self.closed.emit)
        head.addWidget(self.close_button)
        column.addLayout(head)
        self.stack = QStackedWidget()
        self.list_page = Page()
        self.preview = EvidenceDrawer(embedded=True)
        self.stack.addWidget(self.list_page)
        self.stack.addWidget(self.preview)
        column.addWidget(self.stack, 1)
        self.show_list()
        self.hide()

    def _icon_button(self, icon, label):
        button = QPushButton()
        button.setObjectName('IconButton')
        button.setIcon(icons.icon(icon, theme.TEXT_MUTED, 16))
        button.setFixedSize(28, 28)
        button.setToolTip(label)
        button.setAccessibleName(label)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def show_list(self):
        self.doc_id = None
        self.stack.setCurrentWidget(self.list_page)
        self.back_button.hide()

    def show_document(self, doc_id, items):
        self.doc_id = doc_id
        self.preview.show_evidence(items, '문서 미리보기')
        self.stack.setCurrentWidget(self.preview)
        self.back_button.show()
