"""건 — 목록의 한 행이 한 건이다 (규제개혁 개선계획서 P1-1).

같은 건의가 부서 목록 → 우리 기관 목록 → 상급기관 취합표 → 부처 검토결과에 나온다. 이것을 한
건으로 잇고, 자료가 말한 데까지만 진행을 매기며, 남의 기관 행은 세기만 하는지 본다. 기관·
과제·부서 이름은 지어낸 것이다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.core import continuity, matters, reconstruction as rc, task_manual, workcard
from app.db import Database
from app.ingest.hasher import sha256

BASIS = date(2026, 10, 14)
HEADER = ["통합연번", "연번", "구분", "광역\n(시도)", "기초\n(시군구)", "제    목", "관련법령", "소관부처1"]


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    database.con.execute("INSERT INTO tasks(id, name) VALUES (1, '규제혁신 과제 제출')")
    yield database
    database.close()


def _xlsx(folder: Path, name: str, sheets: dict[str, list[list]]) -> Path:
    book = Workbook()
    book.remove(book.active)
    for title, rows in sheets.items():
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(row)
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    return path


def _register(db, path: Path, year: int = 2025, task: int | None = 1) -> int:
    source = db.add_source(str(path.parent))
    digest, _ = sha256(path)
    doc = db.upsert_document(source, {
        "path": str(path), "filename": path.name, "ext": path.suffix, "parse_status": "ok",
        "hash": digest, "eff_year": year, "eff_date": f"{year}-03-01", "eff_precision": "day",
        "eff_date_kind": "body"})
    if task:
        db.assign_document(task, doc)
    return doc


def _row(n, locality, title, law="「어느 법」", ministry="국토교통부", round_="상반기"):
    return [n, n, round_, "가나", locality, title, law, ministry]


def _three_lists(db, folder: Path):
    """부서 목록, 우리 기관 목록, 상급기관 취합표, 부처 검토결과 — 같은 건이 네 번 나온다."""
    _register(db, _xlsx(folder, "2025년 상반기 건의과제 목록(토지정보과).xlsx",
                        {"건의과제": [HEADER, _row(1, "다라구", "토지 경계조정 간소화")]}))
    _register(db, _xlsx(folder, "(다라구)2025년 상반기 건의과제 목록.xlsx",
                        {"건의과제": [["2025년 상반기 지자체 규제개선 건의과제 목록"], HEADER,
                                     _row(1, "다라구", "토지 경계조정 간소화"),
                                     _row(2, "다라구", "공원 내 홍보관 설치 허용", ministry="산업통상자원부")]}))
    national = [HEADER] + [_row(n, f"마{n}시", f"남의 과제 {n}") for n in range(1, 8)]
    national.insert(3, _row(99, "가나 다라구", "토지 경계조정  간소화"))      # 띄어쓰기만 다르다
    _register(db, _xlsx(folder, "★2025년 상반기 건의과제 목록_취합.xlsx", {"전체": national}))
    review = [HEADER + ["부처의견1"], _row(1, "다라구", "토지 경계조정 간소화") + ["3. 수용불가"],
              _row(2, "마1시", "남의 과제 1") + ["1. 수용"]]
    _register(db, _xlsx(folder, "(붙임2) 2025년 상반기 건의과제 부처별 검토결과.xlsx", {"전체": review}))


# ── 머리글과 회신 ───────────────────────────────────────────────────

def test_a_list_needs_a_title_and_a_locality_column():
    assert matters.find_columns([tuple(HEADER)]).locality == 4
    assert matters.find_columns([("연번", "제목", "부서")]) is None          # 누구의 건인지 모른다
    # 규제사무 이름은 정비 칸이 함께 있어야 건 목록이다(등록규제 대장은 건 목록이 아니다).
    assert matters.find_columns([("연번", "시군구", "규제사무명", "등록일")]) is None
    found = matters.find_columns([("연번", "시군구", "규제사무명", "정비대상 여부", "정비내용")])
    assert found.kind == "regulation" and not found.outcome and len(found.extra) == 2


@pytest.mark.parametrize("text, outcome", [
    ("3. 수용불가", matters.REJECTED), ("부분수용", matters.PARTIAL), ("1. 수용", matters.ACCEPTED),
    ("중장기 검토", matters.PENDING), ("X", matters.REJECTED), ("O", matters.ACCEPTED),
    ("5. 기시행", ""), ("6. 타부처 소관", ""), ("", "")])
def test_reply_text_is_classified_only_when_it_says_so(text, outcome):
    assert matters.classify_outcome(text) == outcome


# ── 우리 기관 ───────────────────────────────────────────────────────

def test_own_locality_comes_from_file_names_or_the_user(db, tmp_path):
    assert matters.own_locality(db) == ""          # 모르면 정하지 않는다 — 건을 만들지 않는다
    for name in ("사례 제출목록(다라구).xlsx", "실적서(다라구).xlsx", "건의(다라구).xlsx", "참고(마1시).xlsx"):
        _register(db, _xlsx(tmp_path, name, {"s": [["a"]]}), task=None)
    assert matters.own_locality(db) == "다라구"
    matters.set_own_locality(db, "바사군")
    assert matters.own_locality(db) == "바사군"


def test_nothing_is_built_without_knowing_whose_matters(db, tmp_path):
    _register(db, _xlsx(tmp_path, "목록.xlsx", {"전체": [HEADER, _row(1, "다라구", "토지 경계조정 간소화")]}))
    assert matters.rebuild(db)["matters"] == 0
    assert db.con.execute("SELECT COUNT(*) FROM matters").fetchone()[0] == 0


# ── 한 건으로 잇기 ──────────────────────────────────────────────────

def test_one_matter_across_four_lists_with_stage_from_the_evidence(db, tmp_path):
    matters.set_own_locality(db, "다라구")
    _three_lists(db, tmp_path)
    totals = matters.rebuild(db)
    assert totals["matters"] == 2 and totals["other_rows"] == 8

    found = {m["title"]: m for m in matters.for_task(db, 1)}
    land = found["토지 경계조정 간소화"]
    assert land["stage"] == matters.ANSWERED and land["outcome"] == matters.REJECTED
    assert land["dept"] == "토지정보과"                     # 부서가 낸 목록의 이름에서
    assert land["ministry"] == "국토교통부" and land["round"] == "2025 상반기"
    assert [e["locator"] for e in land["evidence"]].count("'전체' 4행") == 1   # 취합표의 행 번호
    assert {e["list_kind"] for e in land["evidence"]} == {"own", "compiled"}
    assert all(e["current"] for e in land["evidence"])

    park = found["공원 내 홍보관 설치 허용"]
    assert park["stage"] == matters.LISTED and park["outcome"] == ""   # 우리 목록에만 있다
    assert "제출 근거 미확인" in matters.progress_line(park)
    # 남의 과제는 건이 되지 않는다(수용 회신이 있어도).
    assert "남의 과제 1" not in found


def test_rebuilding_keeps_the_same_ids_and_skips_unchanged_files(db, tmp_path):
    matters.set_own_locality(db, "다라구")
    _three_lists(db, tmp_path)
    matters.rebuild(db)
    before = {m["title"]: m["id"] for m in matters.for_task(db, 1)}
    assert matters.rebuild(db)["read"] == 0               # 같은 판은 다시 열지 않는다
    assert {m["title"]: m["id"] for m in matters.for_task(db, 1)} == before


def test_a_changed_file_is_not_read_and_earlier_matters_stay(db, tmp_path):
    matters.set_own_locality(db, "다라구")
    path = _xlsx(tmp_path, "(다라구)목록.xlsx", {"전체": [HEADER, _row(1, "다라구", "토지 경계조정 간소화")]})
    _register(db, path)
    matters.rebuild(db)
    # 분석 뒤에 원본이 바뀌었다. 저장된 해시와 다른 판은 근거로 읽지 않는다.
    _xlsx(tmp_path, "(다라구)목록.xlsx", {"전체": [HEADER, _row(1, "다라구", "전혀 다른 과제")]})
    db.con.execute("DELETE FROM matter_scans")
    assert matters.rebuild(db)["unreadable"] == 1
    assert [m["title"] for m in matters.for_task(db, 1)] == ["토지 경계조정 간소화"]
    assert db.con.execute("SELECT state FROM matter_scans").fetchone()[0] == "changed"


def test_people_decide_progress_and_their_word_survives(db, tmp_path):
    matters.set_own_locality(db, "다라구")
    _three_lists(db, tmp_path)
    matters.rebuild(db)
    park = next(m for m in matters.for_task(db, 1) if m["title"].startswith("공원"))
    matters.review(db, park["id"], stage="submitted", note="2025.4.30. 시행문으로 제출 확인")
    with pytest.raises(ValueError):
        matters.review(db, park["id"], stage="완료")
    db.con.execute("DELETE FROM matter_scans")      # 전부 다시 읽어도
    matters.rebuild(db)
    again = next(m for m in matters.for_task(db, 1) if m["id"] == park["id"])
    assert again["human_stage"] == "submitted" and again["human_note"].startswith("2025.4.30.")
    assert "담당자: 제출 확인" in matters.progress_line(again)


# ── 업무에서 보기 ───────────────────────────────────────────────────

def test_the_work_shows_matters_and_asks_about_unanswered_ones(db, tmp_path):
    matters.set_own_locality(db, "다라구")
    _three_lists(db, tmp_path)
    totals = rc.restore(db, BASIS, statements=False)
    assert totals["matters"] == 2

    check = next(a for a in continuity.next_actions(db, 1) if a["text"].startswith("[건별 진행 확인]"))
    assert check["type"] == "check"
    assert "2025 상반기 우리 기관 건 1건은" in check["text"] and "(기다리는 결과: 부처 회신(검토의견))" in check["text"]
    assert "‘공원 내 홍보관 설치 허용’" in check["text"] and "따로 잡혔을" not in check["text"]

    section = next(s for s in task_manual.build(db, 1).sections if s.key == "cases")
    assert section.lines[0].startswith("우리 기관의 건 2건 · 회신이 이어진 것 1건.")
    assert "다른 기관 행 8건" in section.lines[0]
    assert any(line.startswith("2025 상반기 · 토지 경계조정 간소화 — 주관 토지정보과 · 소관 국토교통부")
               for line in section.lines)
    assert section.evidence and all(e["locator"].endswith("행") for e in section.evidence)

    draft = workcard.draft_markdown(db, 1, BASIS)
    assert "## 건별 현황" in draft and "회신 ‘3. 수용불가’" in draft


def test_answered_or_confirmed_matters_are_not_asked_again(db, tmp_path):
    matters.set_own_locality(db, "다라구")
    _three_lists(db, tmp_path)
    rc.restore(db, BASIS, statements=False)
    park = next(m for m in matters.for_task(db, 1) if m["title"].startswith("공원"))
    matters.review(db, park["id"], stage="submitted")
    assert not any(a["text"].startswith("[건별 진행 확인]") for a in continuity.next_actions(db, 1))


def test_a_retitled_twin_is_pointed_out_but_not_merged(db, tmp_path):
    matters.set_own_locality(db, "다라구")
    _register(db, _xlsx(tmp_path, "(다라구)2025년 상반기 목록.xlsx",
                        {"건의과제": [HEADER, _row(1, "다라구", "외국인관광 도시민박업 대상 주택 요건 완화")]}))
    review = [HEADER + ["부처의견1"], _row(1, "다라구", "규제가 발목잡은 외국인 도시민박업 개선") + ["1. 수용"],
              _row(2, "마1시", "남의 과제 1") + ["1. 수용"], _row(3, "마2시", "남의 과제 2") + ["1. 수용"]]
    _register(db, _xlsx(tmp_path, "(붙임2) 2025년 상반기 부처별 검토결과.xlsx", {"전체": review}))
    rc.restore(db, BASIS, statements=False)

    assert len(matters.for_task(db, 1)) == 2          # 제목이 다르면 합치지 않는다
    check = next(a for a in continuity.next_actions(db, 1) if a["text"].startswith("[건별 진행 확인]"))
    assert ("‘외국인관광 도시민박업 대상 주택 요건 완화’ ↔ ‘규제가 발목잡은 외국인 도시민박업 개선’"
            in check["text"]) and "회신 ‘1. 수용’" in check["text"]


def test_regulation_matters_wait_for_a_fix_not_a_reply(db, tmp_path):
    matters.set_own_locality(db, "다라구")
    _register(db, _xlsx(tmp_path, "제출서식(건설과).xlsx", {"추진목록": [
        ["연번", "광역", "기초", "규제사무명", "소관부서", "정비대상 여부", "정비내용"],
        [1, "가나", "다라구", "도로점용료 납부방법", "건설과", "O", "분할 납부 허용"]]}))
    rc.restore(db, BASIS, statements=False)

    [matter] = matters.for_task(db, 1)
    assert matter["kind"] == "regulation" and matter["stage"] == matters.LISTED   # 정비 판단은 회신이 아니다
    check = next(a for a in continuity.next_actions(db, 1) if a["text"].startswith("[건별 진행 확인]"))
    assert "정비 결과(자치법규 개정·등록 반영)" in check["text"]
