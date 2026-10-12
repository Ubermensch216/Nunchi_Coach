"""왼쪽 판 — 이 노트북의 자료를 더하고, 근거로 쓸 것을 고르고, 원문을 읽는다.

목록과 원문은 같은 자리를 번갈아 쓴다. 대화에서 인용 번호를 누르면 이 판이
그 문서의 원문으로 바뀌고 인용한 대목을 칠해 보여 준다 — 답 옆에 원문 칸을
따로 세우면 세 판 가운데 대화가 가장 좁아진다.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from ....core import notebook, notebook_ingest, notebook_tasks, onnara_import
from ....db import representative_predicate
from ....jobs import NotebookIngestRunner
from ....search.rag import Citation
from ... import icons, theme
from ...widgets import (
    Card,
    ElidedLabel,
    UnknownBlock,
    clear_layout,
    muted_label,
    open_original,
)
from ..ask import _cited_ordinals, _fill_passages, _meta_strip
from .panel import Panel, icon_button, set_icon
from .task_picker import TaskSourceDialog

ORIGIN_LABELS = {"project": "전임자 자료", "upload": "올린 파일", "onnara": "온나라 수집본",
                 notebook_tasks.ORIGIN: "눈치코치가 정리한 업무"}
# 넣은 뒤 업무의 내용이 달라진 정리본. 원문이 바뀐 것과는 다른 일이라 말을 따로 둔다.
STALE_BRIEF = "업무 내용 변경"
BRIEF_NOTE = ("눈치코치가 전임자 자료를 분석해 정리한 글입니다. 원문이 아닙니다 — "
              "항목마다 적힌 근거 문서를 함께 확인하세요.")


def source_state(row) -> str:
    """문제가 있는 자료에만 붙는 한마디. 멀쩡하면 빈 문자열이다."""
    if not row["allowed"] or row["missing_since"]:
        return "이용 불가"
    if row["parse_status"] == "pending":
        return "처리 대기"
    if row["parse_status"] not in ("ok", "partial"):
        return "읽지 못함"
    if row["hash"] != row["version_hash"]:
        return "원문 변경"
    if not notebook.file_matches(row["path"], row["version_hash"]):
        return "원문 변경 · 재분석 필요"
    if row["parse_status"] == "partial":
        return "일부만 읽음"
    return ""


def _fit_narrow(card: QWidget) -> None:
    """원문 대목이 좁은 판의 폭을 밀어내지 않게 한다.

    파일 이름처럼 띄어쓰기 없는 긴 낱말은 QLabel이 접지 못해 그 길이가 곧
    최소 폭이 된다. 질문 화면의 넓은 원문 칸에서는 드러나지 않던 문제다.
    밑줄·빗금 뒤에 폭 없는 공백을 넣어 접을 자리를 만들어 준다.
    """
    for label in card.findChildren(QLabel):
        if label.objectName() in ("ParaText", "ParaCited"):
            label.setText(label.text().replace("_", "_\u200b").replace("/", "/\u200b"))
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            label.setMinimumWidth(0)


SOURCE_ROW_H = 36


class SourceItemDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, option, painter, option.widget)


class SourceItem(QListWidgetItem):
    """자료 목록 아이템.

    QListView는 아이템에 CheckStateRole 또는 ItemIsUserCheckable 플래그가 있으면
    왼쪽에 체크 표시 영역(약 24~28px)을 강제로 예약하여 setItemWidget의 위젯을 오른쪽으로 밀어낸다.
    이로 인해 위젯 안의 QCheckBox 왼쪽에 빈 공간(유령 여백)이 생기므로,
    체크 상태는 아이템 내부 변수로 관리하고 QListWidgetItem의 CheckStateRole에는 기록하지 않는다.
    """

    def __init__(self, text: str = ""):
        super().__init__(text)
        self._check_state = Qt.CheckState.Unchecked

    def checkState(self) -> Qt.CheckState:
        return self._check_state

    def setCheckState(self, state: Qt.CheckState) -> None:
        state = Qt.CheckState(state)
        if self._check_state != state:
            self._check_state = state
            lw = self.listWidget()
            if lw is not None:
                lw.itemChanged.emit(self)


class SourceRow(QWidget):
    """자료 목록의 각 줄 — 체크 상자, 파일 이름, 원문 보기·연결 해제 아이콘 단추."""

    def __init__(self, item: QListWidgetItem, doc_id: int, filename: str, state: str,
                 editable: bool, on_read, on_detach, parent: QWidget | None = None):
        super().__init__(parent)
        self.item = item
        self.doc_id = doc_id
        self._on_read = on_read
        self.setObjectName("SourceRow")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 4, 2)
        layout.setSpacing(theme.SP_XS)

        self.cb = QCheckBox()
        self.cb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cb.setEnabled(editable)
        self.cb.setChecked(item.checkState() == Qt.CheckState.Checked)
        self.cb.clicked.connect(lambda c: item.setCheckState(Qt.CheckState.Checked if c else Qt.CheckState.Unchecked))
        layout.addWidget(self.cb)

        self.name_label = ElidedLabel(filename)
        self.name_label.setObjectName("SourceRowText")
        self.name_label.setToolTip(item.toolTip())
        layout.addWidget(self.name_label, 1)

        if state:
            state_label = QLabel(f"· {state}")
            state_label.setObjectName("SourceRowState")
            state_label.setToolTip(item.toolTip())
            layout.addWidget(state_label)

        # 원문 보기 아이콘 단추 — 아이콘으로만 표시하고 설명 텍스트는 넣지 않는다
        self.read_btn = icon_button("eye", "원문 보기")
        self.read_btn.setFixedSize(26, 26)
        self.read_btn.setIconSize(QSize(16, 16))
        self.read_btn.clicked.connect(lambda: on_read(doc_id))
        layout.addWidget(self.read_btn)

        # 연결 해제 아이콘 단추 — 아이콘으로만 표시하고 설명 텍스트는 넣지 않는다
        self.detach_btn = None
        if editable:
            self.detach_btn = icon_button("unlink", "연결 해제")
            self.detach_btn.setFixedSize(26, 26)
            self.detach_btn.setIconSize(QSize(16, 16))
            self.detach_btn.clicked.connect(lambda: on_detach(doc_id))
            layout.addWidget(self.detach_btn)

    def repaint_icons(self) -> None:
        set_icon(self.read_btn, "eye")
        if self.detach_btn is not None:
            set_icon(self.detach_btn, "unlink")

    def mousePressEvent(self, event) -> None:  # noqa: N802 — Qt 규약
        lw = self.item.listWidget()
        if lw is not None:
            lw.setCurrentItem(self.item)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 — Qt 규약
        self._on_read(self.doc_id)
        super().mouseDoubleClickEvent(event)


class SourcesPanel(Panel):
    selection_changed = Signal()   # 근거로 쓸 자료가 바뀌었다 — 지난 답을 다시 판정해야 한다

    def __init__(self, db, parent: QWidget | None = None):
        super().__init__("출처", parent)
        self.db = db
        self.notebook_id: int | None = None
        self.editable = False
        self.reader_doc_id: int | None = None
        self._report = ""            # 마지막으로 올린 묶음의 결과 한마디
        self._signature = None
        self._reprocessing = False
        self._expanded: set[int] = set()
        self._citation: Citation | None = None
        self._stale_briefs: dict[int, int] = {}   # 문서 → 업무. 노트북을 열 때와 자료가 바뀔 때만 다시 잰다
        self._ingest = NotebookIngestRunner(db.path, self)
        self._ingest.progress.connect(self._on_progress)
        self._ingest.file_done.connect(self._on_file_done)
        self._ingest.done.connect(self._on_ingest_done)
        # 파일을 끌어다 놓아 올릴 수 있다.
        self.setAcceptDrops(True)

        self.stack = QStackedWidget()
        self.body.addWidget(self.stack, 1)
        self.stack.addWidget(self._build_list())
        self.stack.addWidget(self._build_reader())

    # ── 목록 ────────────────────────────────────────────────────────
    def _build_list(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(theme.SP_SM)

        self.add_button = QPushButton("+ 자료 추가")
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_menu = QMenu(self.add_button)
        self.add_menu.addAction("파일 올리기…", lambda: self._safe(self._pick_files))
        self.add_menu.addAction("눈치코치가 파악한 업무에서 고르기…", lambda: self._safe(self._pick_tasks))
        self.add_menu.addAction("분석한 전임자 자료에서 고르기…", lambda: self._safe(self._link_documents))
        self.add_menu.addAction("온나라 수집본 가져오기…", lambda: self._safe(self._pick_collection))
        self.add_button.setMenu(self.add_menu)
        column.addWidget(self.add_button)
        self.resume_button = QPushButton("자료 처리 이어서")
        self.resume_button.setObjectName("Quiet")
        self.resume_button.setToolTip("중단된 처리나 자료 업데이트를 실행합니다. 기존 결과는 열 때 그대로 사용합니다.")
        self.resume_button.clicked.connect(lambda: self._safe(self._resume_uploads))
        self.resume_button.hide()
        column.addWidget(self.resume_button)

        # 올리는 동안의 진행. 남은 시간을 아는 척하지 않는다 — 그림 판독은 쪽마다 다르다.
        self.progress_box = QWidget()
        progress = QVBoxLayout(self.progress_box)
        progress.setContentsMargins(0, 0, 0, 0)
        progress.setSpacing(theme.SP_XS)
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("Thinking")
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(3)
        progress.addWidget(self.progress_bar)
        line = QHBoxLayout()
        line.setSpacing(theme.SP_SM)
        self.progress_label = muted_label("", small=True)
        line.addWidget(self.progress_label, 1)
        self.cancel_ingest = QPushButton("처리 취소")
        self.cancel_ingest.setObjectName("Quiet")
        self.cancel_ingest.clicked.connect(self._ingest.cancel)
        line.addWidget(self.cancel_ingest)
        progress.addLayout(line)
        self.progress_box.hide()
        column.addWidget(self.progress_box)

        row = QHBoxLayout()
        row.setSpacing(theme.SP_SM)
        self.count = muted_label("", small=True, wrap=False)
        row.addWidget(self.count)
        row.addStretch(1)
        self.all_box = QCheckBox("모두 선택")
        self.all_box.setTristate(False)
        self.all_box.clicked.connect(self._select_all)
        row.addWidget(self.all_box)
        column.addLayout(row)

        self.source_list = QListWidget()
        self.source_list.setObjectName("SourceList")
        self.source_list.setItemDelegate(SourceItemDelegate(self.source_list))
        # 긴 파일 이름은 가운데를 접는다. 가로 스크롤이 생기면 상태 표시가 밀려 안 보인다.
        self.source_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.source_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.source_list.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.source_list.itemChanged.connect(self._select_source)
        self.source_list.itemActivated.connect(lambda item: self.show_document(item.data(Qt.ItemDataRole.UserRole)))
        self.source_list.currentItemChanged.connect(lambda *_: self._sync_actions())
        column.addWidget(self.source_list, 1)

        self.hint = muted_label("", small=True)
        column.addWidget(self.hint)

        actions = QHBoxLayout()
        actions.setSpacing(theme.SP_SM)
        self.read_button = QPushButton("원문 보기")
        self.read_button.setObjectName("Quiet")
        self.read_button.clicked.connect(self._read_current)
        self.read_button.hide()
        self.accept_button = QPushButton("새 판본 사용")
        self.accept_button.setObjectName("Quiet")
        self.accept_button.setToolTip("원문이 바뀐 자료를 다시 분석한 판본으로 근거를 바꿉니다")
        self.accept_button.clicked.connect(lambda: self._safe(self._accept))
        self.accept_button.hide()
        actions.addWidget(self.accept_button)
        self.detach_button = QPushButton("연결 해제")
        self.detach_button.setObjectName("Quiet")
        self.detach_button.setToolTip("이 노트북에서만 뺍니다. 원문 파일과 분석 결과는 그대로입니다")
        self.detach_button.clicked.connect(lambda: self._safe(self._detach))
        self.detach_button.hide()
        actions.addStretch(1)
        column.addLayout(actions)
        return page

    def set_notebook(self, notebook_id: int | None, editable: bool) -> None:
        if notebook_id != self.notebook_id:
            self._ingest.cancel()
            self._report = ""
        self.notebook_id = notebook_id
        self.editable = editable
        self._signature = None
        self._check_briefs()
        self.show_list()
        self.refresh()

    def rows(self):
        return notebook.sources(self.db, self.notebook_id) if self.notebook_id is not None else []

    def refresh(self, force: bool = False) -> None:
        """자료 목록을 지금 상태로 맞춘다. 달라진 것이 없으면 다시 그리지 않는다.

        상태 판정(source_state)은 원문 파일의 해시를 다시 읽는다. 분석이 도는
        동안 셸이 1.5초마다 refresh를 부르므로 매번 읽으면 화면이 굳는다.
        """
        rows = self.rows()
        stale = notebook_ingest.outdated_uploads(self.db, self.notebook_id) if self.notebook_id is not None else []
        self.resume_button.setVisible(self.editable and bool(stale))
        self.resume_button.setEnabled(self.editable and bool(stale) and not self._ingest.running)
        pending = any(r["doc_id"] in stale and r["parse_status"] == "pending" for r in rows)
        self.resume_button.setText(("자료 처리 이어서" if pending else "자료 업데이트") + f" ({len(stale)}건)")
        signature = (self.notebook_id, self.editable, tuple(
            (r["doc_id"], r["hash"], r["version_hash"], r["selected"], r["allowed"],
             r["missing_since"], r["parse_status"]) for r in rows), tuple(sorted(self._stale_briefs)), tuple(stale), self._ingest.running)
        if not force and signature == self._signature:
            return
        self._signature = signature
        current = self._current_doc()
        self.source_list.blockSignals(True)
        self.source_list.clear()
        for row in rows:
            state = source_state(row)
            if not state and row["doc_id"] in self._stale_briefs:
                state = STALE_BRIEF
            item = SourceItem(row["filename"] + (f"  ·  {state}" if state else ""))
            item.setData(Qt.ItemDataRole.UserRole, row["doc_id"])
            item.setData(Qt.ItemDataRole.UserRole + 1, state)
            reason = row["parse_error"] or row["parse_note"] or ""
            item.setToolTip("\n".join(part for part in (
                row["filename"], ORIGIN_LABELS.get(row["origin"], row["origin"]), state, reason) if part))
            # 보관한 노트북에서도 원문은 읽을 수 있어야 한다. 선택만 잠근다.
            # QListView가 체크 표시 영역을 예약해 위젯을 밀어내지 않도록 ItemIsUserCheckable 플래그는 주지 않는다.
            item.setCheckState(Qt.CheckState.Checked if row["selected"] else Qt.CheckState.Unchecked)
            item.setSizeHint(QSize(0, SOURCE_ROW_H))
            self.source_list.addItem(item)
            row_widget = SourceRow(
                item, row["doc_id"], row["filename"], state, self.editable,
                on_read=self.show_document,
                on_detach=lambda did: self._safe(lambda: self._detach_doc(did)),
            )
            self.source_list.setItemWidget(item, row_widget)
            if row["doc_id"] == current:
                self.source_list.setCurrentItem(item)
        self.source_list.blockSignals(False)
        self.add_button.setEnabled(self.editable and not self._ingest.running)
        self._sync_counts(rows)
        self._sync_actions()

    def repaint_icons(self) -> None:
        super().repaint_icons()
        if hasattr(self, "back_button"):
            set_icon(self.back_button, "arrow-left")
        if hasattr(self, "open_button"):
            self.open_button.setIcon(icons.icon("external-link", theme.TEXT_MUTED, 16))
        for i in range(self.source_list.count()):
            widget = self.source_list.itemWidget(self.source_list.item(i))
            if isinstance(widget, SourceRow):
                widget.repaint_icons()

    def _sync_counts(self, rows) -> None:
        chosen = sum(1 for r in rows if r["selected"])
        self.count.setText(f"자료 {len(rows)}건 · 선택 {chosen}건" if rows else "")
        self.all_box.setVisible(bool(rows))
        self.all_box.setEnabled(self.editable)
        self.all_box.blockSignals(True)
        self.all_box.setChecked(bool(rows) and chosen == len(rows))
        self.all_box.blockSignals(False)
        if not rows and not self._report:
            self.hint.setText("‘+ 자료 추가’에서 눈치코치가 파악한 업무를 고르거나, 파일을 올리거나 끌어다 "
                              "놓으세요. 추가한 자료만 근거로 묻고 정리합니다."
                              if self.editable else "")
        elif self._report:
            self.hint.setText(self._report)
        self.hint.setVisible(bool(self._report) or not rows)

    def _sync_actions(self) -> None:
        item = self.source_list.currentItem()
        state = item.data(Qt.ItemDataRole.UserRole + 1) if item else ""
        self.read_button.setEnabled(item is not None)
        self.detach_button.setEnabled(item is not None and self.editable)
        brief = state == STALE_BRIEF
        self.accept_button.setText("새 정리본 사용" if brief else "새 판본 사용")
        self.accept_button.setToolTip(
            "업무 화면에서 달라진 내용으로 정리본을 다시 만듭니다. 지난 답은 가려집니다" if brief
            else "원문이 바뀐 자료를 다시 분석한 판본으로 근거를 바꿉니다")
        self.accept_button.setVisible(brief or (bool(state) and state.startswith("원문 변경")))
        self.accept_button.setEnabled(self.editable and not self._ingest.running
                                      and (brief or state == "원문 변경"))

    def _current_doc(self) -> int | None:
        item = self.source_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _safe(self, action) -> None:
        try:
            action()
        except (ValueError, OSError) as exc:
            QMessageBox.information(self, "출처", str(exc))

    def _select_source(self, item: QListWidgetItem) -> None:
        row_widget = self.source_list.itemWidget(item)
        if isinstance(row_widget, SourceRow):
            is_checked = item.checkState() == Qt.CheckState.Checked
            if row_widget.cb.isChecked() != is_checked:
                row_widget.cb.blockSignals(True)
                row_widget.cb.setChecked(is_checked)
                row_widget.cb.blockSignals(False)
        notebook.select(self.db, self.notebook_id, item.data(Qt.ItemDataRole.UserRole),
                        item.checkState() == Qt.CheckState.Checked)
        self._signature = None
        self._sync_counts(self.rows())
        self.selection_changed.emit()

    def _select_all(self, checked: bool) -> None:
        notebook.select_all(self.db, self.notebook_id, checked)
        self.refresh(force=True)
        self.selection_changed.emit()

    def _accept(self) -> None:
        task_id = self._stale_briefs.get(self._current_doc())
        if task_id is not None:
            self.add_tasks([task_id])
            return
        notebook.accept_version(self.db, self.notebook_id, self._current_doc())
        self.refresh(force=True)
        self.selection_changed.emit()

    def _detach_doc(self, doc_id: int) -> None:
        notebook.detach(self.db, self.notebook_id, doc_id)
        if self.reader_doc_id == doc_id and self.stack.currentIndex() == 1:
            self.show_list()
        self._check_briefs()
        self.refresh(force=True)
        self.selection_changed.emit()

    def _detach(self) -> None:
        doc_id = self._current_doc()
        if doc_id is not None:
            self._detach_doc(doc_id)

    def _read_current(self) -> None:
        doc_id = self._current_doc()
        if doc_id is not None:
            self.show_document(doc_id)

    # ── 파일 올리기 ─────────────────────────────────────────────────
    def _pick_files(self) -> None:
        notebook.require(self.db, self.notebook_id)
        paths, _ = QFileDialog.getOpenFileNames(self, "노트북에 올릴 파일", "", notebook_ingest.file_filter())
        self.upload(paths)

    def upload(self, paths: list[str]) -> None:
        """파일을 보관소에 사본으로 두고 읽는다. 원래 자리의 파일은 건드리지 않는다."""
        if not paths or self.notebook_id is None or not self.editable:
            return
        if self._ingest.running:
            QMessageBox.information(self, "파일 올리기", "앞서 올린 파일을 읽고 있습니다. 끝난 뒤에 다시 올려 주세요.")
            return
        self._report = ""
        if self._ingest.start(self.notebook_id, list(paths)):
            self._show_progress(f"{len(paths)}건을 올리는 중")

    def _pick_collection(self) -> None:
        notebook.require(self.db, self.notebook_id)
        folder = QFileDialog.getExistingDirectory(
            self, "온나라 수집본 폴더 (manifest.json) 또는 sAIde 꾸러미 폴더 (package.json)")
        if not folder:
            return
        if onnara_import.is_package(folder):
            summary = onnara_import.describe_package(folder)
            note = ("이 꾸러미의 파일을 프로젝트 안에 수집본으로 복사한 뒤 읽어 노트북 자료로 연결합니다. "
                    "꾸러미 폴더의 파일은 읽기만 하고 바꾸지 않습니다.")
        else:
            summary = onnara_import.describe(onnara_import.read_manifest(folder)[1])
            note = ("이 수집본의 문서를 읽어 노트북 자료로 연결합니다. "
                    "수집 폴더의 파일은 읽기만 하고 바꾸지 않습니다.")
        answer = QMessageBox.question(
            self, "온나라 수집본 가져오기",
            f"{summary}\n\n{note} 이 프로그램이 온나라에 접속하지는 않습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.import_collection(folder)

    def import_collection(self, folder: str) -> None:
        if self.notebook_id is None or not self.editable:
            return
        if self._ingest.running:
            QMessageBox.information(self, "온나라 수집본", "앞서 올린 파일을 읽고 있습니다. 끝난 뒤에 다시 시도하세요.")
            return
        self._report = ""
        if self._ingest.start(self.notebook_id, [], collection=folder):
            self._show_progress("온나라 수집본을 읽는 중")

    # ── 눈치코치가 파악한 업무 ──────────────────────────────────────
    def showEvent(self, event) -> None:  # noqa: N802 — Qt 규약
        super().showEvent(event)
        # 업무 화면에서 내용을 고치고 돌아왔을 수 있다. 화면에 다시 설 때만 잰다 —
        # 분석이 도는 동안 셸이 1.5초마다 부르는 refresh에서는 재지 않는다.
        if self.notebook_id is not None:
            self._check_briefs()
            self.refresh()

    def _check_briefs(self) -> None:
        """넣은 뒤 업무 내용이 달라진 정리본을 찾는다. 보관한 노트북은 고칠 수 없으니 재지 않는다."""
        self._stale_briefs = (notebook_tasks.outdated(self.db, self.notebook_id)
                              if self.notebook_id is not None and self.editable else {})

    def _pick_tasks(self) -> None:
        notebook.require(self.db, self.notebook_id)
        if self._ingest.running:
            QMessageBox.information(self, "업무에서 고르기", "앞서 넣은 자료를 읽고 있습니다. 끝난 뒤에 다시 시도하세요.")
            return
        dialog = TaskSourceDialog(self.db, {row["doc_id"] for row in self.rows()},
                                  notebook_tasks.linked_tasks(self.db, self.notebook_id), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.add_tasks(*dialog.picked())

    def add_tasks(self, task_ids: list[int], doc_ids: list[int] | None = None) -> None:
        """업무 정리본을 만들어 넣고, 그 업무의 문서를 연결한다."""
        if self.notebook_id is None or not self.editable or not (task_ids or doc_ids):
            return
        self._report = ""
        linked = notebook_tasks.add_documents(self.db, self.notebook_id, doc_ids or [])
        if linked:
            self._report = f"업무의 문서 {linked}건을 연결했습니다."
        # 정리본은 글을 만들고 조각내고 임베딩해야 한다. 올린 파일과 같은 길로 뒤에서 한다.
        if task_ids and self._ingest.start(self.notebook_id, [], tasks=list(task_ids)):
            self._show_progress(f"업무 정리본 {len(task_ids)}건을 만드는 중")
        self.refresh(force=True)
        self.selection_changed.emit()

    def _resume_uploads(self) -> None:
        """읽다 멈췄거나 파서가 바뀐 올린 파일을 이어서 읽는다."""
        if self.notebook_id is None or not self.editable:
            return
        stale = notebook_ingest.outdated_uploads(self.db, self.notebook_id)
        if stale and not self._ingest.running and self._ingest.start(self.notebook_id, [], stale):
            self._show_progress(f"기존 자료 {len(stale)}건을 처리하는 중", reprocessing=True)

    def _show_progress(self, text: str, *, reprocessing: bool = False) -> None:
        self._reprocessing = reprocessing
        self.resume_button.setEnabled(False)
        self.progress_label.setText(text)
        self.cancel_ingest.setEnabled(True)
        self.progress_box.show()
        self.add_button.setEnabled(False)

    def _on_progress(self, index: int, total: int, name: str, phase: str) -> None:
        count = f"{index} / {total} · " if total else ""
        self.progress_label.setText(f"{count}{name} — {phase}")

    def _on_file_done(self, _outcome) -> None:
        self.refresh(force=True)
        self.selection_changed.emit()

    def _on_ingest_done(self, results) -> None:
        self.progress_box.hide()
        failed = [r for r in results if not r.usable]
        reused = sum(1 for r in results if r.status == "reused")
        done = len(results) - len(failed)
        parts = [self._report] if self._report else []
        if done:
            parts.append(f"{done}건을 처리했습니다." if self._reprocessing else f"{done}건을 추가했습니다.")
        if reused:
            parts.append(f"{reused}건은 기존 분석 결과를 사용했습니다." if self._reprocessing
                         else f"{reused}건은 이미 읽어 둔 같은 내용의 문서를 연결했습니다.")
        for outcome in failed[:3]:
            parts.append(f"읽지 못함 — {outcome.filename}: {outcome.error}")
        if len(failed) > 3:
            parts.append(f"그 밖에 {len(failed) - 3}건을 읽지 못했습니다. 목록의 상태를 확인하세요.")
        self._report = "\n".join(parts)
        self._check_briefs()
        self.refresh(force=True)
        self.selection_changed.emit()

    def dragEnterEvent(self, event) -> None:  # noqa: N802 — Qt 규약
        if self.editable and event.mimeData().hasUrls() and any(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 — Qt 규약
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        files = [p for p in paths if Path(p).is_file()]
        if files:
            event.acceptProposedAction()
            self.upload(files)

    def _link_documents(self) -> None:
        notebook.require(self.db, self.notebook_id)
        linked = {row["doc_id"] for row in self.rows()}
        dialog = QDialog(self)
        dialog.setWindowTitle("분석한 자료 연결 — 파일은 복사하지 않습니다")
        layout = QVBoxLayout(dialog)
        search = QLineEdit()
        search.setPlaceholderText("파일 이름으로 찾기")
        search.setClearButtonEnabled(True)
        layout.addWidget(search)
        choices = QListWidget()
        for row in self.db.con.execute(
                "SELECT d.id,d.filename FROM documents d WHERE d.hash IS NOT NULL AND d.missing_since IS NULL "
                f"AND d.parse_status IN ('ok','partial') AND {representative_predicate('d')} "
                "ORDER BY d.filename"):
            item = QListWidgetItem(row["filename"])
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            item.setCheckState(Qt.CheckState.Checked if row["id"] in linked else Qt.CheckState.Unchecked)
            choices.addItem(item)
        layout.addWidget(choices, 1)

        def narrow(text: str) -> None:
            needle = text.strip().lower()
            for index in range(choices.count()):
                entry = choices.item(index)
                entry.setHidden(needle not in entry.text().lower())

        search.textChanged.connect(narrow)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(560, 480)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        picked = {choices.item(i).data(Qt.ItemDataRole.UserRole) for i in range(choices.count())
                  if choices.item(i).checkState() == Qt.CheckState.Checked}
        notebook.add_sources(self.db, self.notebook_id, picked - linked)
        for doc_id in linked - picked:
            notebook.detach(self.db, self.notebook_id, doc_id)
        self.refresh(force=True)
        self.selection_changed.emit()

    # ── 원문 ────────────────────────────────────────────────────────
    def _build_reader(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(theme.SP_SM)

        head = QHBoxLayout()
        head.setSpacing(theme.SP_SM)
        self.back_button = icon_button("arrow-left", "자료 목록")
        self.back_button.clicked.connect(self.show_list)
        head.addWidget(self.back_button)
        head.addStretch(1)
        self.open_button = QPushButton("원본")
        self.open_button.setObjectName("Quiet")
        self.open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_button.setToolTip("원본 파일을 엽니다")
        self.open_button.setIcon(icons.icon("external-link", theme.TEXT_MUTED, 16))
        self.open_button.setIconSize(QSize(16, 16))
        self.open_button.clicked.connect(self._open_original)
        head.addWidget(self.open_button)
        column.addLayout(head)

        self.reader_title = ElidedLabel("")
        self.reader_title.setObjectName("SectionTitle")
        column.addWidget(self.reader_title)

        scroll = QScrollArea()
        scroll.setObjectName("AnswerPane")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName("PaneBody")
        self.reader_column = QVBoxLayout(holder)
        self.reader_column.setContentsMargins(0, 0, theme.SP_SM, 0)
        self.reader_column.setSpacing(theme.SP_SM)
        self.reader_column.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(holder)
        column.addWidget(scroll, 1)
        return page

    def show_list(self) -> None:
        self.reader_doc_id = None
        self._citation = None
        self.stack.setCurrentIndex(0)

    def show_citation(self, citation: Citation) -> None:
        """대화에서 고른 인용의 원문을 편다. 지금 선택한 자료의 것만 보여 준다."""
        if self.notebook_id is None:
            return
        if str(citation.doc_id) not in notebook.selected_snapshot(self.db, self.notebook_id):
            self._render_blocked("이 인용은 지금 선택한 자료 밖의 문서입니다. "
                                 "자료를 다시 선택하거나 새로 질문하세요.")
            return
        self._citation = citation
        self._render(citation.doc_id)

    def show_document(self, doc_id: int) -> None:
        if doc_id not in {row["doc_id"] for row in self.rows()}:
            return
        self._citation = None
        self._render(doc_id)

    def _render_blocked(self, message: str) -> None:
        clear_layout(self.reader_column)
        self.reader_doc_id = None
        self.reader_title.setText("")
        self.open_button.hide()
        self.reader_column.addWidget(UnknownBlock(message))
        self.stack.setCurrentIndex(1)

    def _render(self, doc_id: int) -> None:
        clear_layout(self.reader_column)
        self.reader_doc_id = doc_id
        info = self.db.document_context([doc_id]).get(doc_id, {})
        document = self.db.document(doc_id)
        citation = self._citation or Citation(0, doc_id, document["filename"], "", document["path"])
        self.reader_title.setText(document["filename"])
        self.open_button.show()

        sections = self.db.sections(doc_id)
        self._render_guide(doc_id, document)
        card = Card(tone="static")
        card.body.addWidget(_meta_strip(self.db, citation, info, len(sections)))
        if not sections:
            card.body.addWidget(UnknownBlock(
                "이 문서의 본문을 읽지 못해 원문을 보여줄 수 없습니다. ‘원본 열기’로 직접 확인하세요."))
            self.reader_column.addWidget(card)
            self.stack.setCurrentIndex(1)
            return

        cited = _cited_ordinals(sections, citation) if self._citation else set()
        _fill_passages(card.body, sections, cited, doc_id in self._expanded,
                       lambda: self._expand(doc_id))
        _fit_narrow(card)
        self.reader_column.addWidget(card)
        self.stack.setCurrentIndex(1)

    def _render_guide(self, doc_id: int, document) -> None:
        """자료 안내 — 요약·주제어와, 읽는 방식에 대한 주의."""
        row = next((r for r in self.rows() if r["doc_id"] == doc_id), None)
        if row is None:
            return
        if row["origin"] == notebook_tasks.ORIGIN:
            self.reader_column.addWidget(UnknownBlock(BRIEF_NOTE))
        if document["parse_note"]:
            self.reader_column.addWidget(UnknownBlock(document["parse_note"]))
        topics = [t for t in json.loads(row["topics"] or "[]") if isinstance(t, str)]
        if not row["summary"] and not topics:
            return
        guide = Card(tone="static")
        guide.body.addWidget(muted_label("AI가 만든 자료 안내 · 원문이 아닙니다", small=True))
        if row["summary"]:
            guide.body.addWidget(muted_label(row["summary"]))
        if topics:
            guide.body.addWidget(muted_label("주제어 — " + " · ".join(topics), small=True))
        self.reader_column.addWidget(guide)

    def _open_original(self) -> None:
        document = self.db.document(self.reader_doc_id) if self.reader_doc_id is not None else None
        if document is not None:
            open_original(self, self.db, document["path"])

    def _expand(self, doc_id: int) -> None:
        self._expanded.add(doc_id)
        self._render(doc_id)
