"""서식 보관함: 고르기 → 문서 구성/모양 편집 → 예시 확인 → 저장."""

from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QDialog,
    QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter, QStackedWidget,
    QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from ....studio import format_import, layout, markers, templates
from ... import theme
from ...widgets import TabBar, muted_label, view_title
from .output_html import output_html

STYLE_PARTS = (("title", "문서 제목"), ("heading", "항목 제목"),
               ("body", "일반 문단"), ("levels.0", "목록 1단계"),
               ("levels.1", "목록 2단계"), ("levels.2", "목록 3단계"),
               ("note", "주석"))
NO_GLYPH = {"title", "body"}


class TemplateDialog(QDialog):
    def __init__(self, db, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self._current: templates.Template | None = None
        self._loading = True
        self._dirty = False
        self._style_slot = "title"
        self._draft_format = copy.deepcopy(templates.DEFAULT_FORMAT)
        self.setObjectName("TemplateDialog")
        self.setWindowTitle("서식 관리")
        self.setMinimumSize(1080, 660)
        self.resize(1280, 800)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 16)
        outer.setSpacing(16)
        outer.addWidget(view_title("서식 관리"))
        outer.addWidget(muted_label("문서의 항목과 모양을 정해 두세요. 저장한 서식은 업무노트에서 문서를 만들 때 사용할 수 있습니다."))

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(12)
        outer.addWidget(self.splitter, 1)
        self._build_shelf()
        self._build_editor()
        self._build_preview()
        self.splitter.setSizes([224, 480, 500])

        footer = QHBoxLayout()
        self.delete_button = self._button("서식 삭제", self._delete, "Quiet")
        footer.addWidget(self.delete_button)
        self.state = muted_label("", small=True)
        self.state.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.state.setAccessibleName("서식 저장 상태")
        footer.addWidget(self.state, 1)
        self.close_button = self._button("닫기", self.reject, "Quiet")
        footer.addWidget(self.close_button)
        self.save_button = self._button("저장", self._save, "Primary")
        footer.addWidget(self.save_button)
        outer.addLayout(footer)
        # 입력 중 Enter가 저장/삭제 버튼을 우연히 실행하지 않게 한다.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self.save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        self.save_shortcut.activated.connect(self._save)
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(80)
        self.preview_timer.timeout.connect(self._preview)
        self._loading = False
        self._reload()

    @staticmethod
    def _button(text, action, object_name=""):
        button = QPushButton(text)
        if object_name:
            button.setObjectName(object_name)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(action)
        return button

    def _build_shelf(self):
        pane = QFrame()
        pane.setObjectName("TemplateLibrary")
        pane.setMinimumWidth(210)
        column = QVBoxLayout(pane)
        column.setContentsMargins(12, 16, 12, 12)
        column.setSpacing(10)
        title = QLabel("서식 보관함")
        title.setObjectName("SectionTitle")
        column.addWidget(title)
        self.search = QLineEdit()
        self.search.setPlaceholderText("서식 이름 검색")
        self.search.setAccessibleName("서식 검색")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        column.addWidget(self.search)
        self.filter = QComboBox()
        self.filter.addItems(["전체 서식", "기본 서식", "내 서식"])
        self.filter.setAccessibleName("서식 종류 필터")
        self.filter.currentIndexChanged.connect(self._filter)
        column.addWidget(self.filter)
        self.shelf = QListWidget()
        self.shelf.setObjectName("TemplateShelf")
        self.shelf.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.shelf.setWordWrap(True)
        self.shelf.setAccessibleName("서식 목록")
        self.shelf.currentItemChanged.connect(self._show)
        column.addWidget(self.shelf, 1)
        self.empty = muted_label("조건에 맞는 서식이 없습니다.", small=True)
        self.empty.hide()
        column.addWidget(self.empty)
        self.new_button = self._button("＋ 새 서식 만들기", self._new)
        column.addWidget(self.new_button)
        self.import_button = self._button("파일에서 가져오기", self._import)
        self.import_button.setToolTip("한글(.hwpx)·온나라(.odt) 문서에서 본문 서식을 가져옵니다.")
        column.addWidget(self.import_button)
        column.addWidget(muted_label("지원 파일: .hwpx · .odt", small=True))
        self.splitter.addWidget(pane)

    def _build_editor(self):
        pane = QWidget()
        pane.setMinimumWidth(390)
        column = QVBoxLayout(pane)
        column.setContentsMargins(8, 0, 8, 0)
        column.setSpacing(12)
        self.editor_title = QLabel()
        self.editor_title.setObjectName("SectionTitle")
        self.editor_title.setWordWrap(True)
        column.addWidget(self.editor_title)
        self.editor_hint = muted_label("", small=True)
        column.addWidget(self.editor_hint)
        self.tabs = TabBar((("structure", "문서 구성"), ("appearance", "글자·문단")))
        self.tabs.switched.connect(self._switch_tab)
        self.tabs.select("structure")
        column.addWidget(self.tabs)
        self.editor_stack = QStackedWidget()
        column.addWidget(self.editor_stack, 1)
        structure = QVBoxLayout()
        structure.setSpacing(8)
        fields = QGridLayout()
        fields.addWidget(muted_label("서식 이름", small=True), 0, 0)
        fields.addWidget(muted_label("문서 종류", small=True), 0, 1)
        self.name = QLineEdit()
        self.name.setAccessibleName("서식 이름")
        self.name.setPlaceholderText("예: 분기 실적 보고")
        self.name.textChanged.connect(self._changed)
        self.kind = QLineEdit()
        self.kind.setAccessibleName("문서 종류")
        self.kind.setPlaceholderText("예: 업무보고")
        self.kind.textChanged.connect(self._changed)
        fields.addWidget(self.name, 1, 0)
        fields.addWidget(self.kind, 1, 1)
        fields.setColumnStretch(0, 3)
        fields.setColumnStretch(1, 2)
        structure.addLayout(fields)
        head = QHBoxLayout()
        self.section_count = QLabel("문서 항목")
        self.section_count.setObjectName("SectionTitle")
        head.addWidget(self.section_count)
        head.addStretch(1)
        self.add_section_button = self._button("＋ 항목 추가", self._add_section, "Quiet")
        head.addWidget(self.add_section_button)
        structure.addLayout(head)
        self.sections = QTableWidget(0, 2)
        self.sections.setObjectName("TemplateSections")
        self.sections.setHorizontalHeaderLabels(["항목 제목", "이 항목에 담을 내용"])
        self.sections.setAccessibleName("문서 항목과 작성 내용")
        self.sections.setToolTip("위에서부터 문서에 들어갑니다. 제목과 작성 내용을 각각 편집하세요.")
        self.sections.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sections.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.sections.setWordWrap(True)
        self.sections.setShowGrid(False)
        self.sections.verticalHeader().hide()
        self.sections.verticalHeader().setDefaultSectionSize(68)
        self.sections.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.sections.setMinimumHeight(100)
        self.sections.itemChanged.connect(self._changed)
        self.sections.itemSelectionChanged.connect(self._section_actions)
        structure.addWidget(self.sections, 1)
        actions = QHBoxLayout()
        self.up_button = self._button("↑ 위로", lambda: self._move_section(-1), "Quiet")
        self.down_button = self._button("↓ 아래로", lambda: self._move_section(1), "Quiet")
        self.remove_section_button = self._button("항목 삭제", self._remove_section, "Quiet")
        for button in (self.up_button, self.down_button, self.remove_section_button):
            actions.addWidget(button)
        actions.addStretch(1)
        structure.addLayout(actions)
        self.guidance_button = self._button("작성 지침 보기", self._toggle_guidance, "TemplateDisclosure")
        structure.addWidget(self.guidance_button)
        self.guidance = QPlainTextEdit()
        self.guidance.setAccessibleName("문서 전체 작성 지침")
        self.guidance.setPlaceholderText("예: 사실과 수치를 중심으로 짧게 쓰고, 확인되지 않은 내용은 비워 둡니다.")
        self.guidance.setFixedHeight(80)
        self.guidance.hide()
        self.guidance.textChanged.connect(self._changed)
        structure.addWidget(self.guidance)
        self._editor_page(structure)

        appearance = QVBoxLayout()
        appearance.setSpacing(16)
        appearance.addWidget(muted_label("바꿀 부분을 고른 뒤 모양을 조정하세요. 오른쪽 예시에 바로 반영됩니다.", small=True))
        self.style_slot = QComboBox()
        self.style_slot.setAccessibleName("모양을 바꿀 문서 부분")
        self.style_slot.currentIndexChanged.connect(self._select_style)
        appearance.addWidget(self.style_slot)
        form = QFormLayout()
        form.setSpacing(14)
        self.font = QComboBox()
        self.font.setEditable(True)
        self.font.addItems(["맑은 고딕", "휴먼명조", "함초롬바탕", "바탕", "돋움"])
        self.font.currentTextChanged.connect(self._style_changed)
        self.size = QDoubleSpinBox()
        self.size.setRange(6, 72)
        self.size.setDecimals(1)
        self.size.setSuffix(" pt")
        self.size.valueChanged.connect(self._style_changed)
        self.bold = QCheckBox("진하게")
        self.bold.toggled.connect(self._style_changed)
        self.line = QSpinBox()
        self.line.setRange(80, 400)
        self.line.setSuffix(" %")
        self.line.valueChanged.connect(self._style_changed)
        self.glyph = QLineEdit()
        self.glyph.setPlaceholderText("예: ○, -, 1.")
        self.glyph.textChanged.connect(self._style_changed)
        self.lead = QSpinBox()
        self.lead.setRange(0, 40)
        self.lead.setSuffix(" 칸")
        self.lead.valueChanged.connect(self._style_changed)
        for control in (self.size, self.line, self.lead):
            control.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
            control.setToolTip("직접 입력하거나 키보드의 ↑·↓로 조정합니다.")
        for label, widget in (("글꼴", self.font), ("글자 크기", self.size),
                              ("강조", self.bold), ("줄 간격", self.line),
                              ("앞 기호", self.glyph), ("들여쓰기", self.lead)):
            widget.setAccessibleName(label)
            form.addRow(label, widget)
        appearance.addLayout(form)
        self.glyph_hint = muted_label("", small=True)
        appearance.addWidget(self.glyph_hint)
        self.origin = muted_label("", small=True)
        appearance.addWidget(self.origin)
        self.notes_button = self._button("가져온 서식의 확인 사항 보기", self._toggle_notes, "Quiet")
        appearance.addWidget(self.notes_button)
        self.notes = muted_label("", small=True)
        self.notes.hide()
        appearance.addWidget(self.notes)
        appearance.addStretch(1)
        self._editor_page(appearance)
        self.splitter.addWidget(pane)

    def _editor_page(self, column):
        content = QWidget()
        column.setContentsMargins(0, 4, 4, 8)
        content.setLayout(column)
        scroll = QScrollArea()
        scroll.setObjectName("TemplateEditorScroll")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        self.editor_stack.addWidget(scroll)

    def _build_preview(self):
        pane = QFrame()
        pane.setObjectName("TemplatePreviewPane")
        pane.setMinimumWidth(320)
        column = QVBoxLayout(pane)
        column.setContentsMargins(16, 16, 16, 12)
        column.setSpacing(10)
        head = QHBoxLayout()
        title = QLabel("미리보기")
        title.setObjectName("SectionTitle")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(muted_label("실시간 반영", small=True))
        column.addLayout(head)
        column.addWidget(muted_label("서식 확인용 예시입니다. 실제 문서 내용은 포함하지 않습니다.", small=True))
        self.preview = QTextBrowser()
        self.preview.setObjectName("TemplatePreview")
        self.preview.setAccessibleName("서식 예시 미리보기")
        self.preview.setOpenExternalLinks(False)
        column.addWidget(self.preview, 1)
        self.preview_summary = muted_label("", small=True)
        column.addWidget(self.preview_summary)
        self.splitter.addWidget(pane)

    def _switch_tab(self, key):
        self.tabs.select(key)
        self.editor_stack.setCurrentIndex(0 if key == "structure" else 1)

    def _reload(self, select=None):
        self.shelf.blockSignals(True)
        self.shelf.clear()
        for template in templates.all_templates(self.db):
            source = "기본 서식" if template.builtin else "파일에서 가져옴" if template.format.get("source") else "내 서식"
            item = QListWidgetItem(f"{template.title}\n{source} · 항목 {len(template.sections)}개")
            item.setSizeHint(QSize(180, 64))
            item.setToolTip(template.description or template.title)
            item.setData(Qt.ItemDataRole.UserRole, template.id)
            item.setData(Qt.ItemDataRole.UserRole + 1, template.builtin)
            item.setData(Qt.ItemDataRole.UserRole + 2, template.title + " " + template.doc_type)
            self.shelf.addItem(item)
            if template.id == select:
                self.shelf.setCurrentItem(item)
        if self.shelf.currentItem() is None and self.shelf.count():
            self.shelf.setCurrentRow(0)
        self.shelf.blockSignals(False)
        item = self.shelf.currentItem()
        if item:
            self._load(templates.get(self.db, item.data(Qt.ItemDataRole.UserRole)))
        self._filter()

    def _filter(self, *_):
        query = self.search.text().strip().casefold()
        category = self.filter.currentIndex()
        visible = 0
        for row in range(self.shelf.count()):
            item = self.shelf.item(row)
            builtin = item.data(Qt.ItemDataRole.UserRole + 1)
            match = query in item.data(Qt.ItemDataRole.UserRole + 2).casefold()
            match = match and (category == 0 or (category == 1 and builtin) or (category == 2 and not builtin))
            item.setHidden(not match)
            visible += bool(match)
        self.empty.setVisible(visible == 0)

    def _show(self, item, previous=None):
        if item is None:
            return
        template_id = item.data(Qt.ItemDataRole.UserRole)
        if not self._may_leave():
            self.shelf.blockSignals(True)
            self.shelf.setCurrentItem(previous)
            self.shelf.blockSignals(False)
            return
        # 저장 중 목록이 다시 만들어질 수 있으므로 item을 다시 읽지 않는다.
        self._load(templates.get(self.db, template_id))
        self.shelf.blockSignals(True)
        for row in range(self.shelf.count()):
            candidate = self.shelf.item(row)
            if candidate.data(Qt.ItemDataRole.UserRole) == template_id:
                self.shelf.setCurrentItem(candidate)
                break
        self.shelf.blockSignals(False)

    def _load(self, template, samples=None):
        self._loading = True
        self.preview_timer.stop()
        self._current = copy.deepcopy(template)
        self._draft_format = copy.deepcopy(template.format)
        self.name.setText(template.title)
        self.kind.setText(template.doc_type)
        self.sections.setRowCount(len(template.sections))
        for row, section in enumerate(template.sections):
            self.sections.setItem(row, 0, QTableWidgetItem(section.title))
            self.sections.setItem(row, 1, QTableWidgetItem(section.instruction))
        self.sections.clearSelection()
        self.guidance.setPlainText(template.guidance)
        self.guidance.hide()
        self.guidance_button.setText("작성 지침 보기 · 설정됨" if template.guidance else "작성 지침 추가")
        self.guidance_button.setToolTip(template.guidance or "문서 전체에 적용할 작성 방법을 입력합니다.")
        self.delete_button.setEnabled(template.id is not None and not template.builtin)
        self.save_button.setText("사본으로 저장" if template.builtin else "저장")
        self.editor_title.setText(template.title)
        self.editor_hint.setText("기본 서식 · 변경한 내용은 내 서식으로 저장됩니다." if template.builtin
                                 else "새 서식 · 저장하면 보관함에 추가됩니다." if template.id is None
                                 else "내 서식 · 수정한 내용은 저장 후 사용할 수 있습니다.")
        self.state.setText("기본 서식을 수정하면 사본으로 저장합니다." if template.builtin else "")
        self.style_slot.clear()
        for slot, title in STYLE_PARTS:
            if slot.startswith("levels.") and int(slot[-1]) >= len(template.format.get("levels") or []):
                continue
            self.style_slot.addItem(title, slot)
        self.style_slot.setCurrentIndex(0)
        self._style_slot = "title"
        self._samples = samples or {}
        source = template.format.get("source")
        self.origin.setText(f"가져온 파일: {source['file']}" if source else "")
        self.origin.setVisible(bool(source))
        notes = template.format.get("notes") or []
        self.notes.setText("\n".join(f"• {note}" for note in notes))
        self.notes.hide()
        self.notes_button.setVisible(bool(notes))
        self.notes_button.setText("가져온 서식의 확인 사항 보기")
        self._load_style()
        self._loading = False
        self._dirty = False
        self._section_actions()
        self._preview()

    @staticmethod
    def _part(fmt, slot):
        key, _, index = slot.partition(".")
        return fmt[key][int(index)] if index else fmt.setdefault(key, {})

    def _select_style(self, *_):
        if self._loading or self.style_slot.currentIndex() < 0:
            return
        self._style_slot = self.style_slot.currentData()
        self._loading = True
        self._load_style()
        self._loading = False

    def _load_style(self):
        style = layout.styles(self._draft_format)[self._style_slot.replace("levels.", "level")]
        self.font.setCurrentText(str(style.get("font", "맑은 고딕")))
        self.size.setValue(float(style.get("size", 12)))
        self.bold.setChecked(bool(style.get("bold")))
        self.line.setValue(int(style.get("line", 160)))
        self.glyph.setText(str(style.get("glyph", "")))
        self.lead.setValue(int(style.get("lead", 0)))
        enabled = self._style_slot not in NO_GLYPH
        self.glyph.setEnabled(enabled)
        self.lead.setEnabled(enabled)
        sample = self._samples.get(str(style.get("key", "")))
        self.glyph.setToolTip(f"원본 예시(저장하지 않음): {sample}" if sample else "")
        self.glyph_hint.setText("제목과 일반 문단에는 앞 기호를 붙이지 않습니다." if not enabled
                                else "한컴 전용 기호는 미리보기에서 대체 모양으로 표시됩니다." if markers.is_private(self.glyph.text())
                                else "앞 기호를 비우면 기호 없이 표시합니다.")

    def _style_changed(self, *_):
        if self._loading:
            return
        part = self._part(self._draft_format, self._style_slot)
        part.update(font=self.font.currentText().strip() or "맑은 고딕", size=self.size.value(),
                    bold=self.bold.isChecked(), line=self.line.value())
        if self._style_slot not in NO_GLYPH:
            glyph = self.glyph.text().strip()
            if glyph != str(part.get("glyph", "")):
                marker = markers.classify(f"{glyph} 글")
                part.update(glyph=glyph, glyphs=[glyph] if glyph else [], key=marker.key if marker else "")
            part["lead"] = self.lead.value()
        self._changed()

    def _format(self):
        return copy.deepcopy(self._draft_format)

    def _section_values(self):
        values = []
        for row in range(self.sections.rowCount()):
            title = self.sections.item(row, 0)
            instruction = self.sections.item(row, 1)
            values.append(templates.Section(title.text().strip() if title else "",
                                           instruction.text().strip() if instruction else ""))
        return values

    def _titles(self):
        return [section.title for section in self._section_values() if section.title]

    def _section_actions(self):
        row = self.sections.currentRow() if self.sections.selectedItems() else -1
        self.up_button.setEnabled(row > 0)
        self.down_button.setEnabled(0 <= row < self.sections.rowCount() - 1)
        self.remove_section_button.setEnabled(row >= 0)

    def _add_section(self):
        row = self.sections.rowCount()
        self._loading = True
        self.sections.insertRow(row)
        self.sections.setItem(row, 0, QTableWidgetItem("새 항목"))
        self.sections.setItem(row, 1, QTableWidgetItem(""))
        self._loading = False
        self.sections.setCurrentCell(row, 0)
        self._changed()
        self.sections.editItem(self.sections.item(row, 0))

    def _remove_section(self):
        row = self.sections.currentRow()
        if row >= 0:
            self.sections.removeRow(row)
            self._section_actions()
            self._changed()

    def _move_section(self, delta):
        row = self.sections.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.sections.rowCount():
            return
        self._loading = True
        for column in range(2):
            first = self.sections.takeItem(row, column)
            second = self.sections.takeItem(target, column)
            self.sections.setItem(row, column, second)
            self.sections.setItem(target, column, first)
        self._loading = False
        self.sections.setCurrentCell(target, 0)
        self._section_actions()
        self._changed()

    def _changed(self, *_):
        if self._loading:
            return
        self._dirty = True
        self.state.setText("저장하지 않은 변경사항이 있습니다.")
        self.state.setToolTip("")
        self.editor_title.setText(self.name.text().strip() or "새 서식")
        self.preview_timer.start()

    def _preview(self):
        if self._loading or self._current is None:
            return
        self.section_count.setText(f"문서 항목 · {self.sections.rowCount()}개")
        lines = ["# " + (self.name.text().strip() or "새 서식"), ""]
        for title in self._titles() or ["항목 제목"]:
            lines.extend([f"## {title}", "- 주요 내용을 개조식으로 적습니다",
                          "  - 세부 사항을 적습니다", "    - 세부 내용", ""])
        lines.append("> 주석 및 근거")
        markdown = "\n".join(lines)
        scrollbar = self.preview.verticalScrollBar()
        position = scrollbar.value()
        self.preview.setHtml(output_html(markdown, self._format()))
        scrollbar.setValue(position)
        self.preview_summary.setText(f"항목 {len(self._titles())}개 · 제목·본문·목록의 모양을 확인하세요")

    def _toggle_notes(self):
        opened = not self.notes.isVisible()
        self.notes.setVisible(opened)
        self.notes_button.setText("가져온 서식의 확인 사항 접기" if opened else "가져온 서식의 확인 사항 보기")

    def _toggle_guidance(self):
        opened = self.guidance.isHidden()
        self.guidance.setVisible(opened)
        self.guidance_button.setText("작성 지침 접기" if opened else
                                     "작성 지침 보기 · 설정됨" if self.guidance.toPlainText().strip() else "작성 지침 추가")
        if opened:
            self.guidance.setFocus()

    def _new(self):
        if not self._may_leave():
            return
        self.shelf.blockSignals(True)
        self.shelf.setCurrentRow(-1)
        self.shelf.blockSignals(False)
        self._load(templates.Template("새 서식", "기타", sections=[templates.Section("1. 개요")]))
        self._switch_tab("structure")
        self._changed()
        self.name.setFocus()
        self.name.selectAll()

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "서식 파일 고르기", "", format_import.FILE_FILTER)
        if path:
            self.import_file(path)

    def import_file(self, path):
        if not self._may_leave():
            return
        self.state.setText("파일의 본문 서식을 읽는 중입니다…")
        QApplication.processEvents()
        try:
            analysis = format_import.analyze(path)
        except format_import.FormatError as exc:
            self.state.setText(str(exc))
            self.state.setToolTip(str(exc))
            return
        base = next(t for t in templates.BUILTIN if t.key == "work-report")
        report = analysis.doc_kind == "report"
        sections = ([templates.Section(name) for name in analysis.headings] if analysis.headings
                    else copy.deepcopy(base.sections) if report else [templates.Section("1. 개요")])
        template = templates.Template(Path(path).stem, "업무보고" if report else "기타",
            f"{analysis.file_name}에서 가져온 서식", sections,
            base.guidance if report else "자료에 적힌 사실만 쓴다.", format_import.studio_format(analysis))
        self.shelf.blockSignals(True)
        self.shelf.setCurrentRow(-1)
        self.shelf.blockSignals(False)
        self._load(template, {level.key: level.sample for level in analysis.levels})
        self._switch_tab("structure")
        self._dirty = True
        self.state.setText("가져온 항목과 모양을 확인한 뒤 [저장]을 누르세요.")

    def _save(self):
        focus = self.focusWidget()
        if focus and self.sections.isAncestorOf(focus):
            # Ctrl+S도 현재 셀의 입력을 먼저 확정한 뒤 저장한다.
            focus.clearFocus()
        template = copy.deepcopy(self._current) if self._current else templates.Template("", "기타")
        template.format = self._format()
        template.title, template.doc_type = self.name.text(), self.kind.text()
        template.guidance = self.guidance.toPlainText()
        template.sections = self._section_values()
        try:
            key = templates.save(self.db, template)
        except ValueError as exc:
            self.state.setText(str(exc))
            self.state.setToolTip(str(exc))
            if not template.title.strip():
                self._switch_tab("structure")
                self.name.setFocus()
            elif not self._titles():
                self._switch_tab("structure")
                self.sections.setFocus()
            return False
        self._dirty = False
        self.search.clear()
        self.filter.setCurrentIndex(0)
        self._reload(key)
        self.state.setText("내 서식으로 사본을 저장했습니다." if template.builtin else "저장했습니다.")
        self.state.setToolTip("")
        return True

    def _may_leave(self):
        if not self._dirty:
            return True
        answer = QMessageBox.question(self, "변경사항 저장", "이 서식에 저장하지 않은 변경사항이 있습니다. 저장할까요?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save)
        if answer == QMessageBox.StandardButton.Save:
            return self._save()
        return answer == QMessageBox.StandardButton.Discard

    def reject(self):
        if self._may_leave():
            self.preview_timer.stop()
            super().reject()

    def _delete(self):
        if self._current is None or self._current.id is None or self._current.builtin:
            return
        answer = QMessageBox.question(self, "서식 삭제", f"‘{self._current.title}’ 서식을 삭제할까요?\n이미 만든 문서는 유지됩니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            templates.delete(self.db, self._current.id)
        except ValueError as exc:
            self.state.setText(str(exc))
            self.state.setToolTip(str(exc))
            return
        self._dirty = False
        self._reload()
        self.state.setText("서식을 삭제했습니다.")
