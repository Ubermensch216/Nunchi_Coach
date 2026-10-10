"""업무 복원·수행 데이터 계약 (C01).

완료 증거: 전임자 참여·인계본·검토자 없이 만들어지고, 기존 교정이 보존된다.
"""

import sqlite3
from pathlib import Path

import pytest

from app.core import status, workstate as ws
from app.db import Database
from app.db.migrations import SCHEMA_VERSION

NEW_TABLES = {"work_contexts", "task_items", "work_steps", "evidence",
              "info_gaps", "step_runs", "reviews", "snapshots",
              "snapshot_receipts"}


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    source = database.add_source(r"D:\자료")
    for index, name in enumerate(("요청공문.hwpx", "과거제출본.hwpx"), start=1):
        database.upsert_document(source, {
            "path": rf"D:\자료\{name}", "filename": name, "ext": ".hwpx",
            "parse_status": "ok", "hash": f"hash-{index}"})
    database.con.execute("INSERT INTO tasks(id, name) VALUES (1, '정기 실적 보고')")
    yield database
    database.close()


def deadline(content="2025-09-30", doc=1, **extra):
    return {"kind": "deadline", "slot": "제출기한", "content": content,
            "basis_state": ws.STATED, "applicability": ws.CURRENT,
            "evidence": [(doc, "표 1 · 2행", f"제출기한: {content}")], **extra}


def step(slot, label, ordinal, **extra):
    return {"slot": slot, "label": label, "ordinal": ordinal,
            "procedure_kind": ws.PROCEDURE_STATED, "basis_state": ws.STATED,
            "evidence": [(1, "3문단", label)], **extra}


# ── 전임자 없이 만들어진다 ──────────────────────────────────────────

def test_restoration_starts_and_fills_without_any_predecessor_input(db):
    context = db.start_work_context()
    row = db.work_context()
    assert (row["id"], row["mode"], row["predecessor"]) == (context, ws.MODE_MATERIALS, None)
    assert row["basis_at"]

    assert db.sync_task_items(1, [deadline()], context, extractor="rule@1")["added"] == 1
    db.sync_work_steps(1, [step("집계", "집계 자료 준비", 1), step("작성", "실적표 작성", 2)], context)

    item = db.task_items(1)[0]
    assert item["human_state"] == ws.NONE and item["origin"] == "auto"
    # 사람 확인이 없어도 근거가 있는 값으로 보인다 — 빈 화면이 아니다.
    assert item["display_state"] == status.INFERRED
    assert item["evidence"][0]["filename"] == "요청공문.hwpx" and item["evidence"][0]["current"]
    assert [s["label"] for s in db.work_steps(1)] == ["집계 자료 준비", "실적표 작성"]
    assert db.con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


def test_unknown_state_values_are_refused(db):
    with pytest.raises(ValueError):
        db.sync_task_items(1, [deadline(basis_state="확실함")])
    with pytest.raises(ValueError):
        db.start_work_context(mode="approval_required")
    assert db.task_items(1) == []


# ── 네 상태는 서로 독립이다 ─────────────────────────────────────────

def test_document_statement_and_human_confirmation_are_separate_facts(db):
    db.sync_task_items(1, [deadline(applicability=ws.PAST)])
    item = db.task_items(1)[0]
    db.review_task_item(item["id"], "confirm", reviewer_name="후임", reviewer_role=ws.SUCCESSOR)
    item = db.task_items(1)[0]
    # 확인했다고 과거 사례가 현재 기한으로 바뀌지 않는다.
    assert (item["basis_state"], item["applicability"], item["human_state"]) == (
        ws.STATED, ws.PAST, ws.SUCCESSOR)
    assert item["display_state"] == status.CONFIRMED
    assert ws.describe(item["basis_state"], item["applicability"], item["human_state"]) == (
        "문서에 명시 · 과거 사례 · 후임자 확인")


@pytest.mark.parametrize("basis,human,expected", [
    (ws.STATED, ws.NONE, status.INFERRED), (ws.INFERRED, ws.NONE, status.INFERRED),
    (ws.CONFLICT, ws.NONE, status.WEAK), (ws.MISSING, ws.NONE, status.UNKNOWN),
    (ws.INFERRED, ws.PREDECESSOR, status.CONFIRMED), (ws.STATED, ws.RECHECK, status.WEAK),
])
def test_states_fold_into_the_shared_four_levels(basis, human, expected):
    assert ws.display_state(basis, human) == expected


# ── 재분석은 사람의 흔적을 덮지 않는다 ──────────────────────────────

def test_reanalysis_updates_untouched_items_and_drops_vanished_ones(db):
    db.sync_task_items(1, [deadline(), {"kind": "amount", "slot": "요구액", "content": "820"}])
    result = db.sync_task_items(1, [deadline("2025-10-15", doc=2)])
    assert (result["updated"], result["removed"]) == (1, 1)
    item, = db.task_items(1)
    assert (item["content"], item["version"]) == ("2025-10-15", 2)
    assert [e["filename"] for e in item["evidence"]] == ["과거제출본.hwpx"]
    assert db.con.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1


def test_human_edit_survives_reanalysis_and_is_flagged_when_sources_change(db):
    db.sync_task_items(1, [deadline()])
    item_id = db.task_items(1)[0]["id"]
    db.review_task_item(item_id, "edit", "2025-10-02 (연장 통보)", "전임", ws.PREDECESSOR)

    assert db.sync_task_items(1, [deadline()])["unchanged"] == 1
    assert db.task_items(1)[0]["human_state"] == ws.PREDECESSOR

    assert db.sync_task_items(1, [deadline("2025-10-15")])["recheck"] == 1
    item = db.task_items(1)[0]
    assert item["content"] == "2025-10-02 (연장 통보)", "사람이 고친 값을 덮었다"
    assert (item["auto_content"], item["human_state"]) == ("2025-10-15", ws.RECHECK)
    history = db.reviews("item", item_id)
    assert [(r["action"], r["before_val"], r["reviewer_role"]) for r in history] == [
        ("edit", "2025-09-30", ws.PREDECESSOR)]


def test_confirmed_item_that_disappears_from_sources_is_kept_for_recheck(db):
    db.sync_task_items(1, [deadline()])
    db.review_task_item(db.task_items(1)[0]["id"], "confirm")
    assert db.sync_task_items(1, [])["recheck"] == 1
    item, = db.task_items(1)
    assert (item["content"], item["basis_state"], item["human_state"]) == (
        "2025-09-30", ws.MISSING, ws.RECHECK)


def test_excluded_and_user_added_items_are_not_resurrected_or_removed(db):
    db.sync_task_items(1, [deadline()])
    db.review_task_item(db.task_items(1)[0]["id"], "exclude")
    added = db.add_task_item(1, "contact", "예산팀 내선 1234 — 9월 초 사전 협의", reviewer_name="후임")
    db.sync_task_items(1, [deadline()])
    visible = db.task_items(1)
    assert [i["id"] for i in visible] == [added]
    assert visible[0]["basis_state"] == ws.MISSING and visible[0]["origin"] == "user"
    assert len(db.task_items(1, include_excluded=True)) == 2


def test_evidence_from_a_changed_or_missing_original_is_not_current(db):
    db.sync_task_items(1, [deadline()])
    db.update_document(1, hash="hash-after-edit")
    assert db.task_items(1)[0]["evidence"][0]["current"] is False
    db.update_document(1, hash="hash-1")
    assert db.task_items(1)[0]["evidence"][0]["current"] is True
    db.mark_missing([1])
    assert db.task_items(1)[0]["evidence"][0]["current"] is False


# ── 공백은 자기 단계만 막는다 ───────────────────────────────────────

def test_a_gap_blocks_only_the_step_it_points_at(db):
    db.sync_work_steps(1, [step("집계", "집계 자료 준비", 1), step("제출", "검토 후 제출", 2)])
    first, second = db.work_steps(1)
    gap = db.add_info_gap(1, "missing", "제출처", "제출 대상 부서가 자료에 없다",
                          step_id=second["id"], next_action="요청 공문의 발신 부서 확인")
    first, second = db.work_steps(1)
    assert (first["blocked"], second["blocked"]) == (False, True)
    assert db.info_gaps(1)[0]["next_action"] == "요청 공문의 발신 부서 확인"

    db.resolve_info_gap(gap, "요청 공문 발신: 기획예산과")
    assert not db.work_steps(1)[1]["blocked"] and db.info_gaps(1) == []
    assert db.info_gaps(1, state=None)[0]["resolution"] == "요청 공문 발신: 기획예산과"


# ── 수행 기록 ───────────────────────────────────────────────────────

def test_execution_is_recorded_only_when_the_user_says_so_and_survives_reanalysis(db):
    db.sync_work_steps(1, [step("집계", "집계 자료 준비", 1), step("작성", "실적표 작성", 2)])
    first, second = db.work_steps(1)
    assert db.work_progress(1) == {"not_started": 2, "in_progress": 0, "done": 0,
                                   "waiting": 0, "total": 2}

    db.set_step_run(first["id"], ws.IN_PROGRESS)
    db.set_step_run(first["id"], ws.DONE, result_path="outputs/집계.xlsx", note="9월분 취합")
    with pytest.raises(ValueError):
        db.set_step_run(first["id"], "읽음")

    # 재분석이 단계 이름을 바꾸거나 없애도 수행한 단계는 남는다.
    result = db.sync_work_steps(1, [step("집계", "집계표 갱신", 1)])
    assert (result["recheck"], result["removed"]) == (1, 1)
    kept, = db.work_steps(1)
    assert (kept["label"], kept["auto_label"], kept["run_state"]) == (
        "집계 자료 준비", "집계표 갱신", ws.DONE)
    assert kept["result_path"] == "outputs/집계.xlsx" and kept["finished_at"]
    assert db.work_progress(1)["done"] == 1


def test_reopening_a_done_step_keeps_when_it_was_started(db):
    db.sync_work_steps(1, [step("집계", "집계 자료 준비", 1)])
    step_id = db.work_steps(1)[0]["id"]
    db.set_step_run(step_id, ws.DONE)
    started = db.work_steps(1)[0]["started_at"]
    db.set_step_run(step_id, ws.WAITING, note="제출처 확인 중")
    row = db.work_steps(1)[0]
    assert (row["run_state"], row["finished_at"], row["started_at"]) == (ws.WAITING, None, started)


# ── 기존 프로젝트 이행 ──────────────────────────────────────────────

def test_v4_project_gains_the_tables_and_keeps_corrections(tmp_path):
    path = tmp_path / "old" / "project.db"
    old = Database(path)
    old.init()
    source = old.add_source(r"D:\자료")
    old.upsert_document(source, {"path": r"D:\자료\a.hwp", "filename": "a.hwp", "ext": ".hwp"})
    old.con.execute("INSERT INTO tasks(id, name) VALUES (1, '계약관리')")
    old.confirm_task(1)
    old.set_document_date(1, "2024-07-02", "day")
    # v4 시절로 되돌린다: 새 표가 없고 버전이 4다.
    for table in NEW_TABLES:
        old.con.execute(f"DROP TABLE {table}")
    old.set_meta("schema_version", "4")
    old.close()

    db = Database(path)
    db.init()
    try:
        assert db.get_meta("schema_version") == SCHEMA_VERSION == "10"
        tables = {r[0] for r in db.con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert NEW_TABLES <= tables
        assert db.task(1)["review_state"] == "confirmed"
        assert db.document(1)["eff_date"] == "2024-07-02"
        assert db.con.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] >= 2
        assert path.with_name("project.db.bak-v4").exists()
        db.sync_task_items(1, [deadline(doc=1)])
        assert db.task_items(1)[0]["content"] == "2025-09-30"
    finally:
        db.close()


def test_deleting_a_task_takes_its_restoration_with_it_but_not_the_documents(db):
    db.sync_task_items(1, [deadline()])
    db.sync_work_steps(1, [step("집계", "집계 자료 준비", 1)])
    db.set_step_run(db.work_steps(1)[0]["id"], ws.DONE)
    db.con.execute("DELETE FROM tasks WHERE id = 1")
    for table in ("task_items", "work_steps", "step_runs", "evidence"):
        assert db.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert db.con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError):
        db.con.execute("INSERT INTO task_items(task_id, kind) VALUES (999, 'deadline')")
