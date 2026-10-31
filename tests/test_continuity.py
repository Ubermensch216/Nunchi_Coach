"""정보 공백 처리 (C04).

완료 증거: 한 단계의 미상 때문에 다른 단계가 막히지 않고, 공백마다 구체적인
다음 행동이 있다. '전임자에게 문의'만 내놓으면 실패다.
"""

from datetime import date
from pathlib import Path

import pytest

from app.core import continuity, reconstruction as rc, workstate as ws
from app.db import Database

BASIS = date(2025, 9, 10)


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    database.con.execute("INSERT INTO tasks(id, name) VALUES (1, '정기 실적보고')")
    database.con.execute("INSERT INTO tasks(id, name) VALUES (2, '계약관리')")
    yield database
    database.close()


def add(db, name, when, text="", task=1):
    source = db.add_source(r"D:\자료")
    doc = db.upsert_document(source, {
        "path": rf"D:\자료\{name}", "filename": name, "ext": ".hwpx", "parse_status": "ok",
        "hash": name, "char_count": len(text),
        "eff_date": when, "eff_year": int(when[:4]), "eff_month": int(when[5:7]),
        "eff_precision": "day", "eff_date_kind": "body"})
    if text:
        db.replace_sections(doc, [("paragraph", 1, "1문단", text)])
    db.index_document(doc, name, name, "", text)
    if task:
        db.assign_document(task, doc)
    return doc


def gaps(db, task=1):
    return {gap["gap_key"]: gap for gap in db.info_gaps(task)}


def stated(db):
    return [s for s in db.work_steps(1) if s["procedure_kind"] == ws.PROCEDURE_STATED]


# ── 공백은 자기 단계만 막는다 ───────────────────────────────────────

def test_unknown_deadline_blocks_only_the_submitting_step(db):
    add(db, "2025_요청.hwpx", "2025-09-01", "절차: 집계 자료 준비 → 실적표 작성 → 검토 후 제출")
    rc.restore(db, BASIS)
    first, second, third = stated(db)
    assert (first["blocked"], second["blocked"], third["blocked"]) == (False, False, True)
    found = gaps(db)
    assert found["missing:deadline"]["step_id"] == third["id"]
    assert found["missing:recipient"]["step_id"] == third["id"]
    assert "missing:procedure" not in found

    actions = continuity.next_actions(db, 1)
    # 확인할 것이 남아 있어도 할 수 있는 일이 먼저 나온다.
    assert actions[0] == {"type": "do", "step_id": first["id"], "gap_id": None,
                          "text": "'집계 자료 준비' 단계를 진행할 수 있습니다."}
    assert {a["type"] for a in actions[1:]} == {"check"}


def test_work_continues_past_done_steps_and_stops_at_the_blocked_one(db):
    add(db, "2025_요청.hwpx", "2025-09-01",
        "절차: 취합 → 작성 → 제출\n구비서류: 부서별 취합 자료")
    rc.restore(db, BASIS)
    first, second, third = stated(db)
    assert "준비 자료: 부서별 취합 자료." in continuity.next_actions(db, 1)[0]["text"]
    db.set_step_run(first["id"], ws.DONE)
    assert continuity.next_actions(db, 1)[0]["step_id"] == second["id"]
    db.set_step_run(second["id"], ws.DONE)
    # 남은 것은 막힌 단계뿐이다 — 할 일 대신 확인할 일만 남는다.
    assert [a["type"] for a in continuity.next_actions(db, 1)][0] == "check"


# ── 빠진 항목: 한 번 다시 찾고, 구체적인 다음 행동을 준다 ───────────

def test_missing_field_says_what_was_searched_and_where_to_look_next(db):
    add(db, "2025_요청.hwpx", "2025-09-01", "절차: 취합 → 작성 → 제출\n제출처: 기획예산과")
    rc.restore(db, BASIS)
    gap = gaps(db)["missing:deadline"]
    assert gap["field"] == "기한" and gap["kind"] == "missing"
    assert gap["research"] == ("이 업무의 문서 1건과 관련 문서 0건에서 기한 표기"
                               "(제출기한, 제출 기한, 회신기한 등)를 찾지 못했습니다.")
    assert "요청·시행 공문 본문과 붙임" in gap["next_action"]
    # 자료에 있는 제출처를 써서 누구에게 확인할지까지 적는다.
    assert "제출처(기획예산과)에 이번 회차 일정을 확인" in gap["next_action"]
    assert "온나라에서 전임자 작성 문서를 수집" in gap["next_action"]


def test_no_gap_ever_ends_with_just_ask_the_predecessor(db):
    add(db, "2024_제출본.hwpx", "2024-09-27", "제출기한: 2024-09-27")
    add(db, "메모A.hwpx", "2025-08-01", "제출처: 기획예산과")
    add(db, "메모B.hwpx", "2025-08-02", "제출처: 감사담당관")
    rc.restore(db, BASIS)
    found = db.info_gaps(1)
    assert {g["kind"] for g in found} == {"missing", "conflict", "stale"}
    for gap in found:
        assert gap["next_action"] and len(gap["next_action"]) > 20
        assert "전임자에게" not in gap["next_action"] and "문의하세요" != gap["next_action"]


def test_research_finds_a_candidate_in_an_unassigned_document_without_adopting_it(db):
    add(db, "2025_요청.hwpx", "2025-09-01", "절차: 취합 → 작성 → 제출")
    stray = add(db, "2025_실적보고_시행공문.hwpx", "2025-09-02",
                "제출기한: 2025-09-30\n제출처: 기획예산과", task=None)
    rc.restore(db, BASIS)
    gap = gaps(db)["missing:deadline"]
    assert "2025_실적보고_시행공문.hwpx — 제출기한: 2025-09-30" in gap["research"]
    assert "이 업무의 자료인지는 확인되지 않았습니다" in gap["research"]
    assert "이 업무에 배정하면" in gap["next_action"]
    evidence = db.evidence_for("gap", [gap["id"]])[gap["id"]]
    assert [e["doc_id"] for e in evidence] == [stray]
    # 묶이지 않은 문서의 값을 이 업무의 항목으로 올리지 않는다.
    assert not any(i["kind"] == rc.DEADLINE for i in db.task_items(1))

    # 사람이 그 문서를 이 업무에 배정하면 항목이 생기고 공백이 닫힌다.
    db.assign_document(1, stray)
    rc.restore(db, BASIS)
    assert "missing:deadline" not in gaps(db)
    closed = next(g for g in db.info_gaps(1, state=None) if g["gap_key"] == "missing:deadline")
    assert (closed["state"], closed["resolution"]) == ("resolved", db.AUTO_RESOLVED)
    assert not stated(db)[-1]["blocked"]


def test_research_is_bounded_and_ignores_other_tasks_unrelated_documents(db):
    add(db, "2025_요청.hwpx", "2025-09-01", "절차: 취합 → 작성 → 제출")
    add(db, "계약서.hwpx", "2025-09-03", "납기: 2025-12-01", task=2)   # 다른 업무, 이름도 무관
    for index in range(continuity.RESEARCH_DOC_LIMIT + 10):
        add(db, f"기타{index:02d}.hwpx", "2025-01-01", "제출기한: 별도 통보", task=None)
    found, looked = continuity.research(db, 1, rc.DEADLINE)
    assert looked == continuity.RESEARCH_DOC_LIMIT and found == []


def test_other_tasks_document_naming_this_task_is_considered_related(db):
    add(db, "2025_요청.hwpx", "2025-09-01", "절차: 취합 → 작성 → 제출")
    add(db, "총괄계획.hwpx", "2025-08-20", "정기 실적보고 일정\n제출기한: 2025-09-30", task=2)
    found, _looked = continuity.research(db, 1, rc.DEADLINE)
    assert [item.value for item in found] == ["2025-09-30"]


# ── 충돌: 나란히 놓고 고르지 않는다 ─────────────────────────────────

def test_conflict_gap_lays_both_sources_side_by_side(db):
    add(db, "시행공문.hwpx", "2025-09-01", "절차: 취합 → 작성 → 제출\n제출기한: 2025-09-30")
    add(db, "메모.hwpx", "2025-09-05", "제출기한: 2025-09-26")
    rc.restore(db, BASIS)
    gap = gaps(db)["conflict:deadline:제출기한"]
    assert gap["field"] == "기한 · 제출기한" and gap["item_id"]
    assert "제출기한: 2025-09-30 — 시행공문.hwpx (2025-09-01 · 개인 폴더 자료(PC 파일))" in gap["research"]
    assert "제출기한: 2025-09-26 — 메모.hwpx (2025-09-05 · 개인 폴더 자료(PC 파일))" in gap["research"]
    assert "확정 전에도" in gap["next_action"]
    assert gap["step_id"] == stated(db)[-1]["id"]
    assert not stated(db)[0]["blocked"]

    # 사람이 값을 정해 적으면 공백이 닫히고 제출 단계가 풀린다.
    item = next(i for i in db.task_items(1) if i["kind"] == rc.DEADLINE)
    db.review_task_item(item["id"], "edit", "2025-09-30 (시행공문 기준)", "후임")
    rc.restore(db, BASIS)
    assert "conflict:deadline:제출기한" not in gaps(db)
    assert next(i for i in db.task_items(1) if i["kind"] == rc.DEADLINE)["content"] == (
        "2025-09-30 (시행공문 기준)")
    # 그 뒤 자료가 또 달라지면 다시 묻는다.
    add(db, "변경공문.hwpx", "2025-09-08", "제출기한: 2025-10-15")
    rc.restore(db, BASIS)
    assert "conflict:deadline:제출기한" in gaps(db)


# ── 과거 기한과 이후 상태 ───────────────────────────────────────────

def test_past_deadline_is_not_called_overdue(db):
    add(db, "2025_요청.hwpx", "2025-08-01",
        "절차: 취합 → 작성 → 회신\n회신기한: 2025-08-29\n회신처: 감사담당관")
    rc.restore(db, BASIS)
    gap = gaps(db)["stale:deadline:회신기한"]
    assert "기준일(2025-09-10) 이전입니다" in gap["detail"]
    assert "자료로 알 수 없습니다" in gap["detail"]
    assert "지연" not in gap["detail"] and "미완료" not in gap["detail"]
    assert gap["step_id"] == stated(db)[-1]["id"]


def test_what_followed_the_last_record_points_at_the_next_precedent_step(db):
    request = add(db, "2025_자료요청.hwpx", "2025-09-01")
    collected = add(db, "2025_취합본.hwpx", "2025-09-05")
    db.replace_task_steps(1, 2025, [
        {"ordinal": 1, "label": "자료 요청", "month": 9, "day_hint": "9월 초",
         "doc_id": request, "gap_note": None},
        {"ordinal": 2, "label": "취합", "month": 9, "day_hint": "9월 초",
         "doc_id": collected, "gap_note": None}])
    rc.restore(db, BASIS)
    gap = gaps(db)["afterwards"]
    assert gap["step_id"] is None and gap["detail"].startswith("2025-09-05 이후의 자료가 없습니다")
    assert "결재·발송 문서가 있는지 확인" in gap["next_action"]

    # 문서에 적힌 절차가 없으면 지난 회차 사례로 시작점을 준다.
    first = continuity.next_actions(db, 1)[0]
    assert first["type"] == "do" and "'자료 요청 (2025년 9월 초)'부터" in first["text"]
    assert "2025_자료요청.hwpx" in first["text"]
    assert "수행 사례 2단계" in gaps(db)["missing:procedure"]["next_action"]


# ── 재분석과 사람의 판단 ────────────────────────────────────────────

def test_gap_closed_by_a_person_stays_closed_and_user_gaps_are_untouched(db):
    add(db, "2025_요청.hwpx", "2025-09-01", "절차: 취합 → 작성 → 제출")
    rc.restore(db, BASIS)
    db.resolve_info_gap(gaps(db)["missing:recipient"]["id"], "기획예산과로 확인(9/8 통화)")
    own = db.add_info_gap(1, "missing", "결재선", "올해 결재선 변경 여부", next_action="직제 확인")

    rc.restore(db, BASIS)
    found = gaps(db)
    assert "missing:recipient" not in found, "사람이 닫은 공백을 다시 열었다"
    assert any(g["id"] == own for g in db.info_gaps(1))
    kept = next(g for g in db.info_gaps(1, state=None) if g["gap_key"] == "missing:recipient")
    assert kept["resolution"] == "기획예산과로 확인(9/8 통화)"
    # 같은 공백이 행으로 불어나지 않는다.
    assert len([g for g in db.info_gaps(1, state=None) if g["origin"] == "auto"]) == len(
        {g["gap_key"] for g in db.info_gaps(1, state=None) if g["origin"] == "auto"})


def test_auto_closed_gap_reopens_when_the_source_disappears(db):
    add(db, "2025_요청.hwpx", "2025-09-01", "절차: 취합 → 작성 → 제출")
    notice = add(db, "시행공문.hwpx", "2025-09-02", "제출기한: 2025-09-30")
    rc.restore(db, BASIS)
    assert "missing:deadline" not in gaps(db)
    db.mark_missing([notice])
    rc.restore(db, BASIS)
    assert "missing:deadline" in gaps(db)


def test_stage_note_counts_open_gaps(db):
    add(db, "2025_요청.hwpx", "2025-09-01", "절차: 취합 → 작성 → 제출")
    totals = rc.restore(db, BASIS)
    assert totals["gaps"] == len(db.info_gaps(1)) + len(db.info_gaps(2)) >= 3
