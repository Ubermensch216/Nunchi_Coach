"""건의 자취 — 공문과 그 붙임으로 건의 진행을 잇는다 (규제개혁 개선계획서 P1-1 남은 일).

부처 검토결과 회신의 붙임에는 "□ 안 건 명 : 90. (제목)" 아래 "□ 검토의견 : □수용 ■수용곤란"이
있다. 위원회 심의결과와 의회 의결 알림은 표의 한 행에 (제목) | 개정 / 원안가결이 있다. 이것을
건에 잇되, 제목에서 떨어진 다른 건의 표시를 가져오지 않고, 의결·개정은 시행 근거 후보로만 두는지
본다. 기관·과제 이름은 지어낸 것이다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.core import continuity, matter_trail, matters, reconstruction as rc, task_manual
from app.db import Database

from tests.test_matters import HEADER, _register, _row, _xlsx

BASIS = date(2026, 10, 14)
TITLE = "토지 경계조정 간소화"


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    database.con.execute("INSERT INTO tasks(id, name) VALUES (1, '규제혁신 과제 제출')")
    matters.set_own_locality(database, "다라구")
    _register(database, _xlsx(tmp_path / "자료" / "2025 과제", "(다라구)2025년 상반기 건의과제 목록.xlsx",
                              {"건의과제": [HEADER, _row(1, "다라구", TITLE),
                                           _row(2, "다라구", "가나구 중증장애인생산품 우선구매 촉진")]}))
    yield database
    database.close()


def _doc(db, folder: Path, name: str, lines: list[str], year=2025, task=1) -> int:
    source = db.add_source(str(folder.parent))
    doc = db.upsert_document(source, {
        "path": str(folder / name), "filename": name, "ext": Path(name).suffix, "parse_status": "ok",
        "hash": name, "eff_year": year, "eff_date": f"{year}-07-07", "eff_precision": "day", "eff_date_kind": "body"})
    db.replace_sections(doc, [("paragraph", i, f"{i}문단", line, {}) for i, line in enumerate(lines, 1)])
    if task:
        db.assign_document(task, doc)
    return doc


REPLY = ["행정안전부", "수신 부산광역시장", "제목 2025년 상반기 지자체 규제개선 건의과제 부처협의 결과 회신",
         "1. 관련: 지방규제혁신과-1000", "2. 부처별 검토결과를 붙임과 같이 알려 드립니다.",
         "시행 지방규제혁신과-1234 (2025. 7. 7.) 접수 기획실-8415 (2025. 7. 8.)"]
REVIEW = ["□ 안 건 명 : 90. 토지 경계조정 간소화", "□ 관련법령", "○ 「어느 법」 제1조",
          "□ 건의요지", "○ (개선방안) 경계조정 절차 간소화",
          "□ 검토의견 : □수용 □일부수용 □기시행 □중장기검토 ■수용곤란", "○ 형평성을 고려할 때 신중할 필요",
          "□ 안 건 명 : 91. 남의 과제", "□ 검토의견 : ■수용 □일부수용"]


def test_a_checked_box_in_the_reply_attachment_answers_the_matter(db, tmp_path):
    folder = tmp_path / "자료" / "2025 과제" / "검토결과"
    _doc(db, folder, "2025년 상반기 건의과제 부처협의 결과 회신.pdf", REPLY)
    _doc(db, folder, "(붙임3) 2025년 상반기 건의과제 소관부처 검토결과.hwp", REVIEW)
    totals = rc.restore(db, BASIS, statements=False)
    assert totals["matter_answers"] == 1
    land = next(m for m in matters.for_task(db, 1) if m["title"] == TITLE)
    assert land["stage"] == matters.ANSWERED and land["outcome"] == matters.REJECTED
    line = matters.progress_line(land)
    assert "갈래: 불수용(검토결과 문서의 표시로 판단)" in line
    assert "회신(검토의견) 수용곤란 — 받은 공문 지방규제혁신과-1234(2025.7.7.)" in line
    assert any(e["locator"] == "1문단" and "검토결과" in e["filename"] for e in land["evidence"])
    # 다음 건 머리 뒤의 '■수용'은 이 건의 답이 아니다.
    assert all(m["outcome"] != matters.ACCEPTED for m in matters.for_task(db, 1))
    # 회신이 이어졌으니 '회신이 없다'고 묻지 않는다.
    assert not any(TITLE in a["text"] for a in continuity.next_actions(db, 1)
                   if a["text"].startswith("[건별 진행 확인]"))


def test_a_list_reply_column_still_wins_over_the_attachment(db, tmp_path):
    folder = tmp_path / "자료" / "2025 과제" / "검토결과"
    _register(db, _xlsx(folder, "(붙임2) 부처별 검토결과.xlsx",
                        {"전체": [HEADER + ["부처의견1"], _row(1, "다라구", TITLE) + ["1. 수용"]]}))
    _doc(db, folder, "(붙임3) 검토결과.hwp", REVIEW)
    rc.restore(db, BASIS, statements=False)
    land = next(m for m in matters.for_task(db, 1) if m["title"] == TITLE)
    assert land["outcome"] == matters.ACCEPTED and "회신 글로 판단" in matters.progress_line(land)


def test_a_council_decision_is_a_candidate_for_a_person_to_confirm(db, tmp_path):
    folder = tmp_path / "자료" / "의안"
    _doc(db, folder, "제289회 다라구의회 임시회 제2차 본회의 의결 의안 알림.hwp",
         ["제289회 다라구의회 임시회 의결 의안 알림", "의결결과", "제244호",
          "가나구 중증장애인생산품 우선구매 촉진에 관한 조례안", "원안가결", "제245호", "다른 조례 일부개정조례안", "원안가결",
          "시행 기획실-1304 (2024. 3. 7.) 접수"])
    rc.restore(db, BASIS, statements=False)
    bill = next(m for m in matters.for_task(db, 1) if "중증장애인" in m["title"])
    assert bill["stage"] == matters.LISTED                    # 의결은 진행으로 올리지 않는다
    assert "시행 근거 후보: 의회 의결 원안가결 — 내부 공문" not in matters.progress_line(bill)
    assert "시행 근거 후보: 의회 의결 원안가결 — 보낸 공문 기획실-1304(2024.3.7.)" in matters.progress_line(bill)
    asked = next(a["text"] for a in continuity.next_actions(db, 1) if "개정·의결 자료가 있는 건" in a["text"])
    assert "중증장애인" in asked and "'개선 시행 확인'" in asked
    matters.review(db, bill["id"], stage="implemented")
    assert not any("개정·의결 자료가 있는 건" in a["text"] for a in continuity.next_actions(db, 1))


def test_a_committee_table_row_gives_the_decision(db, tmp_path):
    _doc(db, tmp_path / "자료" / "위원회", "2025년 제1회 규제개혁위원회 심의결과 알림.hwp",
         ["2025년 제1회 규제개혁위원회 심의결과 알림", "연번", "규제명", "심의결과", "4",
          TITLE, "* 어느 조례 시행규칙 제5조", "개정", "5", "남의 규제", "존치"])
    rc.restore(db, BASIS, statements=False)
    land = next(m for m in matters.for_task(db, 1) if m["title"] == TITLE)
    assert land["stage"] == matters.DECIDED
    assert land["trail"][0]["signal"] == matter_trail.DECIDED and land["trail"][0]["result"] == "개정"


def test_a_plan_that_only_names_the_matter_does_not_move_it(db, tmp_path):
    _doc(db, tmp_path / "자료" / "계획", "2026년 규제혁신 추진계획.hwp",
         ["2026년 규제혁신 추진계획", "지난해 건의한 과제: " + TITLE + " 등 3건을 계속 추진"])
    rc.restore(db, BASIS, statements=False)
    land = next(m for m in matters.for_task(db, 1) if m["title"] == TITLE)
    assert land["stage"] == matters.LISTED
    assert [t["signal"] for t in land["trail"]] == [matter_trail.MENTIONED]
    assert land["evidence"][0]["locator"].startswith("'건의과제'")       # 언급은 근거로 늘어놓지 않는다
    assert len(land["evidence"]) == 1


def test_an_ai_report_is_not_part_of_the_trail(db, tmp_path):
    folder = tmp_path / "자료" / "GPT 결과물"
    _doc(db, folder, "건의과제 분석(ChatGPT 작성).hwp", REVIEW)
    rc.restore(db, BASIS, statements=False)
    land = next(m for m in matters.for_task(db, 1) if m["title"] == TITLE)
    assert land["trail"] == [] and land["stage"] == matters.LISTED


def test_the_manual_and_the_review_screen_tell_the_trail(db, tmp_path):
    folder = tmp_path / "자료" / "2025 과제" / "검토결과"
    _doc(db, folder, "2025년 상반기 건의과제 부처협의 결과 회신.pdf", REPLY)
    _doc(db, folder, "(붙임3) 2025년 상반기 건의과제 소관부처 검토결과.hwp", REVIEW)
    rc.restore(db, BASIS, statements=False)
    cases = next(s for s in task_manual.build(db, 1).sections if s.key == "cases")
    assert cases.lines[0].startswith("우리 기관의 건 2건 · 회신이 이어진 것 1건")
    assert any("받은 공문 지방규제혁신과-1234" in line for line in cases.lines)
