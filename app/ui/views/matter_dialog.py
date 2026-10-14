"""건 확인·고치기 — 업무 안의 건을 담당자가 확인하고 고친다 (규제개혁 개선계획서 P1-1 남은 일).

자료는 '목록에 있음 → 취합 목록에 있음 → 회신 있음'까지만 말한다. 제출했는지, 수용됐는지,
개선이 시행됐는지는 담당자가 공문·시스템으로 확인해 정한다. 여기서 정한 것은
  · 다시 분석해도 남는다(matters.human_stage·human_note·excluded).
  · 자료로 확인한 진행을 지우지 않는다 — 둘을 나란히 보인다.
  · 실행 승인이 아니다. 이 앱 안의 확인 기록이다.

우리 기관을 정하지 못하면 남의 건을 우리 건으로 만들지 않으려고 목록을 읽지 않는다. 여기서
기관을 정하면 건 목록을 다시 읽는다.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from ...core import matter_trail, matters
from .. import theme
from ..widgets import Card, clear_layout, muted_label, section_title

_AS_READ = ""        # 콤보의 '자료대로' — 사람이 정하지 않음


class MatterDialog(QDialog):
    def __init__(self, parent: QWidget, db, task_id: int, show_evidence=None) -> None:
        super().__init__(parent)
        self.db, self.task_id, self._show_evidence = db, task_id, show_evidence
        self.changed = False
        self.setWindowTitle("건 확인·고치기")
        self.setMinimumSize(620, 560)
        column = QVBoxLayout(self)
        column.setSpacing(theme.SP_MD)
        column.addWidget(muted_label(
            "자료로 확인한 진행과 담당자가 정한 진행을 나란히 적습니다. 정한 것은 다시 분석해도 남으며, "
            "실행 승인이 아니라 이 앱 안의 확인 기록입니다."))
        self.place = QHBoxLayout()
        column.addLayout(self.place)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        self.rows = QVBoxLayout(holder)
        self.rows.setSpacing(theme.SP_SM)
        scroll.setWidget(holder)
        column.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        column.addWidget(buttons)
        self.reload()

    # ── 그리기 ──────────────────────────────────────────────────────
    def reload(self) -> None:
        clear_layout(self.place)
        place, basis = matters.own_locality_basis(self.db)
        said = {"chosen": "담당자가 정함", "inferred": "파일 이름에서 추정"}.get(basis, "")
        label = QLabel(f"우리 기관: {place} ({said})" if place else "우리 기관: 정하지 못함 — 건 목록을 읽지 않았습니다")
        label.setObjectName("MetaChip")
        self.place.addWidget(label)
        self.place.addStretch(1)
        change = QPushButton("우리 기관 정하기" if not place else "바꾸기")
        change.clicked.connect(self._choose_place)
        self.place.addWidget(change)

        clear_layout(self.rows)
        found = matters.for_task(self.db, self.task_id, include_excluded=True)
        kept = [m for m in found if not m["excluded"]]
        dropped = [m for m in found if m["excluded"]]
        if not found:
            self.rows.addWidget(muted_label(
                "이 업무의 자료에서 우리 기관의 건을 찾지 못했습니다." if place else
                "우리 기관(시·군·구 이름)을 정하면 이 업무의 표 계산 문서에서 건 목록을 읽습니다."))
        for matter in kept:
            self.rows.addWidget(self._row(matter))
        if dropped:
            self.rows.addWidget(section_title(f"건이 아니라고 뺀 것 {len(dropped)}건"))
            for matter in dropped:
                self.rows.addWidget(self._dropped_row(matter))
        self.rows.addStretch(1)

    def _row(self, matter: dict) -> Card:
        card = Card(tone="static")
        title = QLabel(matters.describe(matter))
        title.setWordWrap(True)
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        card.body.addWidget(title)
        card.body.addWidget(muted_label("자료: " + matters.progress_line({**matter, "human_stage": None}), small=True))
        trail = matter.get("trail") or []
        told = [t for t in trail if t["signal"] != matter_trail.MENTIONED]
        for mention in told[:4]:
            card.body.addWidget(muted_label("· " + matter_trail.describe(mention), small=True))
        quiet = len(trail) - len(told)
        if quiet:
            card.body.addWidget(muted_label(f"· 진행을 말하지 않고 제목만 나온 문서 {quiet}곳(계획·실적·증빙 등)", small=True))
        line = QHBoxLayout()
        line.addWidget(QLabel("담당자 확인"))
        stage = QComboBox()
        stage.setAccessibleName(f"{matter['title']} 진행 확인")
        stage.addItem("정하지 않음(자료대로)", _AS_READ)
        for key in matters.HUMAN_STAGES:
            stage.addItem(matters.HUMAN_STAGE_LABEL[key], key)
        stage.setCurrentIndex(max(0, stage.findData(matter.get("human_stage") or _AS_READ)))
        stage.currentIndexChanged.connect(
            lambda _=0, m=matter["id"], box=stage: self._review(m, stage=box.currentData()))
        line.addWidget(stage, 1)
        if matter["evidence"] and self._show_evidence:
            proof = QPushButton(f"근거 {len(matter['evidence'])}")
            proof.clicked.connect(lambda _=False, m=matter: self._show_evidence(m["evidence"], m["title"] + "의 근거"))
            line.addWidget(proof)
        drop = QPushButton("건이 아님")
        drop.setToolTip("목록의 머리글·예시 행처럼 건이 아닌 것을 뺍니다. 다시 분석해도 빠져 있습니다.")
        drop.clicked.connect(lambda _=False, m=matter["id"]: self._review(m, excluded=True, reload=True))
        line.addWidget(drop)
        card.body.addLayout(line)
        note = QLineEdit(matter.get("human_note") or "")
        note.setPlaceholderText("메모(공문 번호, 확인한 곳 등)")
        note.setAccessibleName(f"{matter['title']} 메모")
        note.editingFinished.connect(
            lambda m=matter["id"], box=note: self._review(m, note=box.text().strip()))
        card.body.addWidget(note)
        return card

    def _dropped_row(self, matter: dict) -> QWidget:
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        text = muted_label(matters.describe(matter), small=True)
        line.addWidget(text, 1)
        back = QPushButton("되돌리기")
        back.clicked.connect(lambda _=False, m=matter["id"]: self._review(m, excluded=False, reload=True))
        line.addWidget(back)
        return row

    # ── 고치기 ──────────────────────────────────────────────────────
    def _review(self, matter_id: int, stage=None, note=None, excluded=None, reload: bool = False) -> None:
        current = self.db.con.execute("SELECT human_stage, human_note FROM matters WHERE id = ?",
                                      (matter_id,)).fetchone()
        if current is None:
            return
        if note is not None and note == (current["human_note"] or ""):
            return          # 칸을 지나가기만 했다 — 기록하지 않는다
        if stage is not None and (stage or None) == current["human_stage"]:
            return
        matters.review(self.db, matter_id, stage=stage, note=note, excluded=excluded)
        self.changed = True
        if reload:
            self.reload()

    def _choose_place(self) -> None:
        place, _ = matters.own_locality_basis(self.db)
        text, ok = QInputDialog.getText(
            self, "우리 기관", "이 자료의 주인인 기관(시·군·구 이름, 예: 가나구)", text=place)
        text = " ".join((text or "").split())
        if not ok or not text or text == place:
            return
        self.apply_place(text)

    def apply_place(self, place: str) -> dict:
        """기관을 정하고 건 목록을 다시 읽는다. 원본은 해시를 확인한 뒤 읽기만 한다."""
        matters.set_own_locality(self.db, place)
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            totals = matters.rebuild(self.db)
            matter_trail.link(self.db)
        finally:
            QGuiApplication.restoreOverrideCursor()
        self.changed = True
        self.reload()
        return totals
