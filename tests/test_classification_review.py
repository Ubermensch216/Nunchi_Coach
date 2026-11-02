"""자료 분류 확인 대화상자(P3)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ai import classify_units as cu  # noqa: E402
from app.core import charter, units  # noqa: E402
from app.db import Database  # noqa: E402
from app.ui.views.classification_review import ClassificationReviewDialog  # noqa: E402
from app.ui.views.structure_review import StructureReviewDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    source = database.con.execute("INSERT INTO sources (path, kind) VALUES ('D:/전임자', 'local')").lastrowid
    charter.save_user_items(database, charter.parse_user_text("고객 응대 혁신\n예산 편성"))
    for rel in ("잡다/c.hwp", "잡다/d.hwp"):
        database.con.execute(
            "INSERT INTO documents (source_id, path, filename, ext, hash, parse_status) VALUES (?, ?, ?, '.hwp', ?, 'ok')",
            (source, f"D:/전임자/{rel}", rel.split("/")[1], rel))
    cu.classify(database)
    yield database
    database.close()


def test_dialog_lists_attention_rows_and_choosing_saves_user_decision(qapp, db):
    dialog = ClassificationReviewDialog(db)
    assert dialog.table.rowCount() == 1 and dialog.table.item(0, 0).text() == "잡다"
    assert dialog.summary.rowCount() == 4            # 항목 2 + 분장 밖 + 판단 불가
    dialog.choose(dialog.rows[0].unit_key, "예산 편성")
    assert dialog.changed and dialog.table.rowCount() == 0
    key = units.load(db)[0]["key"]
    assert cu.assignments(db)[key].decided_by == "user"
    dialog.choose(key, None)                          # 고르기 안내 항목은 아무것도 바꾸지 않는다
    assert cu.assignments(db)[key].item_name == "예산 편성"
    dialog.deleteLater()


def test_outside_choice_and_structure_review_button(qapp, db):
    dialog = ClassificationReviewDialog(db)
    dialog.choose(dialog.rows[0].unit_key, "")
    key = units.load(db)[0]["key"]
    assert cu.assignments(db)[key].bucket == cu.OUTSIDE
    review = StructureReviewDialog(db)
    assert review.classification_button.isVisibleTo(review)
    review.deleteLater()
    dialog.deleteLater()
