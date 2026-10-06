"""문서에 명시된 항목의 자동 추출과 출처 검증 (C02).

완료 증거: 온나라/폴더에서 업무·기한·절차 후보가 자동으로 생기고, 공식 근거가
보완된다. 사람이 먼저 입력하지 않는다.
"""

from datetime import date
from pathlib import Path

import pytest

from app.connectors.onnara import FixtureConnector, SearchScope
from app.core import reconstruction as rc, workstate as ws
from app.db import Database
from app.jobs.onnara_collect import Collection
from app.tools.onnara_demo import index_collection, make_catalog

BASIS = date(2025, 9, 10)


def read(text, year=2025, official=False, doc=1, locator="1문단"):
    return rc.read_section(text, doc, locator, year, official)


def one(text, **kwargs):
    found = read(text, **kwargs)
    assert len(found) == 1, found
    return found[0]


# ── 한 조각에서 읽기 ────────────────────────────────────────────────

def test_several_labelled_values_on_one_line_are_separated():
    found = read("절차: 집계 자료 준비 → 실적표 작성 → 검토 후 제출. 제출기한: 2025-09-30.")
    assert [(f.kind, f.slot, f.value) for f in found] == [
        (rc.PROCEDURE, "절차", "집계 자료 준비 → 실적표 작성 → 검토 후 제출"),
        (rc.DEADLINE, "제출기한", "2025-09-30"),
    ]
    assert found[1].when == date(2025, 9, 30)
    assert found[1].quote == "제출기한: 2025-09-30."


@pytest.mark.parametrize("text,value,when", [
    ("○ 제출 기한 : 2025. 9. 30.(화) 18:00까지", "2025-09-30", date(2025, 9, 30)),
    ("회신기한: 2025년 10월 2일", "2025-10-02", date(2025, 10, 2)),
    # 연도가 적혀 있지 않으면 화면 값에 연도를 지어 넣지 않는다.
    ("마감: 9월 30일까지", "9월 30일", date(2025, 9, 30)),
])
def test_deadlines_are_read_as_dates(text, value, when):
    item = one(text)
    assert (item.kind, item.value, item.when) == (rc.DEADLINE, value, when)


@pytest.mark.parametrize("text", [
    "제출기한: 별도 통보 예정",            # 날짜가 없다
    "예산: 미정",                          # 숫자가 없다
    "무기한: 2025-09-30",                  # 다른 낱말의 일부다
    "작성일: 2025-09-01. 작성자: 합성담당자.",  # '담당자'는 이름의 일부다
    "제출기한은 9월 말로 예상된다",        # 이름표 형식이 아니다 — 짐작하지 않는다
    "제출기한: 2025-02-30",                # 없는 날짜
])
def test_what_is_not_actually_stated_is_not_extracted(text):
    assert read(text) == []


def test_other_field_kinds_and_units():
    assert one("예산(백만원): 820").value == "820 백만원"
    assert one("제출처: 기획예산과").kind == rc.RECIPIENT
    assert one("구비서류: 집계표, 결과보고서").kind == rc.INPUT
    assert one("사용 서식: 별지 제3호").slot == "사용서식"
    assert one("문의: 예산팀 내선 1234").kind == rc.CONTACT
    assert one("추진 목적: 분기별 실적의 정기 점검").kind == rc.PURPOSE


def test_table_row_uses_its_header_as_the_label_and_keeps_the_row_name():
    row = "첫 행: 사업 | 예산(백만원) | 비고\n1열: 시범사업 3 | 2열: 820 | 3열: 계속"
    item = one(row, locator="표 1 · 2행")
    assert (item.kind, item.slot, item.value) == (rc.AMOUNT, "예산 · 시범사업 3", "820 백만원")
    assert "시범사업 3" in item.quote and "예산(백만원)" in item.quote

    with_unit = one("머리글: 구분 | 요구액\n1열: 운영비 | 2열: 95\n단위: 천원")
    assert (with_unit.slot, with_unit.value) == ("요구액 · 운영비", "95 천원")
    # 머리글이 항목 이름표가 아니면 읽지 않는다.
    assert read("첫 행: 순번 | 성명\n1열: 1 | 2열: 홍길동") == []


def test_source_check_refuses_a_value_that_is_not_in_its_quote():
    good = rc.Found(rc.AMOUNT, "예산", "820", 1, "1문단", "예산: 820")
    assert rc._verified(good, "820")
    assert not rc._verified(rc.Found(rc.AMOUNT, "예산", "999", 1, "1문단", "예산: 820"), "999")
    # 숫자의 일부만 겹치는 경우도 통과시키지 않는다.
    assert not rc._verified(rc.Found(rc.AMOUNT, "예산", "1,820", 1, "1문단", "예산: 820"), "1,820")


# ── 업무 단위로 접기 ────────────────────────────────────────────────

def choose(*found):
    return {(c["kind"], c["slot"]): c for c in rc.choose(list(found), BASIS)}


def test_latest_period_wins_and_past_values_are_not_mixed_in():
    result = choose(
        one("제출기한: 2024-09-27", year=2024, doc=1),
        one("제출기한: 2025-09-30", year=2025, doc=2))
    deadline = result[(rc.DEADLINE, "제출기한")]
    assert (deadline["content"], deadline["basis_state"], deadline["applicability"]) == (
        "2025-09-30", ws.STATED, ws.CURRENT)
    assert [doc for doc, _locator, _quote in deadline["evidence"]] == [2]


def test_a_deadline_before_the_basis_date_is_a_past_record_not_a_current_due_date():
    result = choose(one("제출기한: 2025-08-29", doc=1), one("예산: 820", year=2024, doc=2))
    assert result[(rc.DEADLINE, "제출기한")]["applicability"] == ws.PAST
    assert result[(rc.AMOUNT, "예산")]["applicability"] == ws.PAST
    # 올해 문서의 금액은 지금도 적용되는지 알 수 없다 — 확인했다고 하지 않는다.
    assert choose(one("예산: 820"))[(rc.AMOUNT, "예산")]["applicability"] == ws.UNKNOWN


def test_conflicting_sources_are_shown_side_by_side_and_official_does_not_win():
    result = choose(
        one("제출기한: 2025-09-26", doc=1, locator="2문단"),
        one("제출기한: 2025-09-30", doc=2, official=True),
        one("제출기한: 2025. 9. 30.", doc=3))
    deadline = result[(rc.DEADLINE, "제출기한")]
    assert deadline["basis_state"] == ws.CONFLICT
    # 등록본의 값이 앞에 오지만 다른 값도 지워지지 않는다.
    assert deadline["content"] == "2025-09-30 / 2025-09-26"
    assert deadline["applicability"] == ws.UNKNOWN
    assert {doc for doc, _l, _q in deadline["evidence"]} == {1, 2, 3}


def test_same_value_written_differently_is_not_a_conflict():
    result = choose(one("예산: 1,820 백만원", doc=1), one("예산 : 1,820백만원.", doc=2))
    assert result[(rc.AMOUNT, "예산")]["basis_state"] == ws.STATED


# ── 온나라 수집본과 개인 폴더를 함께 ────────────────────────────────

@pytest.fixture
def project(tmp_path: Path):
    """온나라 수집본 2건(본문 ODT+붙임)과 개인 폴더의 지난해 초안이 한 업무에 묶인 상태."""
    catalog = make_catalog(tmp_path / "source", count=5)
    state = Collection(FixtureConnector(catalog),
                       SearchScope(("author-001",), "2025-01-01", "2025-12-31"),
                       tmp_path / "collected").run()
    index_collection(state, tmp_path / "collected", tmp_path / "project")
    db = Database(tmp_path / "project" / "project.db")
    db.con.execute("INSERT INTO tasks(id, name) VALUES (1, '정기 실적 보고')")
    for row in db.con.execute("SELECT id FROM documents").fetchall():
        db.assign_document(1, row["id"])
    yield db
    db.close()


def stated(db):
    """문서에서 읽어 낸 항목만(기록 요약 제외)."""
    return [item for item in db.task_items(1) if item["kind"] != rc.RECORD]


def add_local(db, name, year, text, hash_="local"):
    source = db.add_source(str(Path(db.path).parent / "개인폴더"))
    doc = db.upsert_document(source, {
        "path": f"개인폴더/{name}", "filename": name, "ext": ".hwpx", "parse_status": "ok",
        "hash": hash_, "eff_year": year, "char_count": len(text)})
    db.replace_sections(doc, [("paragraph", 1, "1문단", text)])
    db.index_document(doc, name, f"개인폴더/{name}", "", text)
    db.assign_document(1, doc)
    return doc


def test_items_appear_from_collected_documents_without_any_human_input(project):
    db = project
    totals = rc.restore(db, BASIS)
    # 문서에서 읽은 항목에 더해 '현재까지의 기록' 두 줄이 항상 함께 생긴다.
    assert totals["tasks"] == 1 and totals["items"] >= 4
    assert totals["added"] == totals["items"] + 2
    items = {(i["kind"], i["slot"]): i for i in db.task_items(1)}

    deadline = items[(rc.DEADLINE, "제출기한")]
    assert (deadline["content"], deadline["basis_state"], deadline["applicability"]) == (
        "2025-09-30", ws.STATED, ws.CURRENT)
    assert deadline["human_state"] == ws.NONE and deadline["extractor"] == rc.EXTRACTOR
    assert all(e["current"] and e["filename"].endswith(".odt") for e in deadline["evidence"])
    assert items[(rc.PROCEDURE, "절차")]["content"].startswith("집계 자료 준비 →")
    # 표의 금액은 어느 사업의 값인지 잃지 않는다.
    amounts = {slot: i["content"] for (kind, slot), i in items.items() if kind == rc.AMOUNT}
    assert amounts == {"예산 · 시범사업 1": "820 백만원", "예산 · 시범사업 2": "820 백만원"}
    # 전임자 정보 없이 '자료로 시작'한 것으로 남는다.
    context = db.work_context()
    assert (context["mode"], context["predecessor"]) == (ws.MODE_MATERIALS, None)
    assert db.con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


def test_official_documents_supply_what_the_personal_folder_lacks(tmp_path):
    """개인 폴더에는 지난해 초안뿐이다. 수집 전에는 과거 기한만, 수집 후에는 올해 기한이 나온다."""
    db = Database(tmp_path / "p.db")
    db.init()
    db.con.execute("INSERT INTO tasks(id, name) VALUES (1, '정기 실적 보고')")
    draft = add_local(db, "2024_실적보고_초안.hwpx", 2024, "제출기한: 2024-09-27")
    rc.restore(db, BASIS)
    before, = stated(db)
    assert (before["content"], before["applicability"]) == ("2024-09-27", ws.PAST)

    registered = add_local(db, "수집본.odt", 2025, "제출기한: 2025-09-30", hash_="registered")
    rc.restore(db, BASIS)
    after, = stated(db)
    assert (after["content"], after["applicability"], after["version"]) == (
        "2025-09-30", ws.CURRENT, 2)
    assert [e["doc_id"] for e in after["evidence"]] == [registered] != [draft]
    db.close()


def test_personal_note_that_disagrees_with_the_registered_document_becomes_a_conflict(project):
    db = project
    add_local(db, "메모.hwpx", 2025, "제출기한: 2025-09-26 (과장님 구두 지시)")
    rc.restore(db, BASIS)
    deadline = next(i for i in db.task_items(1) if i["kind"] == rc.DEADLINE)
    assert deadline["basis_state"] == ws.CONFLICT
    assert deadline["content"] == "2025-09-30 / 2025-09-26"
    names = [e["filename"] for e in deadline["evidence"]]
    assert names[0].endswith(".odt") and "메모.hwpx" in names


def test_rerun_is_stable_and_keeps_what_a_person_changed(project):
    db = project
    rc.restore(db, BASIS)
    second = rc.restore(db, BASIS)
    assert (second["added"], second["updated"], second["removed"]) == (0, 0, 0)

    deadline = next(i for i in db.task_items(1) if i["kind"] == rc.DEADLINE)
    db.review_task_item(deadline["id"], "edit", "2025-10-02 (연장 통보)", "후임")
    add_local(db, "변경공문.hwpx", 2025, "제출기한: 2025-10-15")
    assert rc.restore(db, BASIS)["recheck"] == 1
    deadline = next(i for i in db.task_items(1) if i["kind"] == rc.DEADLINE)
    assert deadline["content"] == "2025-10-02 (연장 통보)"
    assert deadline["human_state"] == ws.RECHECK and "2025-10-15" in deadline["auto_content"]


def test_duplicate_copies_and_missing_originals_are_not_used_as_evidence(project):
    db = project
    add_local(db, "사본1.hwpx", 2025, "제출처: 기획예산과", hash_="same")
    add_local(db, "사본2.hwpx", 2025, "제출처: 기획예산과", hash_="same")
    gone = add_local(db, "사라진문서.hwpx", 2025, "제출처: 감사담당관", hash_="gone")
    db.mark_missing([gone])
    rc.restore(db, BASIS)
    recipient = next(i for i in db.task_items(1) if i["kind"] == rc.RECIPIENT)
    assert recipient["basis_state"] == ws.STATED and len(recipient["evidence"]) == 1


def test_pipeline_stage_fills_items_without_a_model(project, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from app.jobs.pipeline import STAGE_RESTORE, Pipeline

    db = project
    reports = []
    worker = Pipeline(db.path)
    worker.stage_done.connect(reports.append)
    worker._restore(db)
    assert reports[-1].stage == STAGE_RESTORE and reports[-1].note.startswith("항목 ")
    assert db.get_meta("items_checked") == "1" and db.task_items(1)

    # 문서가 바뀌면 다음 실행이 다시 읽는다.
    db.invalidate_document_analysis(1)
    assert db.get_meta("items_checked") is None
