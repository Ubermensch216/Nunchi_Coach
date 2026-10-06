"""적힌 절차와 과거 사례의 구분, 마지막 기록과 '이후 상태 미상' (C03).

완료 증거: 시간순 파일을 의무 절차로 오인하지 않고, 입력·서식·산출물을 잇는다.
"""

from datetime import date
from pathlib import Path

import pytest

from app.core import reconstruction as rc, workstate as ws
from app.db import Database

BASIS = date(2025, 9, 10)


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    database.con.execute("INSERT INTO tasks(id, name) VALUES (1, '정기 실적 보고')")
    yield database
    database.close()


def add(db, name, when, text="", kind="body"):
    source = db.add_source(r"D:\자료")
    doc = db.upsert_document(source, {
        "path": rf"D:\자료\{name}", "filename": name, "ext": ".hwpx", "parse_status": "ok",
        "hash": name, "char_count": len(text),
        "eff_date": when, "eff_year": int(when[:4]) if when else None,
        "eff_month": int(when[5:7]) if when else None,
        "eff_precision": "day" if when else None, "eff_date_kind": kind if when else None})
    if text:
        db.replace_sections(doc, [("paragraph", 1, "1문단", text)])
    db.index_document(doc, name, name, "", text)
    db.assign_document(1, doc)
    return doc


def precedent(db, year, *steps):
    db.replace_task_steps(1, year, [
        {"ordinal": n, "label": label, "month": month, "day_hint": f"{month}월 초",
         "doc_id": doc, "gap_note": None}
        for n, (label, month, doc) in enumerate(steps, start=1)])


def steps_of(db, kind):
    return [s for s in db.work_steps(1) if s["procedure_kind"] == kind]


def records(db):
    return {i["slot"]: i for i in db.task_items(1) if i["kind"] == rc.RECORD}


# ── 절차 문구 나누기 ────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("집계 자료 준비 → 실적표 작성 → 검토 후 제출", ["집계 자료 준비", "실적표 작성", "검토 후 제출"]),
    ("① 요청 공문 접수 ② 부서별 취합 ③ 결재", ["요청 공문 접수", "부서별 취합", "결재"]),
    ("1) 계획 수립 2) 계약 체결 3) 정산.", ["계획 수립", "계약 체결", "정산"]),
    ("접수 -> 검토 -> 회신", ["접수", "검토", "회신"]),
])
def test_stated_procedure_is_split_where_the_document_shows_an_order(text, expected):
    assert rc.split_procedure(text) == expected


@pytest.mark.parametrize("text", [
    "별첨 지침 참조",                       # 순서가 드러나지 않는다
    "2025. 9. 30. 까지 부서별로 처리",       # 날짜의 숫자는 단계 번호가 아니다
    "3) 정산 5) 보고",                      # 1부터 차례가 아니다
    "",
])
def test_text_without_a_visible_order_is_not_turned_into_steps(text):
    assert rc.split_procedure(text) == []


# ── 적힌 절차와 과거 사례 ───────────────────────────────────────────

def test_stated_procedure_becomes_ordered_steps_with_inputs_forms_and_closing(db):
    doc = add(db, "2025_실적보고_요청.hwpx", "2025-09-01",
              "절차: 집계 자료 준비 → 실적표 작성 → 검토 후 제출. 제출기한: 2025-09-30.\n"
              "제출처: 기획예산과\n구비서류: 부서별 집계 자료\n사용 서식: 실적표 별지 제3호")
    totals = rc.restore(db, BASIS)
    assert totals["stated_steps"] == 3

    first, second, third = steps_of(db, ws.PROCEDURE_STATED)
    assert [s["label"] for s in (first, second, third)] == [
        "집계 자료 준비", "실적표 작성", "검토 후 제출"]
    assert all(s["basis_state"] == ws.STATED and s["applicability"] == ws.UNKNOWN
               for s in (first, second, third))
    # 문서가 순서를 적었으므로 선행 관계가 있다.
    assert (first["after_step_id"], second["after_step_id"], third["after_step_id"]) == (
        None, first["id"], second["id"])
    # 낱말이 겹치는 단계에 붙는다: '집계 자료' → 준비 단계, '실적표' 서식 → 작성 단계.
    assert first["inputs"] == "부서별 집계 자료" and second["forms"] == "실적표 별지 제3호"
    assert third["done_criteria"] == "제출처: 기획예산과 · 제출기한: 2025-09-30"
    assert first["evidence"][0]["doc_id"] == doc and "절차:" in first["evidence"][0]["quote"]


def test_chronological_files_are_precedent_not_required_procedure(db):
    request = add(db, "2024_자료요청.hwpx", "2024-09-02")
    collect = add(db, "2024_취합본.hwpx", "2024-09-12")
    submit = add(db, "2024_제출본.hwpx", "2024-09-27")
    precedent(db, 2024, ("자료 요청", 9, request), ("취합", 9, collect), ("제출", 9, submit))
    rc.restore(db, BASIS)

    assert steps_of(db, ws.PROCEDURE_STATED) == []
    cases = steps_of(db, ws.PROCEDURE_PRECEDENT)
    assert [s["label"] for s in cases] == [
        "자료 요청 (2024년 9월 초)", "취합 (2024년 9월 초)", "제출 (2024년 9월 초)"]
    for case in cases:
        # 그렇게 했다는 기록일 뿐이다 — 추정이고, 과거이고, 선행 관계가 없다.
        assert (case["basis_state"], case["applicability"], case["after_step_id"]) == (
            ws.INFERRED, ws.PAST, None)
    assert cases[2]["output"] == "2024_제출본.hwpx"
    assert cases[2]["evidence"][0]["doc_id"] == submit
    assert ws.procedure_label(cases[0]["procedure_kind"]) == "과거 수행 사례"


def test_stated_procedure_and_precedent_are_kept_apart_in_one_task(db):
    old = add(db, "2024_제출본.hwpx", "2024-09-27")
    add(db, "2025_요청.hwpx", "2025-09-01", "처리 절차: ① 취합 ② 작성 ③ 제출")
    precedent(db, 2024, ("제출", 9, old))
    precedent(db, 2025, ("자료 요청", 9, 2))
    rc.restore(db, BASIS)

    stated, cases = steps_of(db, ws.PROCEDURE_STATED), steps_of(db, ws.PROCEDURE_PRECEDENT)
    assert [s["label"] for s in stated] == ["취합", "작성", "제출"]
    # 과거 사례는 끝까지 간 회차(기준 연도보다 앞선 가장 최근 해) 한 줄만 쓴다.
    # 올해 것은 아직 앞부분뿐이라 '그다음에 무엇을 했는가'가 없다.
    assert [s["label"] for s in cases] == ["제출 (2024년 9월 초)"]
    assert max(s["ordinal"] for s in stated) < min(s["ordinal"] for s in cases)
    assert cases[0]["after_step_id"] is None


def test_conflicting_procedures_are_not_promoted_to_a_stated_procedure(db):
    add(db, "지침.hwpx", "2025-08-01", "절차: 취합 → 작성 → 제출")
    add(db, "메모.hwpx", "2025-08-20", "절차: 작성 → 과장 검토 → 제출")
    rc.restore(db, BASIS)
    assert steps_of(db, ws.PROCEDURE_STATED) == []
    procedure = next(i for i in db.task_items(1) if i["kind"] == rc.PROCEDURE)
    assert procedure["basis_state"] == ws.CONFLICT and " / " in procedure["content"]


def test_unmatched_form_stays_a_task_item_and_past_deadline_is_marked(db):
    add(db, "2025_요청.hwpx", "2025-08-01",
        "절차: 취합 → 검토 → 회신\n사용 서식: 별지 제7호\n회신기한: 2025-08-29")
    rc.restore(db, BASIS)
    stated = steps_of(db, ws.PROCEDURE_STATED)
    assert all(not s["forms"] for s in stated), "어느 단계의 서식인지 모르면 붙이지 않는다"
    assert any(i["kind"] == rc.FORM for i in db.task_items(1))
    assert stated[-1]["done_criteria"] == "회신기한: 2025-08-29 (과거 기한)"


# ── 현재까지의 기록 ─────────────────────────────────────────────────

def test_last_record_is_reported_and_what_followed_is_unknown(db):
    add(db, "2025_자료요청.hwpx", "2025-09-01")
    last = add(db, "2025_취합본.hwpx", "2025-09-05")
    rc.restore(db, BASIS)
    found = records(db)
    record, afterwards = found[rc.SLOT_LAST_RECORD], found[rc.SLOT_AFTERWARDS]
    assert record["content"] == "2025-09-05 · 취합 — 2025_취합본.hwpx"
    assert (record["basis_state"], record["applicability"]) == (ws.INFERRED, ws.PAST)
    assert record["evidence"][0]["doc_id"] == last
    # 후속 자료가 없다는 것은 끝났다는 뜻도, 안 했다는 뜻도 아니다.
    assert afterwards["content"].startswith("2025-09-05 이후의 자료가 없습니다.")
    assert (afterwards["basis_state"], afterwards["applicability"]) == (ws.MISSING, ws.UNKNOWN)
    assert "완료" not in afterwards["content"] and "지연" not in afterwards["content"]


def test_only_last_years_records_do_not_say_this_years_round_started(db):
    add(db, "2024_제출본.hwpx", "2024-09-27")
    rc.restore(db, BASIS)
    found = records(db)
    assert found[rc.SLOT_LAST_RECORD]["content"].startswith("2024-09-27 · 제출")
    assert found[rc.SLOT_AFTERWARDS]["content"].startswith("2025년 자료가 없습니다.")


def test_file_copy_dates_are_not_used_as_the_last_record(db):
    add(db, "2025_요청.hwpx", "2025-09-01")
    add(db, "복사본.hwpx", "2025-09-09", kind="fs")
    add(db, "날짜없음.hwpx", None)
    rc.restore(db, BASIS)
    assert records(db)[rc.SLOT_LAST_RECORD]["content"].startswith("2025-09-01")


def test_task_without_dated_documents_says_so_instead_of_guessing(db):
    add(db, "날짜없음.hwpx", None)
    rc.restore(db, BASIS)
    found = records(db)
    assert set(found) == {rc.SLOT_AFTERWARDS}
    assert found[rc.SLOT_AFTERWARDS]["basis_state"] == ws.MISSING


# ── 재분석과 수행 기록 ──────────────────────────────────────────────

def test_executed_and_confirmed_steps_survive_a_changed_procedure(db):
    add(db, "2025_요청.hwpx", "2025-09-01", "절차: 취합 → 작성 → 제출")
    rc.restore(db, BASIS)
    first, second, third = steps_of(db, ws.PROCEDURE_STATED)
    db.set_step_run(first["id"], ws.DONE, note="9월분 취합 완료")
    db.review_work_step(second["id"], "confirm", reviewer_name="후임")
    assert rc.restore(db, BASIS)["recheck"] == 0

    db.replace_sections(1, [("paragraph", 1, "1문단", "절차: 부서 취합 → 실적표 작성 → 결재 → 제출")])
    assert rc.restore(db, BASIS)["recheck"] == 2
    steps = steps_of(db, ws.PROCEDURE_STATED)
    assert [s["label"] for s in steps] == ["취합", "작성", "결재", "제출"]
    assert (steps[0]["run_state"], steps[0]["auto_label"]) == (ws.DONE, "부서 취합")
    assert (steps[1]["human_state"], steps[1]["auto_label"]) == (ws.RECHECK, "실적표 작성")
    # 손대지 않은 단계는 새 자료를 따른다.
    assert steps[2]["label"] == "결재" and steps[3]["after_step_id"] == steps[2]["id"]
