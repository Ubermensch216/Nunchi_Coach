import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget, QHBoxLayout, QLabel, QPushButton
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QPixmap

from app.ui import icons, theme
from app.ui.views.notebooks.panel import icon_button, set_icon

app = QApplication.instance() or QApplication([])
app.setStyleSheet(theme.stylesheet())

icons._SHAPES["arrow-left"] = '<path d="M19 12H5"/><path d="m12 19-7-7 7-7"/>'
icons._SHAPES["external-link"] = '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>'

# Case A: IconTool (transparent by default, subtle hover)
w_a = QWidget()
w_a.setStyleSheet(f"background: {theme.BG};")
l_a = QHBoxLayout(w_a)
l_a.setContentsMargins(12, 12, 12, 12)
back_a = icon_button("arrow-left", "자료 목록")
l_a.addWidget(back_a)
l_a.addStretch(1)
open_a = QPushButton(" 원본")
open_a.setObjectName("Quiet")
open_a.setCursor(Qt.CursorShape.PointingHandCursor)
open_a.setIcon(icons.icon("external-link", theme.TEXT_MUTED, 16))
open_a.setIconSize(QSize(16, 16))
l_a.addWidget(open_a)
w_a.resize(320, 60)
p_a = QPixmap(320, 60)
w_a.render(p_a)
p_a.save("test_head_icontool.png")

# Case B: Quiet (pill with light gray background)
w_b = QWidget()
w_b.setStyleSheet(f"background: {theme.BG};")
l_b = QHBoxLayout(w_b)
l_b.setContentsMargins(12, 12, 12, 12)
back_b = QPushButton()
back_b.setObjectName("Quiet")
back_b.setCursor(Qt.CursorShape.PointingHandCursor)
back_b.setToolTip("자료 목록")
back_b.setIcon(icons.icon("arrow-left", theme.TEXT_MUTED, 16))
back_b.setIconSize(QSize(16, 16))
back_b.setFixedSize(32, 32)
l_b.addWidget(back_b)
l_b.addStretch(1)
open_b = QPushButton(" 원본")
open_b.setObjectName("Quiet")
open_b.setCursor(Qt.CursorShape.PointingHandCursor)
open_b.setIcon(icons.icon("external-link", theme.TEXT_MUTED, 16))
open_b.setIconSize(QSize(16, 16))
l_b.addWidget(open_b)
w_b.resize(320, 60)
p_b = QPixmap(320, 60)
w_b.render(p_b)
p_b.save("test_head_quiet.png")

print("Both head cases rendered!")
