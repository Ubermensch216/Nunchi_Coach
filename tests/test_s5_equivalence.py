"""업무 단위 분석(S5)이 일괄 분석과 같은 결과를 내는가.

실행 경로는 업무 단위(_analyze_by_tasks)이고, 결과의 정답 시험(test_handover_e2e)은
일괄 단계(_roles → _cycles → _steps → _restore)로 돈다. 둘이 어긋나면 시험은 통과하는데
사용자는 다른 결과를 본다. 같은 표본을 두 길로 돌려 결과 표를 통째로 견준다.

예전에 어긋났던 두 가지
  · 업무를 하나씩 끝까지 돌려, 뒤 업무의 처리 단계가 아직 없을 때 위원회 자료에 인용된
    선발 절차가 위원회의 '적힌 절차'가 됐다(처리 순서에 따라 결과가 달라짐).
  · 앞 업무에서 만든 업무 정리본(업무노트)이 뒤 업무의 공백 조사에 근거 문서로 잡혔다.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ai.discover import discover  # noqa: E402
from app.core import continuity, reconstruction as rc  # noqa: E402
from app.db import Database  # noqa: E402
from app.jobs.pipeline import Pipeline  # noqa: E402
from test_handover_e2e import COMMITTEE, ROOT, TREE  # noqa: E402

RESULT_TABLES = ("task_cycles", "task_steps", "task_items", "work_steps", "evidence", "info_gaps")
# 행 번호와 시각은 실행마다 다르다. 내용만 견준다.
VOLATILE = {"id", "created_at", "updated_at", "extracted_at", "resolved_at", "context_id",
            "after_step_id", "task_step_id", "step_id", "item_id", "owner_id"}


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def discovered(tmp_path: Path) -> Path:
    """업무 파악까지 마친 프로젝트. 뒤 단계는 시험마다 따로 돈다."""
    db = Database(tmp_path / "base.db")
    db.init()
    source_id = db.add_source(ROOT)
    for relative, sections, when in TREE:
        name = relative.rsplit("\\", 1)[-1]
        doc_id = db.upsert_document(source_id, {
            "path": ROOT + "\\" + relative, "filename": name, "ext": Path(name).suffix.lower(),
            "parse_status": "ok", "hash": relative, "char_count": 500, "eff_year": int(when[:4]),
            "eff_month": int(when[5:7]), "eff_date": when, "eff_precision": "day",
            "eff_date_kind": "filename", "fs_mtime": when + " 10:00:00"})
        db.replace_sections(doc_id, [("paragraph", index, f"{index}문단", text, {})
                                     for index, text in enumerate(sections, start=1)])
        db.index_document(doc_id, name, ROOT + "\\" + relative, "", "\n".join(sections))
    discover(db, client=None)
    db.con.commit()
    db.close()
    return tmp_path / "base.db"


def _copy(base: Path, name: str) -> Database:
    target = base.with_name(name)
    shutil.copy(base, target)
    return Database(target)


def _results(path: Path) -> dict[str, list[str]]:
    con = sqlite3.connect(path)
    try:
        out = {}
        for table in RESULT_TABLES:
            columns = [row[1] for row in con.execute(f"PRAGMA table_info({table})")
                       if row[1] not in VOLATILE]
            out[table] = sorted(repr(row) for row in con.execute(
                f"SELECT {', '.join(columns)} FROM {table}"))
        return out
    finally:
        con.close()


def _batch(base: Path) -> Path:
    db = _copy(base, "batch.db")
    worker = Pipeline(db.path)
    for stage in ("_roles", "_cycles", "_steps", "_restore"):
        getattr(worker, stage)(db)
    db.con.commit()
    db.close()
    return base.with_name("batch.db")


def _by_tasks(base: Path) -> Path:
    db = _copy(base, "by_tasks.db")
    Pipeline(db.path)._analyze_by_tasks(db)
    db.con.commit()
    db.close()
    return base.with_name("by_tasks.db")


def test_by_tasks_matches_batch(discovered: Path):
    batch, by_tasks = _results(_batch(discovered)), _results(_by_tasks(discovered))

    for table in RESULT_TABLES:
        assert by_tasks[table] == batch[table], table
    assert batch["work_steps"], "표본이 비어 있으면 견준 것이 없다"


def test_by_tasks_does_not_depend_on_task_order(discovered: Path, monkeypatch):
    from app.jobs import pipeline

    forward = _results(_by_tasks(discovered))
    ordered = pipeline._order_tasks_for_analysis
    monkeypatch.setattr(pipeline, "_order_tasks_for_analysis", lambda db: ordered(db)[::-1])
    backward_path = discovered.with_name("by_tasks.db")
    backward_path.unlink()
    backward = _results(_by_tasks(discovered))

    assert backward == forward


def test_quoted_procedure_stays_off_the_committee_in_by_tasks(discovered: Path):
    db = Database(_by_tasks(discovered))
    try:
        committee = next(row["id"] for row in db.tasks() if row["name"] == COMMITTEE)
        assert not [s for s in db.work_steps(committee) if s["procedure_kind"] == "stated"]
    finally:
        db.close()


def test_research_ignores_uploaded_and_generated_documents(discovered: Path):
    db = Database(discovered)
    try:
        task = db.tasks()[0]
        upload = db.add_source(str(discovered.parent / "uploads"), kind="upload")
        doc_id = db.upsert_document(upload, {
            "path": str(discovered.parent / "uploads" / "업무 정리본.md"), "filename": "업무 정리본.md",
            "ext": ".md", "parse_status": "ok", "hash": "generated"})
        text = "제출처 : 행정안전부 정리본에만 있는 과"
        db.replace_sections(doc_id, [("paragraph", 1, "1문단", text, {})])
        db.index_document(doc_id, "업무 정리본.md", "", "", text)
        db.con.commit()

        found, _looked = continuity.research(db, task["id"], rc.RECIPIENT)
        assert doc_id not in {item.doc_id for item in found}
    finally:
        db.close()
