"""건 확인·고치기 화면 — 담당자가 건의 진행을 정하고, 건이 아닌 행을 빼고, 우리 기관을 정한다.

자료가 말하는 진행(목록·취합·회신)과 사람이 정한 진행을 나란히 두고, 사람이 정한 것은 다시
분석해도 남는지 본다. 기관·과제 이름은 지어낸 것이다.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QLineEdit, QPushButton  # noqa: E402

from app.core import continuity, matters, reconstruction as rc, task_manual  # noqa: E402
from app.db import Database  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.views.matter_dialog import MatterDialog  # noqa: E402

from tests.test_matters import _three_lists  # noqa: E402

BASIS = date(2026, 10, 14)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet())
    yield app


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    database.con.execute("INSERT INTO tasks(id, name) VALUES (1, '규제혁신 과제 제출')")
    yield database
    database.close()


def _texts(dialog):
    return [w.text() for w in dialog.findChildren(QLabel) if w.text()]


def test_without_our_locality_the_manual_says_why_there_are_no_matters(db, tmp_path):
    _three_lists(db, tmp_path / "자료")
    rc.restore(db, BASIS, statements=False)
    cases = next(s for s in task_manual.build(db, 1).sections if s.key == "cases")
    assert cases.lines[0].startswith("우리 기관을 정하지 못해 표 계산 문서 4건의 건 목록을 읽지 않았습니다")


def test_choosing_our_locality_reads_the_lists(db, tmp_path, qapp):
    _three_lists(db, tmp_path / "자료")
    rc.restore(db, BASIS, statements=False)
    dialog = MatterDialog(None, db, 1)
    try:
        assert any("정하지 못함" in t for t in _texts(dialog))
        totals = dialog.apply_place("다라구")
        assert totals["matters"] == 2 and dialog.changed
        assert matters.own_locality_basis(db) == ("다라구", "chosen")
        assert any("토지 경계조정 간소화" in t for t in _texts(dialog))
        assert any(t.startswith("자료: 회신(검토의견) 있음") for t in _texts(dialog))
    finally:
        dialog.deleteLater()


def test_a_decision_and_a_note_survive_reanalysis(db, tmp_path, qapp):
    matters.set_own_locality(db, "다라구")
    _three_lists(db, tmp_path / "자료")
    rc.restore(db, BASIS, statements=False)
    dialog = MatterDialog(None, db, 1)
    try:
        combo = next(c for c in dialog.findChildren(QComboBox) if "공원 내 홍보관" in c.accessibleName())
        combo.setCurrentIndex(combo.findData("submitted"))
        note = next(e for e in dialog.findChildren(QLineEdit) if "공원 내 홍보관" in e.accessibleName())
        note.setText("시행 기획실-123 제출")
        note.editingFinished.emit()
    finally:
        dialog.deleteLater()
    rc.restore(db, BASIS, statements=False)          # 다시 분석해도
    park = next(m for m in matters.for_task(db, 1) if "홍보관" in m["title"])
    assert park["human_stage"] == "submitted" and park["human_note"] == "시행 기획실-123 제출"
    assert "담당자: 제출 확인" in matters.progress_line(park)
    # 사람이 진행을 정한 건은 '[건별 진행 확인]'으로 다시 묻지 않는다.
    asked = [a["text"] for a in continuity.next_actions(db, 1) if a["text"].startswith("[건별 진행 확인]")]
    assert not any("홍보관" in text for text in asked)

    dialog = MatterDialog(None, db, 1)
    try:
        combo = next(c for c in dialog.findChildren(QComboBox) if "공원 내 홍보관" in c.accessibleName())
        combo.setCurrentIndex(combo.findData(""))      # 정한 것을 지우면 자료대로
    finally:
        dialog.deleteLater()
    assert db.con.execute("SELECT human_stage FROM matters WHERE title LIKE '%홍보관%'").fetchone()[0] is None


def test_a_row_that_is_not_a_matter_can_be_dropped_and_restored(db, tmp_path, qapp):
    matters.set_own_locality(db, "다라구")
    _three_lists(db, tmp_path / "자료")
    rc.restore(db, BASIS, statements=False)
    dialog = MatterDialog(None, db, 1)
    try:
        drop = next(b for b in dialog.findChildren(QPushButton) if b.text() == "건이 아님")
        drop.click()
        assert any(t.startswith("건이 아니라고 뺀 것 1건") for t in _texts(dialog))
        assert len(matters.for_task(db, 1)) == 1
        rc.restore(db, BASIS, statements=False)
        assert len(matters.for_task(db, 1)) == 1         # 다시 분석해도 빠져 있다
        back = next(b for b in dialog.findChildren(QPushButton) if b.text() == "되돌리기")
        back.click()
        assert len(matters.for_task(db, 1)) == 2
    finally:
        dialog.deleteLater()


def test_the_manual_offers_the_review_only_where_there_are_matters(db, tmp_path, qapp):
    from app.ui.views.tasks import TasksView

    db.con.execute("INSERT INTO tasks(id, name) VALUES (2, '다른 업무')")
    matters.set_own_locality(db, "다라구")
    _three_lists(db, tmp_path / "자료")
    rc.restore(db, BASIS, statements=False)
    view = TasksView(db)
    try:
        view.open_task(1, "guide")
        assert any(b.text() == "건 확인·고치기" for b in view.pages["guide"].findChildren(QPushButton))
        view.open_task(2, "guide")
        assert not any(b.text() == "건 확인·고치기" for b in view.pages["guide"].findChildren(QPushButton))
    finally:
        view.setParent(None)
