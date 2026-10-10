"""문서 화면 — 검증과 안전망.

AI가 틀렸을 때 돌아올 수 있는 곳. 이 화면이 없으면 사용자는 AI를 검증할
방법이 없다. 그래서 AI가 하나도 동작하지 않아도 이 화면은 온전해야 한다.

중복·버전은 개별 행이 아니라 묶음 한 줄로 접는다. 목록의 체감 분량이 즉시
줄어드는 것이 '정리'의 실감이다.

우측 패널은 시점 판정 근거를 보여준다. "무엇을 근거로 2024년이라 했는지"를
사람이 확인하고 고칠 수 있어야 When과 How를 믿을 수 있다.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import docrole, docyears, status
from ...db import Database, representative_predicate
from ...jobs import SummaryRunner
from ...search.documents import find_documents
from .. import theme
from ..widgets import (
    Badge,
    ElidedLabel,
    EmptyState,
    InfoDot,
    UnknownBlock,
    clear_layout,
    muted_label,
    section_title,
    view_title,
)

COLUMNS = ["파일명", "업무", "시점", "형식", "크기", "상태"]

# 상태는 색만으로 구분하지 않는다. 기호와 글자를 함께 쓴다 (PRD §18.4).
# 셀 위젯 대신 아이템으로 그린다 — 위젯 500개는 저사양 PC에서 무겁고,
# 열 너비가 바뀔 때 위젯 위치가 어긋난다.
# 색은 값이 아니라 이름으로 담는다 — 테마를 바꾸면 theme의 색이 갈리는데,
# 값을 import 시점에 복사해 두면 이 표만 옛 색으로 남는다.
#
# 잘 읽힌 문서는 **비워 둔다.** 줄마다 초록 '정상'을 찍으면 수백 줄이 같은 말을
# 하고, 정작 못 읽은 한 줄이 그 사이에 묻힌다. 이 열은 문제가 있는 줄만 말한다.
PARSE_LABEL = {
    "ok": ("", "TEXT_MUTED"),
    "partial": ("◐ 일부만 읽음", "ATTENTION"),
    "empty": ("○ 빈 문서", "TEXT_MUTED"),
    "failed": ("✕ 읽기 실패", "DANGER"),
    "unsupported": ("— 미지원 형식", "TEXT_MUTED"),
    "encrypted": ("△ 암호 걸림", "ATTENTION"),
    "too_large": ("△ 크기 초과", "ATTENTION"),
    "locked": ("△ 다른 곳에서 열려 있음", "ATTENTION"),
    "pending": ("… 대기", "TEXT_MUTED"),
    "skipped": ("· 대상 아님", "TEXT_DISABLED"),
}
MISSING_LABEL = ("⚠ 원본 없음", "DANGER")

# 시점 판정 근거. 번호는 우선순위다 (doc/00 §8.1).
KIND_LABEL = {
    "body": "① 본문",
    "filename": "② 파일명",
    "meta": "③ 문서 속성",
    "folder": "④ 폴더 경로",
    "fs": "⑤ 파일 수정일",
}
PRECISION_LABEL = {"day": "일 단위", "month": "월 단위", "year": "연 단위"}

# 대표 문서만 골라내는 조건. When·How·RAG와 같은 기준을 쓴다(db/repo.py).
REPRESENTATIVE = representative_predicate("d")
DUP_COUNT = (
    "(SELECT COUNT(*) FROM documents y "
    " WHERE y.hash = d.hash AND d.hash IS NOT NULL AND y.missing_since IS NULL)"
)
# 미분류 = 어느 업무에도 붙지 않은 문서. repo.unclassified_count()와 같은 조건을
# 써야 한다 — 상단 집계와 목록이 다른 수를 말하면 둘 다 못 믿는다.
UNCLASSIFIED = "NOT EXISTS(SELECT 1 FROM task_docs td WHERE td.doc_id = d.id)"


class DocumentsView(QWidget):
    def __init__(self, db: Database, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self._page = 0
        self._filter_key = None
        self._current_id: int | None = None
        # 기본 보기: 업무 분류가 존재하면 첫 번째 업무의 문서를 보는 것이 기본 보기다.
        # 업무가 없을 때는 전체 문서를 보여준다.
        tasks = self.db.tasks()
        self._selected_task_id: int | None = tasks[0]["id"] if tasks else None
        self._summary_doc_id: int | None = None      # 요약 중인 문서
        self._summary_error: tuple[int, str] | None = None
        self._summaries = SummaryRunner(db.path, self)
        self._summaries.done.connect(self._on_summary_done)

        self.setObjectName("Canvas")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 찾는 자리(제목·검색·거르개)와 보는 자리(표·상세)를 흰 띠와 회색
        # 바탕으로 가른다. 업무·일정·질문 화면이 모두 이 구조다.
        header = QWidget()
        header.setObjectName("ViewHeader")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        head = QVBoxLayout(header)
        head.setContentsMargins(theme.SP_XL, theme.SP_LG, theme.SP_XL, theme.SP_LG)
        head.setSpacing(theme.SP_MD)

        # 다른 화면과 같은 자리에 같은 ⓘ를 둔다. 설명이 어디 있는지 매번
        # 찾게 하지 않는 것이 도움말의 절반이다.
        title_row = QHBoxLayout()
        title_row.setSpacing(theme.SP_SM)
        title_row.addWidget(view_title("문서"))
        title_row.addWidget(
            InfoDot(
                "찾은 파일을 업무 분류별로 모아 확인하고 관리합니다. "
                "AI가 분류한 업무가 올바른지 확인하고, 필요 시 사용자가 직접 업무 분류를 "
                "변경하거나 미분류 문서에 업무를 매핑할 수 있습니다."
            ),
            0,
            Qt.AlignmentFlag.AlignVCenter,
        )
        title_row.addStretch(1)
        head.addLayout(title_row)
        head.addLayout(self._build_search())
        head.addLayout(self._build_filters())
        outer.addWidget(header)

        body_wrap = QVBoxLayout()
        body_wrap.setContentsMargins(theme.SP_XL, theme.SP_LG, theme.SP_XL, theme.SP_LG)
        body_wrap.setSpacing(theme.SP_SM)

        self.summary = muted_label("")
        body_wrap.addWidget(self.summary)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.category_sidebar = self._build_category_sidebar()
        self.splitter.addWidget(self.category_sidebar)
        listing = QWidget()
        listing.setLayout(self._build_table())
        self.splitter.addWidget(listing)
        self.splitter.addWidget(self._build_detail())
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([220, 680, theme.DETAIL_PANEL_W])
        body_wrap.addWidget(self.splitter, 1)
        body_holder = QWidget()
        body_holder.setLayout(body_wrap)
        outer.addWidget(body_holder, 1)

        self.empty = EmptyState(
            "아직 찾은 문서가 없습니다",
            "자료원을 등록하고 분석을 시작하면 먼저 찾은 파일부터 여기에 나타납니다.",
        )
        body_wrap.addWidget(self.empty, 1)
        self.no_results = EmptyState(
            "검색 조건에 맞는 문서가 없습니다",
            "다른 검색어를 입력하거나 검색 조건을 초기화해 전체 자료를 확인하세요.",
            "검색 조건 초기화", self._reset_search,
        )
        body_wrap.addWidget(self.no_results, 1)

        self.refresh()

    # ── 구성 ────────────────────────────────────────────────────────
    def _build_search(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(theme.SP_SM)
        self.search = QLineEdit()
        self.search.setPlaceholderText("파일명·본문·작성자 검색")
        self.search.setMinimumHeight(theme.CONTROL_H)
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("문서 검색어")
        self.search.returnPressed.connect(self.refresh)
        row.addWidget(self.search, 1)
        find = QPushButton("검색")
        find.setMinimumHeight(theme.CONTROL_H)
        find.setMinimumWidth(88)
        find.clicked.connect(self.refresh)
        row.addWidget(find)
        reset = QPushButton("초기화")
        reset.setObjectName("Quiet")
        reset.setMinimumHeight(theme.CONTROL_H)
        reset.clicked.connect(self._reset_search)
        row.addWidget(reset)
        return row

    def _build_filters(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(theme.SP_LG)
        self.collapse_dups = QCheckBox("중복 묶기")
        self.collapse_dups.setChecked(True)
        self.collapse_dups.setToolTip("내용이 완전히 같은 파일을 한 줄로 접습니다")
        self.collapse_dups.stateChanged.connect(self.refresh)
        row.addWidget(self.collapse_dups)

        self.documents_only = QCheckBox("문서만 보기")
        self.documents_only.setChecked(True)
        self.documents_only.setToolTip("그림·실행파일 등 분석 대상이 아닌 파일을 숨깁니다")
        self.documents_only.stateChanged.connect(self.refresh)
        row.addWidget(self.documents_only)

        # 미분류는 실패가 아니라 정상이다(어느 업무에도 안 붙는 문서는 늘 있다).
        # 다만 "내 업무 문서인데 안 붙은 것"을 찾아 배정하려면 그것만 모아
        # 볼 수 있어야 한다 — 500행 목록에서 눈으로 고를 수는 없다.
        self.unclassified_only = QCheckBox("미분류만 보기")
        self.unclassified_only.setToolTip("어느 업무에도 배정되지 않은 문서만 봅니다")
        self.unclassified_only.stateChanged.connect(self.refresh)
        row.addWidget(self.unclassified_only)
        row.addStretch(1)
        return row

    def _build_category_sidebar(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("CategoryFrame")
        frame.setMinimumWidth(190)
        frame.setMaximumWidth(280)
        vbox = QVBoxLayout(frame)
        vbox.setContentsMargins(theme.SP_MD, theme.SP_MD, theme.SP_MD, theme.SP_MD)
        vbox.setSpacing(theme.SP_SM)

        head = QHBoxLayout()
        head.addWidget(section_title("업무 분류"))
        head.addStretch(1)
        vbox.addLayout(head)

        self.category_search = QLineEdit()
        self.category_search.setPlaceholderText("업무 검색…")
        self.category_search.setClearButtonEnabled(True)
        self.category_search.textChanged.connect(self._filter_categories)
        vbox.addWidget(self.category_search)

        self.category_list = QListWidget()
        self.category_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.category_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.category_list.itemSelectionChanged.connect(self._on_category_selected)
        vbox.addWidget(self.category_list, 1)

        return frame

    def _filter_categories(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.category_list.count()):
            item = self.category_list.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            if data in ("all", "unclassified", "separator"):
                continue
            name = (item.data(Qt.ItemDataRole.UserRole + 1) or "").lower()
            item.setHidden(bool(needle) and needle not in name)

    def _on_category_selected(self) -> None:
        items = self.category_list.selectedItems()
        if not items:
            return
        data = items[0].data(Qt.ItemDataRole.UserRole)
        if data == "separator":
            return
        if data == "all":
            self._selected_task_id = None
            self.unclassified_only.blockSignals(True)
            self.unclassified_only.setChecked(False)
            self.unclassified_only.blockSignals(False)
        elif data == "unclassified":
            self._selected_task_id = None
            self.unclassified_only.blockSignals(True)
            self.unclassified_only.setChecked(True)
            self.unclassified_only.blockSignals(False)
        elif isinstance(data, int):
            self._selected_task_id = data
            self.unclassified_only.blockSignals(True)
            self.unclassified_only.setChecked(False)
            self.unclassified_only.blockSignals(False)

        self._page = 0
        self.refresh()

    def _populate_categories(self) -> None:
        self.category_list.blockSignals(True)
        self.category_list.clear()

        counts = self.db.counts()
        unclass_count = self.db.unclassified_count()
        tasks = self.db.tasks()

        all_item = QListWidgetItem(f"📂 전체 문서   ({counts['total']:,}건)")
        all_item.setData(Qt.ItemDataRole.UserRole, "all")
        self.category_list.addItem(all_item)

        unclass_item = QListWidgetItem(f"❓ 미분류 문서   ({unclass_count:,}건)")
        unclass_item.setData(Qt.ItemDataRole.UserRole, "unclassified")
        if unclass_count > 0:
            unclass_item.setForeground(QColor(theme.color("ATTENTION")))
        self.category_list.addItem(unclass_item)

        sep = QListWidgetItem("── 업무별 분류 ──")
        sep.setData(Qt.ItemDataRole.UserRole, "separator")
        sep.setFlags(Qt.ItemFlag.NoItemFlags)
        sep.setForeground(QColor(theme.color("TEXT_DISABLED")))
        self.category_list.addItem(sep)

        target_item = None
        for task in tasks:
            t_item = QListWidgetItem(f"📁 {task['name']}   ({task['doc_count']:,}건)")
            t_item.setData(Qt.ItemDataRole.UserRole, task["id"])
            t_item.setData(Qt.ItemDataRole.UserRole + 1, task["name"])
            self.category_list.addItem(t_item)
            if self._selected_task_id == task["id"]:
                target_item = t_item

        if self.unclassified_only.isChecked():
            self.category_list.setCurrentItem(unclass_item)
        elif self._selected_task_id is not None and target_item:
            self.category_list.setCurrentItem(target_item)
        elif not tasks or self._selected_task_id is None:
            self.category_list.setCurrentItem(all_item)

        self.category_list.blockSignals(False)

    def _update_banner(self, count: int) -> None:
        if self._selected_task_id is not None:
            tasks = self.db.tasks()
            task = next((t for t in tasks if t["id"] == self._selected_task_id), None)
            task_name = task["name"] if task else f"업무 #{self._selected_task_id}"
            self.banner_title.setText(f"📁 {task_name}   ({count:,}건)")
            self.banner_desc.setText(
                "이 업무에 속한 문서들입니다. 잘못 분류된 문서는 [업무 분류 변경]이나 마우스 우클릭으로 변경할 수 있습니다."
            )
        elif self.unclassified_only.isChecked():
            self.banner_title.setText(f"❓ 미분류 문서   ({count:,}건)")
            self.banner_desc.setText(
                "어느 업무에도 분류되지 않은 문서입니다. 문서를 선택한 뒤 업무를 지정(매핑)해 주세요."
            )
        else:
            self.banner_title.setText(f"📂 전체 문서   ({count:,}건)")
            self.banner_desc.setText(
                "등록된 전체 문서 목록입니다. 좌측의 업무 분류를 선택하면 각 업무별 문서를 볼 수 있습니다."
            )

    def _build_table(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(theme.SP_SM)

        # 상단 업무 분류 배너 및 빠른 변경 조작
        banner = QFrame()
        banner.setObjectName("CategoryBanner")
        b_layout = QHBoxLayout(banner)
        b_layout.setContentsMargins(theme.SP_MD, theme.SP_SM, theme.SP_MD, theme.SP_SM)
        b_layout.setSpacing(theme.SP_MD)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        self.banner_title = QLabel("업무 분류")
        self.banner_title.setObjectName("SubTitle")
        f = self.banner_title.font()
        f.setBold(True)
        self.banner_title.setFont(f)
        self.banner_desc = muted_label("", small=True)
        title_col.addWidget(self.banner_title)
        title_col.addWidget(self.banner_desc)
        b_layout.addLayout(title_col, 1)

        self.btn_change_category = QPushButton("업무 분류 변경…")
        self.btn_change_category.setObjectName("Quiet")
        self.btn_change_category.setEnabled(False)
        self.btn_change_category.setToolTip("선택한 문서의 업무 분류를 다른 업무로 변경(매핑)합니다")
        self.btn_change_category.clicked.connect(self._change_selected_category)
        b_layout.addWidget(self.btn_change_category)
        column.addWidget(banner)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(theme.ROW_H)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        header = self.table.horizontalHeader()
        # 머리글은 칸의 내용과 같은 쪽에 선다. 가운데에 두면 왼쪽으로 붙은
        # 내용과 어긋나 어느 열의 이름인지 눈이 한 번 더 찾는다.
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, len(COLUMNS)):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.doubleClicked.connect(self._open_selected)
        column.addWidget(self.table, 1)

        paging = QHBoxLayout()
        self.page_label = muted_label("", wrap=False)
        paging.addWidget(self.page_label)
        paging.addStretch(1)
        self.page_size = QComboBox()
        for size in (50, 100, 200):
            self.page_size.addItem(f"{size}개씩", size)
        self.page_size.setCurrentIndex(1)
        self.page_size.setAccessibleName("페이지당 문서 수")
        self.page_size.currentIndexChanged.connect(self.refresh)
        paging.addWidget(self.page_size)
        self.previous_page = QPushButton("이전")
        self.previous_page.clicked.connect(lambda: self._turn_page(-1))
        self.next_page = QPushButton("다음")
        self.next_page.clicked.connect(lambda: self._turn_page(1))
        paging.addWidget(self.previous_page)
        paging.addWidget(self.next_page)
        column.addLayout(paging)
        return column

    def _build_detail(self) -> QWidget:
        # 판 하나에 '고른 문서의 내용'(스크롤)과 '그 문서로 할 일'(아래 고정)을
        # 함께 둔다. 원본 열기는 고른 문서에 대한 조작이라 그 문서 곁에 있어야
        # 한다 — 예전에는 표 아래 왼쪽 구석에 떨어져 있어 무엇을 여는지 멀었다.
        frame = QFrame()
        frame.setObjectName("DetailFrame")
        frame.setMinimumWidth(300)
        box = QVBoxLayout(frame)
        box.setContentsMargins(1, 1, 1, 1)
        box.setSpacing(0)

        panel = QScrollArea()
        panel.setObjectName("DetailPane")
        panel.setWidgetResizable(True)
        panel.setFrameShape(QFrame.Shape.NoFrame)
        # 긴 경로 때문에 가로 스크롤이 생기면 패널이 지저분해진다. 경로는
        # 가운데를 줄여 보여주고 전체는 툴팁으로 준다.
        panel.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        inner = QWidget()
        inner.setObjectName("PaneBody")
        self.detail = QVBoxLayout(inner)
        self.detail.setContentsMargins(theme.SP_LG, theme.SP_LG, theme.SP_LG, theme.SP_LG)
        self.detail.setSpacing(theme.SP_MD)
        self.detail.setAlignment(Qt.AlignmentFlag.AlignTop)
        panel.setWidget(inner)
        box.addWidget(panel, 1)

        bar = QFrame()
        bar.setObjectName("ActionBar")
        actions = QHBoxLayout(bar)
        actions.setContentsMargins(theme.SP_LG, theme.SP_MD, theme.SP_LG, theme.SP_MD)
        actions.setSpacing(theme.SP_SM)
        open_file = self.open_file = QPushButton("원본 열기")
        open_file.setObjectName("Primary")
        open_file.setToolTip("고른 문서를 기본 프로그램으로 엽니다. 읽기만 하고 고치지 않습니다")
        open_file.clicked.connect(self._open_selected)
        actions.addWidget(open_file)
        open_folder = self.open_folder = QPushButton("폴더 열기")
        open_folder.clicked.connect(self._reveal_selected)
        actions.addWidget(open_folder)
        actions.addStretch(1)
        box.addWidget(bar)

        self.detail_panel = panel
        return frame

    # ── 목록 ────────────────────────────────────────────────────────
    def refresh(self) -> None:
        rows = self._query(self.search.text().strip())
        has_rows = bool(rows)
        counts = self.db.counts()
        self.splitter.setVisible(has_rows)
        self.empty.setVisible(not has_rows and not counts["total"])
        self.no_results.setVisible(not has_rows and bool(counts["total"]))
        self.open_file.setEnabled(has_rows)
        self.open_folder.setEnabled(has_rows)
        result = self._result
        first = result.page * self.page_size.currentData() + 1 if result.total else 0
        last = first + len(rows) - 1 if rows else 0
        self.page_label.setText(f"{result.page + 1} / {result.pages} 페이지")
        self.previous_page.setEnabled(result.page > 0)
        self.next_page.setEnabled(result.page + 1 < result.pages)
        parts = [f"파일 {counts['total']:,}건", f"문서 {counts['documents']:,}건"]
        if counts["duplicate_extra"]:
            parts.append(f"중복 {counts['duplicate_extra']:,}건")
        if counts["parse_pending"]:
            parts.append(f"읽는 중 {counts['parse_pending']:,}건")
        if counts["parse_failed"]:
            parts.append(f"읽지 못함 {counts['parse_failed']:,}건")
        if counts["missing"]:
            parts.append(f"원본 없음 {counts['missing']:,}건")
        self.summary.setText(f"검색 결과 {result.total:,}건" + (f" · {first:,}–{last:,}번째 표시" if rows else ""))
        self.summary.setToolTip(" · ".join(parts))

        # 건수를 거르개에 붙여 둔다. 누르기 전에 규모를 알아야 누를지 말지
        # 정할 수 있다(일정 화면 탭에서 쓴 것과 같은 규칙).
        self.unclassified_only.setText(f"미분류만 보기 ({self.db.unclassified_count():,})")

        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            self._fill(r, row)

        # 검색으로 목록이 바뀌면 이전 선택이 목록 밖으로 나갈 수 있다. 그때
        # 패널이 보이지 않는 문서를 계속 보여주면 사용자가 혼란스럽다.
        visible = [row["id"] for row in rows]
        if self._current_id not in visible:
            self._current_id = visible[0] if visible else None
        if self._current_id in visible:
            self.table.selectRow(visible.index(self._current_id))
        self.table.blockSignals(False)
        self._show_detail(self._current_id)

        # 업무 분류 사이드바 건수 동기화 및 상단 배너 갱신
        self._populate_categories()
        self._update_banner(result.total)

    def _turn_page(self, delta: int) -> None:
        self._page += delta
        self.refresh()
        self.table.setFocus()

    def _reset_search(self) -> None:
        self.search.clear()
        for checkbox, checked in ((self.collapse_dups, True), (self.documents_only, True),
                                  (self.unclassified_only, False)):
            checkbox.blockSignals(True)
            checkbox.setChecked(checked)
            checkbox.blockSignals(False)
        self._page = 0
        self.refresh()
        self.search.setFocus()

    def _query(self, term: str) -> list:
        task_id = None if self.unclassified_only.isChecked() else self._selected_task_id
        unclassified = self.unclassified_only.isChecked()
        key = (term, self.documents_only.isChecked(), self.collapse_dups.isChecked(),
               unclassified, task_id, self.page_size.currentData())
        if key != self._filter_key:
            self._page = 0
            self._filter_key = key
        self._result = find_documents(
            self.db, term, documents_only=key[1], collapse=key[2], unclassified=key[3],
            task_id=key[4], page=self._page, page_size=key[5],
        )
        self._page = self._result.page
        return self._result.rows

    def _fill(self, r: int, row) -> None:
        name = row["filename"]
        dup_n = row["dup_n"] if "dup_n" in row.keys() else 0
        if dup_n and dup_n > 1 and self.collapse_dups.isChecked():
            name = f"▸ {name}   ({dup_n}개 묶음)"
        self.table.setItem(r, 0, _cell(name, row["id"], row["path"]))

        task_label = row["task_name"] or "미분류"
        cell_task = _cell(task_label)
        origin = row["task_origin"] if "task_origin" in row.keys() else None
        if origin == "user":
            cell_task.setToolTip(f"업무: {row['task_name']}\n(담당자가 직접 매핑한 업무입니다)")
        elif row["task_name"]:
            cell_task.setToolTip(f"업무: {row['task_name']}\n(AI가 자동 분류한 업무입니다. 틀렸다면 변경하세요)")
        else:
            cell_task.setForeground(QColor(theme.color("ATTENTION")))
            cell_task.setToolTip("어느 업무에도 분류되지 않은 미분류 문서입니다.\n우클릭 또는 [업무 분류 지정]으로 배정하세요.")
        self.table.setItem(r, 1, cell_task)

        self.table.setItem(r, 2, _cell(_when(row)))
        self.table.setItem(r, 3, _cell((row["ext"] or "").lstrip(".").upper()))
        self.table.setItem(r, 4, _cell(_human(row["size"] or 0)))

        if row["missing_since"]:
            label, token = MISSING_LABEL
        else:
            label, token = PARSE_LABEL.get(
                row["parse_status"], (row["parse_status"], "TEXT_MUTED")
            )
        status = _cell(label)
        status.setForeground(QColor(theme.color(token)))
        if row["parse_error"]:
            status.setToolTip(row["parse_error"])
        self.table.setItem(r, 5, status)

    # ── 상세 (시점 근거) ────────────────────────────────────────────
    def _on_selection(self) -> None:
        items = self.table.selectedItems()
        self.btn_change_category.setEnabled(bool(items))
        if not items:
            return
        doc_id = self.table.item(items[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        self._current_id = doc_id
        self._show_detail(doc_id)

    def _show_detail(self, doc_id: int | None) -> None:
        clear_layout(self.detail)
        if doc_id is None:
            return
        row = self.db.document(doc_id)
        if row is None:
            return

        # 파일명에는 띄어쓰기가 없어 줄바꿈이 듣지 않는다. 그대로 두면 이름
        # 하나가 판을 제 폭만큼 밀어내 오른쪽 내용이 통째로 잘린다.
        title = ElidedLabel(row["filename"])
        title.setObjectName("Answer")
        title.setToolTip(f"{row['filename']}\n{row['path']}")
        self.detail.addWidget(title)

        folder = str(Path(row["path"]).parent)
        location = muted_label(_elide_middle(folder), small=True)
        location.setToolTip(folder)
        self.detail.addWidget(location)

        self._add_summary(row)
        self._add_when(row)
        self._add_kind(row)
        self._add_tasks(row)
        self._add_facts(row)
        self._add_trouble(row)

    def _add_summary(self, row) -> None:
        """AI 요약은 미리 만들어 두지 않는다.

        문서 1만 건을 미리 요약하면 27시간이 걸린다(실측). 쓰지도 않을 요약에
        그 시간을 쓰는 대신, 사용자가 문서를 열었을 때 그 하나만 만든다.
        """
        if row["parse_status"] not in ("ok", "partial"):
            return

        self.detail.addWidget(_divider())
        self.detail.addWidget(section_title("요약"))

        analysis = self.db.analysis(row["id"])
        if analysis and analysis["summary"]:
            body = muted_label(analysis["summary"])
            self.detail.addWidget(body)
            self.detail.addWidget(
                muted_label(
                    f"AI 제안 · {analysis['model'] or ''} · 원문에서 직접 확인하세요",
                    small=True,
                )
            )
            return

        if self._summary_doc_id == row["id"]:
            self.detail.addWidget(muted_label("요약하는 중… (10초쯤 걸립니다)"))
            return

        if self._summary_error and self._summary_error[0] == row["id"]:
            self.detail.addWidget(UnknownBlock(self._summary_error[1]))

        button = QPushButton("AI 요약 만들기")
        button.setObjectName("Quiet")
        button.setEnabled(not self._summaries.running)
        button.clicked.connect(lambda _=False, i=row["id"]: self._start_summary(i))
        self.detail.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)

    def _start_summary(self, doc_id: int) -> None:
        if not self._summaries.start(doc_id):
            return
        self._summary_doc_id = doc_id
        self._summary_error = None
        self._show_detail(doc_id)

    def _on_summary_done(self, doc_id: int, summary: str, error: str) -> None:
        self._summary_doc_id = None
        self._summary_error = (doc_id, error) if error else None
        if self._current_id == doc_id:
            self._show_detail(doc_id)

    def _add_when(self, row) -> None:
        """시점과 그 근거. 이 제품에서 가장 중요한 검증 지점이다."""
        head = QHBoxLayout()
        head.setSpacing(theme.SP_SM)
        head.addWidget(section_title("시점"))
        # 표시는 예외에만. '자료에서 추정'은 기본 상태라 적지 않는다.
        date_state = status.of_document_date(row)
        if date_state != status.INFERRED:
            head.addWidget(Badge.state(date_state))
        head.addStretch(1)
        self.detail.addLayout(head)

        if not row["eff_date"]:
            if row["date_decided_by"] == "user":
                # 사람이 '모름'으로 확정한 것과 시스템이 못 찾은 것은 다르다.
                self.detail.addWidget(
                    muted_label(
                        "담당자가 ‘날짜 모름’으로 확정했습니다. 다시 분석해도 "
                        "이 판단은 바뀌지 않습니다."
                    )
                )
            else:
                self.detail.addWidget(
                    UnknownBlock(
                        "이 문서가 언제 작성됐는지 확인할 단서를 찾지 못했습니다. "
                        "본문·파일명·문서 속성·폴더 어디에도 날짜가 없습니다."
                    )
                )
            self._add_date_actions(row)
            return

        kind = row["eff_date_kind"]
        # 날짜가 이 구획의 답이다. 값으로 세우고, 어디서 읽었는지는 아래로 물린다.
        when = QLabel(_when(row))
        when.setObjectName("Value")
        when.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail.addWidget(when)
        self.detail.addWidget(
            muted_label(
                f"{KIND_LABEL.get(kind, kind or '근거 미상')}에서 판정"
                f"   ·   {PRECISION_LABEL.get(row['eff_precision'], '')}",
                small=True,
            )
        )
        if kind == "fs":
            self.detail.addWidget(
                UnknownBlock(
                    "파일 수정일로만 판정했습니다. 폴더째 복사하면 바뀌는 값이라 "
                    "반복 주기 계산에서는 제외됩니다."
                )
            )

        candidates = self.db.dates(row["id"])
        chosen = [c for c in candidates if c["kind"] == kind]
        if chosen and chosen[0]["raw"]:
            evidence = chosen[0]
            text = f"근거:  “{_plain_stamp(evidence['raw'])}”"
            if evidence["locator"]:
                text += f"   ·   {evidence['locator']}"
            self.detail.addWidget(muted_label(text, small=True))

        others = [c for c in candidates if c["kind"] != kind]
        if others:
            self.detail.addWidget(muted_label("다른 후보", small=True))
            for candidate in sorted(others, key=lambda c: _kind_order(c["kind"])):
                label = KIND_LABEL.get(candidate["kind"], candidate["kind"])
                self.detail.addWidget(
                    muted_label(
                        f"   {label}   {candidate['value'][:7]}   "
                        f"“{_plain_stamp(candidate['raw'])[:24]}”",
                        small=True,
                    )
                )

        if row["date_decided_by"] == "user":
            self.detail.addWidget(
                muted_label(
                    f"{status.symbol(status.CONFIRMED)} 담당자가 확정한 시점입니다. "
                    f"다시 분석해도 바뀌지 않습니다.",
                    small=True,
                )
            )
        self._add_date_actions(row)

    def _add_date_actions(self, row) -> None:
        """추정 옆에는 늘 교정 수단이 있어야 한다 (계획서 §11).

        후보를 보여 주기만 하고 고를 수 없으면, 사용자는 틀린 것을 발견하고도
        할 수 있는 일이 없다 — 그 순간 화면 전체가 '읽을거리'가 된다.
        """
        button = QPushButton("시점 고치기" if row["eff_date"] else "시점 직접 지정")
        button.setObjectName("Quiet")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda _=False, i=row["id"]: self._edit_date(i))
        self.detail.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)

    def _edit_date(self, doc_id: int) -> None:
        row = self.db.document(doc_id)
        if row is None:
            return
        picked = _DateDialog.run(self, row, self.db.dates(doc_id))
        if picked is None:
            return
        value, precision, kind = picked
        self.db.set_document_date(doc_id, value, precision=precision, kind=kind)
        self.refresh()

    # ── 상세 (문서 역할·연도) ───────────────────────────────────────
    def _add_kind(self, row) -> None:
        """이 문서의 역할과 연도 갈래를 보이고 고칠 수 있게 한다 (계획서 M8).

        못 정한 역할은 '정하지 못함'으로 그대로 보인다. 사람이 고친 값은
        decided_by='user'로 남아 재분석이 덮지 않는다.
        """
        doc_id = row["id"]
        info = docrole.roles(self.db, [doc_id]).get(doc_id)
        marks = docyears.years(self.db, [doc_id]).get(doc_id, {})

        self.detail.addWidget(section_title("문서 갈래"))
        role = info["role"] if info else docrole.OTHER
        by_user = bool(info) and info["decided_by"] == "user"
        line = docrole.ROLE_LABEL.get(role, role)
        if by_user:
            line += "  · 담당자가 정함"
        elif info and role != docrole.OTHER:
            line += f"  · 근거: {info['basis']}"
        self.detail.addWidget(muted_label(f"역할: {line}", small=True))

        shown = [f"{docyears.KIND_LABEL[kind]} {', '.join(f'{y}년' for y in marks[kind])}"
                 for kind in docyears.KINDS if marks.get(kind)]
        self.detail.addWidget(muted_label(
            "연도: " + (" / ".join(shown) if shown else "읽어 내지 못함"), small=True))

        button = QPushButton("역할·연도 고치기")
        button.setObjectName("Quiet")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda _=False, i=doc_id: self._edit_kind(i))
        self.detail.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)

    def _edit_kind(self, doc_id: int) -> None:
        info = docrole.roles(self.db, [doc_id]).get(doc_id)
        marks = docyears.years(self.db, [doc_id]).get(doc_id, {})
        picked = _KindDialog.run(
            self, info["role"] if info else docrole.OTHER, marks)
        if picked is None:
            return
        role, year_changes = picked
        if role != (info["role"] if info else docrole.OTHER):
            with self.db.transaction():
                docrole.set_role(self.db, doc_id, role)
        for kind, year in year_changes.items():
            docyears.set_year(self.db, doc_id, kind, year)
        self.refresh()

    # ── 상세 (업무 배정) ────────────────────────────────────────────
    def _add_tasks(self, row) -> None:
        """이 문서가 어느 업무에 속하는가, 그리고 그것을 여기서 고칠 수 있는가.

        미분류 문서를 배정하는 자리를 업무 화면에 두면 순서가 거꾸로다 —
        사용자는 '이 파일이 어느 업무지?'를 문서를 보면서 판단한다.
        """
        if row["parse_status"] == "skipped":
            return

        self.detail.addWidget(_divider())
        self.detail.addWidget(section_title("업무"))

        links = self.db.document_tasks(row["id"])
        if not links:
            self.detail.addWidget(
                UnknownBlock(
                    "어느 업무에도 배정되지 않았습니다. AI가 분류하지 못했거나 "
                    "새로 추가된 문서입니다. 업무와 무관한 문서라면 '업무 없음'으로 표시해 두세요."
                )
            )
            # 업무 직접 지정 버튼
            btn_map = QPushButton("👉 업무 분류 지정(매핑)")
            btn_map.setObjectName("Primary")
            btn_map.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_map.setToolTip("이 문서를 특정 업무에 배정(매핑)합니다")
            btn_map.clicked.connect(lambda _=False, i=row["id"]: self._assign_task(i))
            self.detail.addWidget(btn_map, alignment=Qt.AlignmentFlag.AlignLeft)

            # 인수인계 가이드(계획서 §18)의 '미분류 최근 문서'는 여기서
            # 끝난다. 배정하면 미분류에서 빠지고, 무관한 문서면 표시로
            # 끝난다 — 둘 다 아니면 그 문서는 영원히 할 일로 남는다.
            marked = row["id"] in self.db.stray_marks()
            mark = QPushButton("업무 없음 표시 해제" if marked else "업무 없음으로 표시")
            mark.setObjectName("Quiet")
            mark.setCursor(Qt.CursorShape.PointingHandCursor)
            mark.setToolTip(
                "문서를 지우거나 숨기지 않습니다. 인수인계에서 확인한 것으로만 셉니다."
            )
            mark.clicked.connect(
                lambda _=False, i=row["id"], done=not marked: self._mark_stray(i, done)
            )
            self.detail.addWidget(mark, alignment=Qt.AlignmentFlag.AlignLeft)
        for link in links:
            line = QHBoxLayout()
            line.setSpacing(theme.SP_SM)
            name = muted_label(link["name"])
            name.setToolTip(link["name"])
            line.addWidget(name, 1)

            # 분류 출처 뱃지: AI 자동 분류 vs 담당자 직접 매핑
            if link["origin"] == "user":
                line.addWidget(Badge("직접지정", "ok"))
            else:
                line.addWidget(Badge("AI분류", "attention"))

            if link["is_primary"]:
                line.addWidget(Badge("대표", "ok"))

            # 업무 변경 버튼
            btn_change = QPushButton("업무 변경")
            btn_change.setObjectName("Quiet")
            btn_change.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_change.setToolTip("AI가 잘못 분류했거나 다른 업무로 변경(재매핑)합니다")
            btn_change.clicked.connect(
                lambda _=False, d=row["id"], t=link["id"]: self._prompt_move_task(d, t)
            )
            line.addWidget(btn_change)

            drop = QPushButton("배정 해제")
            drop.setObjectName("Quiet")
            drop.setCursor(Qt.CursorShape.PointingHandCursor)
            drop.setToolTip("이 업무에서만 뗍니다. 문서와 분석 결과는 그대로입니다.")
            drop.clicked.connect(
                lambda _=False, t=link["id"], d=row["id"]: self._detach_task(t, d)
            )
            line.addWidget(drop)
            self.detail.addLayout(line)

        assign = QPushButton("＋ 업무에 배정")
        assign.setObjectName("Quiet")
        assign.setCursor(Qt.CursorShape.PointingHandCursor)
        assign.setToolTip("한 문서가 여러 업무에 속할 수 있습니다")
        assign.clicked.connect(lambda _=False, i=row["id"]: self._assign_task(i))
        self.detail.addWidget(assign, alignment=Qt.AlignmentFlag.AlignLeft)

    def _assign_task(self, doc_id: int) -> None:
        taken = {link["id"] for link in self.db.document_tasks(doc_id)}
        choices = [task for task in self.db.tasks() if task["id"] not in taken]
        if not choices:
            self.summary.setText(
                "⚠ 배정할 업무가 없습니다. 업무 화면에서 업무를 먼저 만들거나, "
                "이미 이 문서가 모든 업무에 배정돼 있습니다."
            )
            return
        task_id = _TaskPicker.run(self, choices)
        if task_id is None:
            return
        self.db.assign_document(task_id, doc_id)
        row = self.db.document(doc_id)
        target_name = next((t["name"] for t in choices if t["id"] == task_id), "")
        self.summary.setText(f"✓ '{row['filename'] if row else ''}' 문서를 '{target_name}' 업무에 배정했습니다.")
        self.refresh()

    def _mark_stray(self, doc_id: int, done: bool) -> None:
        self.db.mark_stray(doc_id, done)
        self.refresh()

    def _detach_task(self, task_id: int, doc_id: int) -> None:
        self.db.detach_document(task_id, doc_id)
        self.refresh()

    def _prompt_move_task(self, doc_id: int, from_task_id: int) -> None:
        row = self.db.document(doc_id)
        if row is None:
            return
        tasks = self.db.tasks()
        current_tasks = [t for t in tasks if t["id"] == from_task_id]
        target_task_id = _TaskMoveDialog.run(
            self,
            row["filename"],
            current_tasks,
            tasks,
        )
        if target_task_id is None or target_task_id == from_task_id:
            return

        self.db.move_document(from_task_id, target_task_id, doc_id)
        target_name = next((t["name"] for t in tasks if t["id"] == target_task_id), f"#{target_task_id}")
        self.summary.setText(f"✓ '{row['filename']}' 문서를 '{target_name}' 업무로 분류를 변경했습니다.")
        self.refresh()

    def _prompt_change_category(self, doc_id: int) -> None:
        row = self.db.document(doc_id)
        if row is None:
            return
        tasks = self.db.tasks()
        if not tasks:
            self.summary.setText("⚠ 등록된 업무가 없습니다. 먼저 업무를 생성해 주세요.")
            return

        current_tasks = self.db.document_tasks(doc_id)
        current_task_ids = {t["id"] for t in current_tasks}

        target_task_id = _TaskMoveDialog.run(
            self,
            row["filename"],
            current_tasks,
            tasks,
        )
        if target_task_id is None:
            return

        target_task = next((t for t in tasks if t["id"] == target_task_id), None)
        target_name = target_task["name"] if target_task else f"업무 #{target_task_id}"

        if current_tasks:
            from_task = self._selected_task_id if self._selected_task_id in current_task_ids else current_tasks[0]["id"]
            self.db.move_document(from_task, target_task_id, doc_id)
            self.summary.setText(f"✓ '{row['filename']}' 문서를 '{target_name}' 업무로 분류를 변경했습니다.")
        else:
            self.db.assign_document(target_task_id, doc_id)
            self.summary.setText(f"✓ '{row['filename']}' 문서를 '{target_name}' 업무로 배정했습니다.")

        self.refresh()

    def _change_selected_category(self) -> None:
        items = self.table.selectedItems()
        if not items:
            return
        doc_id = self.table.item(items[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        if doc_id is None:
            return
        self._prompt_change_category(doc_id)

    def _show_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if not item:
            return
        row = item.row()
        self.table.selectRow(row)
        doc_id = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if doc_id is None:
            return

        menu = QMenu(self)
        current_tasks = self.db.document_tasks(doc_id)
        if current_tasks:
            act_change = menu.addAction("🏷 업무 분류 변경...")
            act_change.triggered.connect(lambda: self._prompt_change_category(doc_id))
            act_assign = menu.addAction("＋ 다른 업무에 추가 배정...")
            act_assign.triggered.connect(lambda: self._assign_task(doc_id))
            act_detach = menu.addAction("✕ 업무 배정 해제")
            from_t = self._selected_task_id if self._selected_task_id in {t["id"] for t in current_tasks} else current_tasks[0]["id"]
            act_detach.triggered.connect(lambda: self._detach_task(from_t, doc_id))
        else:
            act_assign = menu.addAction("👉 업무 분류 지정...")
            act_assign.triggered.connect(lambda: self._assign_task(doc_id))

        menu.addSeparator()
        act_open = menu.addAction("📄 원본 파일 열기")
        act_open.triggered.connect(self._open_selected)
        act_folder = menu.addAction("📁 폴더 열기")
        act_folder.triggered.connect(self._reveal_selected)

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _add_facts(self, row) -> None:
        self.detail.addWidget(_divider())
        facts = [
            ("형식", (row["ext"] or "").lstrip(".").upper() or "—"),
            ("크기", _human(row["size"] or 0)),
            ("작성자", row["author"] or "—"),
            ("글자 수", f"{row['char_count']:,}" if row["char_count"] else "—"),
            ("파일 수정일", (row["fs_mtime"] or "—")[:10]),
        ]
        for label, value in facts:
            self.detail.addWidget(muted_label(f"{label}   {value}", small=True))

    def _add_trouble(self, row) -> None:
        if row["missing_since"]:
            self.detail.addWidget(
                UnknownBlock("원본을 찾을 수 없습니다. 분석 기록은 그대로 보존합니다.")
            )
        if row["parse_error"]:
            self.detail.addWidget(UnknownBlock(f"읽지 못했습니다 — {row['parse_error']}"))
        if row["parse_note"]:
            self.detail.addWidget(muted_label(f"ⓘ {row['parse_note']}", small=True))

    # ── 원본 열기 ───────────────────────────────────────────────────
    def _selected_path(self) -> Path | None:
        items = self.table.selectedItems()
        if not items:
            return None
        path = self.table.item(items[0].row(), 0).data(Qt.ItemDataRole.UserRole + 1)
        return Path(path) if path else None

    def _open_selected(self) -> None:
        path = self._selected_path()
        if path is None:
            return
        if not path.exists():
            self.summary.setText(f"⚠ 원본을 찾을 수 없습니다: {path}")
            return
        self.db.audit("document.open", str(path))
        _launch(path)

    def _reveal_selected(self) -> None:
        path = self._selected_path()
        if path is None:
            return
        target = path.parent
        if not target.exists():
            self.summary.setText(f"⚠ 폴더를 찾을 수 없습니다: {target}")
            return
        if sys.platform == "win32" and path.exists():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            _launch(target)


class _DateDialog(QDialog):
    """시점 교정 — 후보 중에서 고르거나, 직접 적거나, ‘모름’으로 확정한다.

    후보를 라디오로 내놓는 이유: 날짜를 손으로 다시 치게 하면 오타가 들어오고,
    무엇보다 **근거와의 연결이 끊긴다.** 후보를 고르면 그 후보의 출처(본문·
    파일명·문서 속성…)를 그대로 유지하므로, 확정한 뒤에도 "무엇을 보고
    정했는지"가 화면에 남는다.

    ‘날짜 모름’도 하나의 판단이다. 빈칸으로 두는 것과 달리 다음 재분석이
    다시 추정해 덮어쓰지 않는다(repo.set_document_date 참고).
    """

    def __init__(self, parent: QWidget, row, candidates) -> None:
        super().__init__(parent)
        self.setWindowTitle("시점 고치기")
        self.setMinimumWidth(460)

        column = QVBoxLayout(self)
        column.setSpacing(theme.SP_MD)
        column.addWidget(muted_label(row["filename"]))

        self._group = QButtonGroup(self)
        self._choices: dict[QRadioButton, tuple[str | None, str, str]] = {}

        ordered = sorted(candidates, key=lambda c: _kind_order(c["kind"]))
        for candidate in ordered:
            label = KIND_LABEL.get(candidate["kind"], candidate["kind"])
            precision = PRECISION_LABEL.get(candidate["precision"], "")
            text = f"{label}   {_value_text(candidate['value'], candidate['precision'])}"
            if precision:
                text += f" ({precision})"
            if candidate["raw"]:
                text += f"   “{candidate['raw'][:24]}”"
            if candidate["locator"]:
                text += f"   · {candidate['locator']}"
            button = QRadioButton(text)
            if candidate["kind"] == row["eff_date_kind"]:
                button.setChecked(True)
            self._group.addButton(button)
            self._choices[button] = (
                candidate["value"], candidate["precision"], candidate["kind"]
            )
            column.addWidget(button)

        if not ordered:
            column.addWidget(
                muted_label("자료에서 찾은 날짜 후보가 없습니다. 직접 적어 주세요.",
                            small=True)
            )

        self.manual = QRadioButton("직접 입력")
        self._group.addButton(self.manual)
        column.addWidget(self.manual)

        self.text = QLineEdit()
        self.text.setPlaceholderText("2024-03-11  ·  2024-03  ·  2024")
        self.text.textEdited.connect(lambda _t: self.manual.setChecked(True))
        column.addWidget(self.text)

        self.unknown = QRadioButton("날짜 모름으로 확정")
        self.unknown.setToolTip("다시 분석해도 날짜를 추정하지 않습니다")
        self._group.addButton(self.unknown)
        column.addWidget(self.unknown)

        if not ordered and not row["eff_date"]:
            self.manual.setChecked(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)

        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._group.buttonToggled.connect(lambda *_: self._revalidate())
        self.text.textChanged.connect(self._revalidate)
        self._revalidate()

    def choice(self) -> tuple[str | None, str, str] | None:
        """(value, precision, kind). 고른 것이 없거나 입력이 틀리면 None."""
        if self.unknown.isChecked():
            return (None, "day", "user")
        if self.manual.isChecked():
            parsed = _parse_date_text(self.text.text())
            if parsed is None:
                return None
            return (parsed[0], parsed[1], "user")
        for button, value in self._choices.items():
            if button.isChecked():
                return value
        return None

    def _revalidate(self) -> None:
        self._ok.setEnabled(self.choice() is not None)

    @classmethod
    def run(cls, parent: QWidget, row, candidates) -> tuple[str | None, str, str] | None:
        dialog = cls(parent, row, candidates)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.choice()


class _TaskPicker(QDialog):
    """이 문서를 어느 업무에 붙일지 고른다.

    업무가 수십 개가 되면 목록만으로는 못 찾으므로 이름 거르기를 함께 둔다.
    """

    def __init__(self, parent: QWidget, tasks) -> None:
        super().__init__(parent)
        self.setWindowTitle("업무에 배정")
        self.setMinimumWidth(460)

        column = QVBoxLayout(self)
        column.setSpacing(theme.SP_MD)
        column.addWidget(
            muted_label("이 문서를 어느 업무의 자료로 볼지 고르세요. "
                        "한 문서가 여러 업무에 속해도 됩니다.")
        )

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("업무 이름으로 거르기")
        self.filter.textChanged.connect(self._apply_filter)
        column.addWidget(self.filter)

        self.list = QListWidget()
        for task in tasks:
            item = QListWidgetItem(f"{task['name']}   ({task['doc_count']:,}건)")
            item.setData(Qt.ItemDataRole.UserRole, task["id"])
            item.setData(Qt.ItemDataRole.UserRole + 1, task["name"])
            self.list.addItem(item)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        column.addWidget(self.list)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)

        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setEnabled(False)
        self.list.itemSelectionChanged.connect(
            lambda: self._ok.setEnabled(bool(self.list.selectedItems()))
        )

    def _apply_filter(self, term: str) -> None:
        needle = term.strip()
        for index in range(self.list.count()):
            item = self.list.item(index)
            name = item.data(Qt.ItemDataRole.UserRole + 1) or ""
            item.setHidden(bool(needle) and needle not in name)

    @classmethod
    def run(cls, parent: QWidget, tasks) -> int | None:
        dialog = cls(parent, tasks)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        items = dialog.list.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else None


class _TaskMoveDialog(QDialog):
    """문서의 업무 분류를 확인하고 다른 업무로 변경(매핑)한다."""

    def __init__(self, parent: QWidget, filename: str, current_tasks: list, all_tasks: list) -> None:
        super().__init__(parent)
        self.setWindowTitle("업무 분류 변경 / 매핑")
        self.setMinimumWidth(460)
        self.setMinimumHeight(420)

        column = QVBoxLayout(self)
        column.setSpacing(theme.SP_MD)

        title = ElidedLabel(f"문서: {filename}")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        column.addWidget(title)

        curr_names = ", ".join(t["name"] for t in current_tasks) if current_tasks else "미분류"
        curr_label = QLabel(f"현재 분류: {curr_names}")
        curr_label.setStyleSheet("color: #4B5563; font-weight: 500;")
        column.addWidget(curr_label)

        column.addWidget(
            muted_label("변경하여 새로 매핑할 업무를 목록에서 선택하세요.", small=True)
        )

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("업무 이름으로 검색")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        column.addWidget(self.filter)

        self.list = QListWidget()
        curr_ids = {t["id"] for t in current_tasks}
        for task in all_tasks:
            is_curr = task["id"] in curr_ids
            suffix = " (현재 분류)" if is_curr else ""
            item = QListWidgetItem(f"📁 {task['name']}   ({task['doc_count']:,}건){suffix}")
            item.setData(Qt.ItemDataRole.UserRole, task["id"])
            item.setData(Qt.ItemDataRole.UserRole + 1, task["name"])
            if is_curr:
                item.setForeground(QColor(theme.color("TEXT_DISABLED")))
            self.list.addItem(item)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        column.addWidget(self.list)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)

        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setText("분류 변경")
        self._ok.setEnabled(False)
        self.list.itemSelectionChanged.connect(self._revalidate)

    def _revalidate(self) -> None:
        items = self.list.selectedItems()
        self._ok.setEnabled(bool(items))

    def _apply_filter(self, term: str) -> None:
        needle = term.strip().lower()
        for index in range(self.list.count()):
            item = self.list.item(index)
            name = (item.data(Qt.ItemDataRole.UserRole + 1) or "").lower()
            item.setHidden(bool(needle) and needle not in name)

    @classmethod
    def run(cls, parent: QWidget, filename: str, current_tasks: list, all_tasks: list) -> int | None:
        dialog = cls(parent, filename, current_tasks, all_tasks)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        items = dialog.list.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else None


class _KindDialog(QDialog):
    """역할과 갈래별 연도를 고른다. 건드리지 않은 연도는 바꾸지 않는다."""

    _KEEP, _NONE = "keep", "none"

    def __init__(self, parent: QWidget, role: str, marks: dict[str, list[int]]) -> None:
        super().__init__(parent)
        self.setWindowTitle("문서 역할·연도 고치기")
        self.setMinimumWidth(380)
        column = QVBoxLayout(self)
        column.setSpacing(theme.SP_MD)
        column.addWidget(muted_label(
            "고친 값은 다시 분석해도 그대로 남습니다. 연도는 건드리지 않으면 바뀌지 않습니다."))

        column.addWidget(QLabel("역할"))
        self.role = QComboBox()
        for key in docrole.ROLES:
            self.role.addItem(docrole.ROLE_LABEL[key], key)
        self.role.setCurrentIndex(max(0, self.role.findData(role)))
        column.addWidget(self.role)

        this_year = date.today().year
        self.years: dict[str, QComboBox] = {}
        for kind in docyears.KINDS:
            column.addWidget(QLabel(f"{docyears.KIND_LABEL[kind]} 연도"))
            combo = QComboBox()
            current = ", ".join(str(y) for y in marks.get(kind, [])) or "없음"
            combo.addItem(f"그대로 ({current})", self._KEEP)
            combo.addItem("이 문서에는 없음", self._NONE)
            span = set(range(this_year - 12, this_year + 2)) | set(marks.get(kind, []))
            for year in sorted(span, reverse=True):
                combo.addItem(f"{year}년", year)
            self.years[kind] = combo
            column.addWidget(combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)

    def result_value(self) -> tuple[str, dict[str, int | None]]:
        changes: dict[str, int | None] = {}
        for kind, combo in self.years.items():
            data = combo.currentData()
            if data == self._KEEP:
                continue
            changes[kind] = None if data == self._NONE else int(data)
        return self.role.currentData(), changes

    @classmethod
    def run(cls, parent: QWidget, role: str, marks: dict[str, list[int]]):
        dialog = cls(parent, role, marks)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.result_value()


def _parse_date_text(text: str) -> tuple[str, str] | None:
    """사람이 적은 날짜를 (YYYY-MM-DD, precision)으로 바꾼다.

    적는 방식을 하나로 강요하지 않는다 — 2024.3.11 / 2024-03-11 / 2024년 3월을
    모두 받는다. 못 알아들을 때는 지어내지 않고 None을 돌려 확인 버튼을 잠근다.

    빠진 자리는 01로 채우고 무엇이 확실한지는 precision이 말한다. 자료에서
    뽑은 후보와 같은 형식이어야 한다(core/dating.py).
    """
    parts = [p for p in re.split(r"[^0-9]+", text.strip()) if p]
    if not parts or len(parts) > 3:
        return None
    try:
        numbers = [int(p) for p in parts]
    except ValueError:                       # pragma: no cover — 숫자만 남겨 두었다
        return None

    year = numbers[0]
    if not 1900 <= year <= 2200:
        return None
    if len(numbers) == 1:
        return f"{year:04d}-01-01", "year"

    month = numbers[1]
    if not 1 <= month <= 12:
        return None
    if len(numbers) == 2:
        return f"{year:04d}-{month:02d}-01", "month"

    day = numbers[2]
    try:
        return date(year, month, day).isoformat(), "day"
    except ValueError:
        return None


def _value_text(value: str, precision: str) -> str:
    if precision == "year":
        return f"{value[:4]}년"
    if precision == "month":
        return f"{value[:4]}.{value[5:7]}"
    return value


def _elide_middle(text: str, limit: int = 46) -> str:
    if len(text) <= limit:
        return text
    head = (limit - 3) // 2
    return f"{text[:head]}…{text[-(limit - 3 - head):]}"


_MACHINE_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$")


def _plain_stamp(raw: str) -> str:
    """'2026-10-08T11:30:19.1522' → '2026-10-08 11:30'.

    문서 속성과 파일 수정일은 기계가 적은 시각이라 초 아래 자리와 T가 붙어 온다.
    근거로 보여 주되 사람이 읽는 꼴로 줄인다. 본문·파일명에서 읽은 글자
    (“작성일: 2025. 12. 12”)는 문서에 적힌 그대로가 근거이므로 손대지 않는다.
    """
    found = _MACHINE_STAMP.match((raw or "").strip())
    return f"{found.group(1)} {found.group(2)}" if found else (raw or "")


def _kind_order(kind: str) -> int:
    order = ["body", "filename", "meta", "folder", "fs"]
    return order.index(kind) if kind in order else len(order)


def _divider() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    line.setFixedHeight(1)
    return line


def _cell(text: str, doc_id: int | None = None, path: str | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if doc_id is not None:
        item.setData(Qt.ItemDataRole.UserRole, doc_id)
        item.setData(Qt.ItemDataRole.UserRole + 1, path)
    return item


def _when(row) -> str:
    """시점과 그 판정 출처를 함께 보여준다. 파일 수정일뿐이면 그렇다고 밝힌다."""
    if not row["eff_date"]:
        return "—"
    precision = row["eff_precision"] or "day"
    year, month, *_ = (row["eff_date"] or "").split("-") + ["", ""]
    if precision == "year":
        text = f"{year}년"
    elif precision == "month":
        text = f"{year}.{int(month):02d}"
    else:
        text = row["eff_date"]
    if row["eff_date_kind"] == "fs":
        text += " (파일 날짜)"
    return text


def _launch(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 — 사용자가 명시적으로 연 원본
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)}{unit}" if unit == "B" else f"{value:,.1f}{unit}"
        value /= 1024
    return f"{value:,.1f}GB"
