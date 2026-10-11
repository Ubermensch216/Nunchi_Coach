import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget, QHBoxLayout, QLabel, QPushButton
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QPixmap

from app.ui import icons, theme
from app.ui.views.notebooks.panel import icon_button, set_icon

app = QApplication.instance() or QApplication([])
app.setStyleSheet(theme.stylesheet())

# Let's test arrow-left
arrow_left_svg = '<path d="M19 12H5"/><path d="m12 19-7-7 7-7"/>'
icons._SHAPES["arrow-left"] = arrow_left_svg

# Let's test external-link
external_link_svg = '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>'
icons._SHAPES["external-link"] = external_link_svg

# Render header variant 1: arrow-left (icon only) + external-link + "원본"
w1 = QWidget()
w1.setStyleSheet(f"background: {theme.BG};")
l1 = QHBoxLayout(w1)
l1.setContentsMargins(12, 12, 12, 12)

back1 = icon_button("arrow-left", "자료 목록")
l1.addWidget(back1)
l1.addStretch(1)

open1 = QPushButton(" 원본")
open1.setObjectName("Quiet")
open1.setCursor(Qt.CursorShape.PointingHandCursor)
open1.setIcon(icons.icon("external-link", theme.TEXT_MUTED, 16))
open1.setIconSize(QSize(16, 16))
l1.addWidget(open1)

w1.resize(320, 60)
p1 = QPixmap(320, 60)
w1.render(p1)
p1.save("test_header_variant1.png")

# Render header variant 2: arrow-left (icon only) + documents + "원본"
w2 = QWidget()
w2.setStyleSheet(f"background: {theme.BG};")
l2 = QHBoxLayout(w2)
l2.setContentsMargins(12, 12, 12, 12)

back2 = icon_button("arrow-left", "자료 목록")
l2.addWidget(back2)
l2.addStretch(1)

open2 = QPushButton(" 원본")
open2.setObjectName("Quiet")
open2.setCursor(Qt.CursorShape.PointingHandCursor)
open2.setIcon(icons.icon("documents", theme.TEXT_MUTED, 16))
open2.setIconSize(QSize(16, 16))
l2.addWidget(open2)

w2.resize(320, 60)
p2 = QPixmap(320, 60)
w2.render(p2)
p2.save("test_header_variant2.png")

print("Both header variants rendered!")
