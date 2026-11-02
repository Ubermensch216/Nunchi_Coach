"""자료 분류 확인 대화상자 (P3).

자료를 사무분장 항목에 나눈 결과를 보여 준다. 위는 항목별 요약(자료가 없는 항목도 보인다),
아래는 사람이 봐야 할 단위 — 판단 불가, 분장 밖, 확신이 낮은 것, AI가 판단한 것이다.
단위의 분류를 고르면 그 폴더와 같은 제목의 공문은 다음 분석에서 같은 분류를 따른다.
고친 내용은 다음 분석부터 반영된다.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ...ai import classify_units, charter_match
from .. import theme
from ..widgets import muted_label, section_title, view_title

KIND_LABELS = {"letter": "공문", "folder": "폴더", "loose": "낱장"}
BUCKET_LABELS = {classify_units.OUTSIDE: "분장 밖", classify_units.UNKNOWN: "판단 불가"}
PLACEHOLDER = "분류 고르기"
OUTSIDE_CHOICE = "분장 밖"


class ClassificationReviewDialog(QDialog):
    """닫힐 때 changed가 참이면 분류가 바뀐 것이다(다음 분석이 반영한다)."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.changed = False
        self.setWindowTitle("자료 분류 확인")
        self.resize(860, 640)
        layout = QVBoxLayout(self)
        layout.setSpacing(theme.SP_MD)
        layout.addWidget(view_title("자료가 어느 업무로 나뉘었는지 확인하세요"))
        layout.addWidget(muted_label(
            "내가 적은 업무 분장 항목별로 자료를 나눈 결과입니다. 아래 목록은 사람이 봐야 할 것만 모았습니다. "
            "분류를 고르면 같은 폴더와 같은 제목의 공문도 다음 분석에서 따라갑니다. 고친 내용은 다음 분석부터 반영됩니다."))

        layout.addWidget(section_title("업무 분장 항목별"))
        self.summary = QTableWidget(0, 4)
        self.summary.setHorizontalHeaderLabels(("항목", "자료", "단위", "사람 지정 · AI 판단"))
        self._plain(self.summary)
        layout.addWidget(self.summary, 1)

        layout.addWidget(section_title("확인이 필요한 자료"))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(("자료", "종류", "문서", "지금 분류와 근거", "분류"))
        self._plain(self.table)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 2)

        foot = QHBoxLayout()
        foot.addStretch(1)
        close = QPushButton("닫기")
        close.setObjectName("Primary")
        close.clicked.connect(self.accept)
        foot.addWidget(close)
        layout.addLayout(foot)
        self.refresh()

    @staticmethod
    def _plain(table: QTableWidget) -> None:
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

    # ── 목록 ────────────────────────────────────────────────────────
    def refresh(self) -> None:
        items = [o["name"] for o in charter_match.charter_options(self.db)]
        summary = classify_units.summary_by_item(self.db)
        self.summary.setRowCount(len(summary))
        for index, row in enumerate(summary):
            self.summary.setItem(index, 0, QTableWidgetItem(row["name"]))
            docs = QTableWidgetItem(f"{row['docs']:,}건" if row["docs"] else "자료 없음")
            docs.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.summary.setItem(index, 1, docs)
            self.summary.setItem(index, 2, QTableWidgetItem(f"{row['units']:,}"))
            self.summary.setItem(index, 3, QTableWidgetItem(f"{row['user']} · {row['ai']}"))

        self.rows = classify_units.review_rows(self.db)
        self.table.setRowCount(len(self.rows))
        for index, row in enumerate(self.rows):
            title = QTableWidgetItem(row.label)
            title.setToolTip(row.sample or row.folder)
            self.table.setItem(index, 0, title)
            self.table.setItem(index, 1, QTableWidgetItem(KIND_LABELS.get(row.kind, row.kind)))
            count = QTableWidgetItem(f"{row.doc_count:,}건")
            count.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(index, 2, count)
            now = row.item_name if row.item_name else BUCKET_LABELS.get(row.bucket, row.bucket)
            detail = f"{now} · {row.basis}" if row.basis else now
            if row.confidence == "low" and row.item_name:
                detail += " (확신 낮음)"
            cell = QTableWidgetItem(detail)
            cell.setToolTip(row.evidence)
            self.table.setItem(index, 3, cell)
            self.table.setCellWidget(index, 4, self._chooser(row.unit_key, items))

    def _chooser(self, unit_key: str, items: list[str]) -> QComboBox:
        box = QComboBox()
        box.addItem(PLACEHOLDER, None)
        for name in items:
            box.addItem(name, name)
        box.addItem(OUTSIDE_CHOICE, "")
        box.setAccessibleName("자료 분류 고르기")
        box.activated.connect(lambda _index, b=box, k=unit_key: self.choose(k, b.currentData()))
        return box

    def choose(self, unit_key: str, item_name: str | None) -> None:
        """item_name이 None이면 아무것도 하지 않고, 빈 문자열이면 분장 밖으로 둔다."""
        if item_name is None:
            return
        classify_units.set_user_assignment(self.db, unit_key, item_name or None)
        self.changed = True
        self.refresh()
