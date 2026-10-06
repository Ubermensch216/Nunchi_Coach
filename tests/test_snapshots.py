"""업무 복원본 스냅샷과 그 글 (C06).

완료 증거: 전임자 확인 없이 저장·열람하고, 근거/적용 시점/사람 확인 상태가 유지된다.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core import handover_release as release  # noqa: E402
from app.core import reconstruction as rc, workcard, workstate as ws  # noqa: E402
from app.core.output_policy import UnsafeOutputError  # noqa: E402
from app.db import Database  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.snapshot_dialog import SnapshotDialog  # noqa: E402

BASIS = date(2025, 9, 10)
REQUEST = ("절차: 집계 자료 준비 → 실적표 작성 → 검토 후 제출\n"
           "제출처: 기획예산과\n제출기한: 2025-09-30\n구비서류: 부서별 집계 자료")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet())
    yield app


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "project" / "project.db")
    database.init()
    database.con.execute("INSERT INTO tasks(id, name) VALUES (1, '정기 실적보고')")
    root = tmp_path / "자료"
    (root / "2025").mkdir(parents=True)
    source = database.add_source(root)
    for name, when, text in (("2025/2025_실적보고_요청.hwpx", "2025-09-01", REQUEST),
                             ("2025/메모.hwpx", "2025-09-05", "제출기한: 2025-09-26")):
        path = root / name
        path.write_text(text, encoding="utf-8")
        doc = database.upsert_document(source, {
            "path": str(path), "filename": path.name, "ext": ".hwpx", "parse_status": "ok",
            "hash": f"hash-{path.name}", "size": len(text), "char_count": len(text),
            "eff_date": when, "eff_year": 2025, "eff_month": 9, "eff_precision": "day",
            "eff_date_kind": "body"})
        database.replace_sections(doc, [("paragraph", 1, "1문단", text)])
        database.index_document(doc, path.name, str(path), "", text)
        database.assign_document(1, doc)
    rc.restore(database, BASIS)
    yield database
    database.close()


def item(payload, kind):
    return next(i for i in payload["tasks"][0]["items"] if i["kind"] == kind)


# ── 저장 ────────────────────────────────────────────────────────────

def test_restoration_is_saved_without_any_human_confirmation(db):
    snapshot_id, created = release.save(db)
    assert created
    row = db.snapshot(snapshot_id)
    assert row["kind"] == ws.SNAPSHOT_RESTORATION
    summary = json.loads(row["summary"])
    assert summary["tasks"] == 1 and summary["items"] >= 3
    # 저장했다고 확인한 것이 되지 않는다.
    assert summary["items_confirmed"] == 0
    assert db.con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0

    payload = release.load(db, snapshot_id)
    assert payload["context"] == {"mode": ws.MODE_MATERIALS, "predecessor": None}
    recipient = item(payload, rc.RECIPIENT)
    assert (recipient["basis_state"], recipient["applicability"], recipient["human_state"]) == (
        ws.STATED, ws.UNKNOWN, ws.NONE)
    assert recipient["evidence"][0]["quote"] == "제출처: 기획예산과"
    assert item(payload, rc.DEADLINE)["basis_state"] == ws.CONFLICT
    assert [s["label"] for s in payload["tasks"][0]["steps"]][:3] == [
        "집계 자료 준비", "실적표 작성", "검토 후 제출"]
    assert "conflict" in {gap["kind"] for gap in payload["tasks"][0]["gaps"]}


def test_snapshot_lists_evidence_documents_by_relative_path_and_hash(db, tmp_path):
    payload = release.build_payload(db)
    paths = {doc["filename"]: doc for doc in payload["documents"]}
    request = paths["2025_실적보고_요청.hwpx"]
    assert request["relative_path"] == "2025/2025_실적보고_요청.hwpx"
    assert request["hash"] == "hash-2025_실적보고_요청.hwpx" and request["source_kind"] == "local"
    # 개인 PC의 절대 경로가 실리지 않는다.
    assert str(tmp_path) not in json.dumps(payload, ensure_ascii=False)


def test_identical_content_is_not_saved_twice_but_any_change_makes_a_new_version(db):
    first, _ = release.save(db)
    again, created = release.save(db)
    assert (again, created) == (first, False)
    assert len(db.snapshots()) == 1

    deadline = next(i for i in db.task_items(1) if i["kind"] == rc.DEADLINE)
    db.review_task_item(deadline["id"], "edit", "2025-09-30 (시행공문 기준)", "후임")
    second, created = release.save(db)
    assert created and second != first

    step = workcard.build(db, 1).stated_steps[0]
    db.set_step_run(step["id"], ws.DONE, note="9월분 취합")
    third, created = release.save(db)
    assert created and [row["id"] for row in db.snapshots()] == [third, second, first]


def test_saved_snapshot_never_changes_afterwards(db):
    first, _ = release.save(db)
    before = db.snapshot(first)["payload"]
    deadline = next(i for i in db.task_items(1) if i["kind"] == rc.DEADLINE)
    db.review_task_item(deadline["id"], "edit", "2025-10-15", "후임")
    db.con.execute("DELETE FROM documents WHERE filename = '메모.hwpx'")
    rc.restore(db, BASIS)
    release.save(db)

    assert db.snapshot(first)["payload"] == before
    old = release.load(db, first)
    assert item(old, rc.DEADLINE)["content"] == "2025-09-30 / 2025-09-26"
    assert item(old, rc.DEADLINE)["human_state"] == ws.NONE
    # 그 뒤에 지워진 문서도 그때의 근거 목록에는 남아 있다.
    assert "메모.hwpx" in {doc["filename"] for doc in old["documents"]}


def test_human_confirmation_and_execution_are_carried_as_they_are(db):
    recipient = next(i for i in db.task_items(1) if i["kind"] == rc.RECIPIENT)
    db.review_task_item(recipient["id"], "confirm", reviewer_name="후임")
    step = workcard.build(db, 1).stated_steps[0]
    db.set_step_run(step["id"], ws.DONE, result_path="outputs/집계.xlsx")
    gap = next(g for g in db.info_gaps(1) if g["kind"] == "conflict")
    db.resolve_info_gap(gap["id"], "시행공문 기준 9/30으로 확인")

    snapshot_id, _ = release.save(db)
    payload = release.load(db, snapshot_id)
    summary = json.loads(db.snapshot(snapshot_id)["summary"])
    assert item(payload, rc.RECIPIENT)["human_state"] == ws.SUCCESSOR
    assert summary["items_confirmed"] == 1 and summary["steps_done"] == 1
    assert summary["gaps_resolved"] == 1
    first = payload["tasks"][0]["steps"][0]
    assert (first["run_state"], first["result_path"]) == (ws.DONE, "outputs/집계.xlsx")


def test_autosave_skips_projects_with_nothing_restored(tmp_path):
    empty = Database(tmp_path / "empty.db")
    empty.init()
    empty.con.execute("INSERT INTO tasks(id, name) VALUES (1, '빈 업무')")
    assert release.autosave(empty) is None and empty.snapshots() == []
    empty.close()


def test_analysis_stage_saves_a_snapshot_once_per_distinct_result(db):
    from app.jobs.pipeline import Pipeline

    reports = []
    worker = Pipeline(db.path)
    worker.stage_done.connect(reports.append)
    worker._restore(db)
    assert reports[-1].note.endswith("복원본 저장") and len(db.snapshots()) == 1
    assert db.snapshots()[0]["label"] == "자동 저장"
    worker._restore(db)
    assert "복원본 저장" not in reports[-1].note and len(db.snapshots()) == 1


# ── 글 ──────────────────────────────────────────────────────────────

def test_markdown_states_what_is_and_is_not_confirmed(db):
    recipient = next(i for i in db.task_items(1) if i["kind"] == rc.RECIPIENT)
    db.review_task_item(recipient["id"], "confirm")
    snapshot_id, _ = release.save(db, basis_at="2025-09-10T09:00:00")
    text = release.to_markdown(release.load(db, snapshot_id))

    assert text.startswith("# 자료 기반 업무 복원본")
    assert "- 기준 시각: 2025-09-10 09:00:00" in text and "- 시작 방식: 자료만으로 시작" in text
    assert "나머지는 자료에서 자동으로 읽은 값이며 사람이 확인하지 않았습니다." in text
    assert "- 사람이 확인한 항목: 1개 /" in text
    assert "| 제출처 | 기획예산과 | 문서에 명시 | 적용 여부 미상 | 후임자 확인 | [" in text
    assert "(엇갈림) 2025-09-30 / 2025-09-26 | 자료가 서로 다름 | 적용 여부 미상 | 사람 확인 없음" in text
    assert "**처리 순서**" in text and "| 1 | 집계 자료 준비 |" in text
    assert "**확인이 필요한 정보**" in text
    assert "2025_실적보고_요청.hwpx · 1문단 — “제출처: 기획예산과”" in text
    assert "| 2025_실적보고_요청.hwpx | 2025/2025_실적보고_요청.hwpx | 개인 폴더 자료 | hash-2025_실" in text
    assert "공식 인계서나 결재 문서가 아닙니다." in text


def test_markdown_comes_from_the_snapshot_not_from_the_live_project(db):
    first, _ = release.save(db)
    text_then = release.to_markdown(release.load(db, first))
    deadline = next(i for i in db.task_items(1) if i["kind"] == rc.DEADLINE)
    db.review_task_item(deadline["id"], "edit", "2025-10-15", "후임")
    assert release.to_markdown(release.load(db, first)) == text_then
    assert "2025-10-15" not in text_then


# ── 화면 ────────────────────────────────────────────────────────────

def test_dialog_lists_saves_and_exports_a_chosen_version(db, qapp):
    dialog = SnapshotDialog(db)
    assert dialog.list.count() == 0 and "아직 저장한 복원본이 없습니다" in dialog.detail.text()
    assert not dialog.export_button.isEnabled()

    first, created = dialog.save_now()
    assert created and dialog.list.count() == 1 and dialog.selected_id() == first
    assert "사람이 확인하지 않은 항목" in dialog.detail.text()
    _same, created = dialog.save_now()
    assert not created and "달라진 것이 없어" in dialog.detail.text()

    deadline = next(i for i in db.task_items(1) if i["kind"] == rc.DEADLINE)
    db.review_task_item(deadline["id"], "edit", "2025-10-15", "후임")
    second, _ = dialog.save_now()
    assert dialog.list.count() == 2 and dialog.selected_id() == second

    # 예전 판을 골라 내보내면 예전 내용이 나온다.
    target = workcard.default_output_dir(db) / "복원본.md"
    assert dialog.export(first, str(target)) > 0
    saved = target.read_text(encoding="utf-8-sig")
    assert "2025-09-30 / 2025-09-26" in saved and "2025-10-15" not in saved

    original = Path(db.document(1)["path"])
    with pytest.raises(UnsafeOutputError):
        dialog.export(first, str(original))
    assert original.read_text(encoding="utf-8") == REQUEST
    dialog.deleteLater()
