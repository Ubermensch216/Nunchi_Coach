"""업무 체계 확인(S4) — 막지 않는 확인 게이트.

- 볼 업무가 있고 아직 확인·미루기를 하지 않았을 때만 묻는다
- 업무마다 세운 단서(분장·공문·폴더·본문·사람)와 문서 수를 보인다
- 이름 고치기·합치기·업무 아님은 업무 화면과 같은 교정으로 남고, 고친 업무는 다음 분석이 다시 본다
- 확정하면 사본 인계 폴더를 제안한다
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.core import structure_review  # noqa: E402
from app.db import Database  # noqa: E402
from app.jobs.pipeline import Pipeline  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    source = database.add_source(tmp_path)

    def task(name: str, docs: int, evidence: str) -> int:
        task_id = database.con.execute(
            "INSERT INTO tasks(name, origin, status) VALUES (?, 'ai', 'proposed')", (name,)).lastrowid
        for index in range(docs):
            doc_id = database.upsert_document(source, {
                "path": str(tmp_path / name / f"{index}.hwp"), "filename": f"{name}{index}.hwp",
                "ext": ".hwp", "hash": f"{name}{index}", "parse_status": "ok",
                "eff_year": 2024, "eff_month": 3, "eff_date": "2024-03-10",
                "eff_precision": "day", "eff_date_kind": "filename"})
            database.con.execute(
                "INSERT INTO task_docs(task_id, doc_id, origin, evidence) VALUES (?, ?, 'ai', ?)",
                (task_id, doc_id, evidence))
        return task_id

    database.ids = {
        "예산편성": task("예산편성", 3, "폴더 위치"),
        "적극행정": task("적극행정", 2, "온나라 공문 · 단위과제"),
        "수질통계": task("수질통계", 4, "본문 유사"),
        "예산 집행": task("예산 집행", 1, "본문 유사"),
    }
    database.set_meta("charter_comparison", json.dumps(
        {"matched": [{"name": "예산편성", "doc_count": 3}], "charter_only": [], "data_only": []},
        ensure_ascii=False))
    database.con.commit()
    yield database
    database.close()


def test_rows_show_basis_and_volume_largest_first(db):
    rows = structure_review.rows(db)
    assert [(r.name, r.basis, r.doc_count) for r in rows] == [
        ("수질통계", structure_review.CONTENT, 4),
        ("예산편성", structure_review.CHARTER, 3),
        ("적극행정", structure_review.OFFICIAL, 2),
        ("예산 집행", structure_review.CONTENT, 1),
    ]


def test_pending_until_confirmed_or_postponed(db):
    assert structure_review.pending(db)
    structure_review.later(db)
    assert not structure_review.pending(db)
    db.set_meta(structure_review.STATE_META, "")
    structure_review.confirm(db)
    assert not structure_review.pending(db)


def test_nothing_to_review_without_tasks(tmp_path: Path):
    empty = Database(tmp_path / "empty.db")
    empty.init()
    try:
        assert not structure_review.pending(empty)
    finally:
        empty.close()


def test_dialog_edits_are_the_same_corrections_and_mark_tasks_for_reanalysis(db, qapp):
    from app.ui.views.structure_review import StructureReviewDialog

    Pipeline(db.path)._analyze_by_tasks(db)
    # 분석은 확인보다 앞서 끝났다. 시각은 초 단위라 같은 초 안의 편집은 구별하지 못한다.
    db.con.execute("UPDATE task_analysis_state SET finished_at = datetime('now', '-1 minute')")
    pipe = Pipeline(db.path)
    assert not any(pipe._is_task_stale(db, task_id) for task_id in db.ids.values())

    dialog = StructureReviewDialog(db)
    try:
        assert dialog.rename(db.ids["수질통계"], "수질 통계 관리")
        assert dialog.merge(db.ids["예산 집행"], db.ids["예산편성"])
        dialog.not_a_task(db.ids["적극행정"])
        assert dialog.changed
        assert {r.name: r.doc_count for r in dialog.rows} == {"예산편성": 4, "수질 통계 관리": 4}
    finally:
        dialog.deleteLater()

    renamed = db.task(db.ids["수질통계"])
    assert renamed["origin"] == "user" and renamed["name"] == "수질 통계 관리"
    assert db.task(db.ids["적극행정"])["not_a_task"] == 1
    assert pipe._is_task_stale(db, db.ids["수질통계"])
    assert pipe._is_task_stale(db, db.ids["예산편성"])     # 합쳐 받은 문서가 늘었다


def test_confirm_offers_the_handover_folder(db, qapp, monkeypatch):
    from app.ui.views.structure_review import StructureReviewDialog

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dialog = StructureReviewDialog(db)
    asked = []
    dialog.handover_folder_requested.connect(lambda: asked.append(True))
    try:
        dialog.confirm_button.click()
    finally:
        dialog.deleteLater()
    assert asked == [True]
    assert structure_review.state(db) == structure_review.CONFIRMED
    assert not dialog.changed


def test_later_keeps_the_proposal_and_stops_asking(db, qapp):
    from app.ui.views.structure_review import StructureReviewDialog

    dialog = StructureReviewDialog(db)
    try:
        dialog.later_button.click()
    finally:
        dialog.deleteLater()
    assert structure_review.state(db) == structure_review.LATER
    assert len(structure_review.rows(db)) == 4


def test_progress_screen_offers_review_only_while_pending(db, qapp):
    from app.ui.views.initial_analysis import InitialAnalysisView

    view = InitialAnalysisView(db)
    try:
        assert not view.review.isHidden()
        structure_review.later(db)
        view.refresh()
        assert view.review.isHidden()
    finally:
        view.deleteLater()
