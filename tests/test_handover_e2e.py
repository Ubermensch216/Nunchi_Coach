"""한 벌을 처음부터 끝까지 — 폴더에서 인수인계 내용까지.

실제 전임자 자료의 꼴을 합성 이름으로 줄여 한 벌로 만들고, 업무 파악 → 갈래 → 일정 →
처리 순서 → 복원을 차례로 돌린 뒤 보고 도구의 요약으로 확인한다. 꼴마다의 규칙은
test_handover_precision.py가 따로 본다. 여기서는 그것들이 함께 돌아 한 업무의 인수인계
내용이 되는지를 본다.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ai.discover import discover  # noqa: E402
from app.core import bundles, reconstruction as rc  # noqa: E402
from app.db import Database  # noqa: E402
from app.jobs.pipeline import Pipeline  # noqa: E402
from app.tools import task_report  # noqa: E402

ROOT = r"D:\전임자\[환경정책]"
BASIS = date(2026, 10, 13)
AWARD, COMMITTEE, REVIEW = "환경정책 우수부서", "환경정책위원회", "환경정책 종합평가"

_PLAN = [
    "등록번호 기획실-16857", "2024년 하반기 환경정책 우수부서 선발 계획",
    "우수부서 선발 및 인센티브 제공으로 성과를 창출한 부서를 우대하여 적극적인 공직문화를 조성하고자 함.",
    "선발인원 : 1차 심사평가 통과 부서 전원 ※변경",
    "우수부서 추천(전부서) : 10. 18.(금) 한",
    "우수부서 인센티브 부여 요청(총무과) : 12월 초",
    "행정사항", "붙임 추천서 서식 1부. 끝."]
_CASES = [f"우수사례 {n}\n사례명: 녹지 사업 {n}\n추진배경: 민원 증가\n사업비 : {n * 7}억원\n추진성과: 만족도 향상"
          for n in range(1, 8)]

TREE: list[tuple[str, list[str], str]] = [
    # 올해 회차 — 번호 폴더
    (rf"2024년 {AWARD}\하반기\1. 선발계획\2024년 하반기 우수부서 선발 계획.hwp", _PLAN, "2024-10-02"),
    (rf"2024년 {AWARD}\하반기\2. 부서제출\우수부서 추천서(녹지과).hwp", ["추천서", "사업비 : 34억원"], "2024-10-17"),
    (rf"2024년 {AWARD}\하반기\2. 부서제출\우수부서 추천서(재무과).hwp", ["추천서", "사업비 : 5억원"], "2024-10-18"),
    (rf"2024년 {AWARD}\하반기\3. 1차 심사\추천 현황(우수사례).hwp", _CASES, "2024-10-25"),
    (rf"2024년 {AWARD}\하반기\4. 위원회 개최\평가 집계.xlsx", ["집계"], "2024-11-12"),
    (rf"2024년 {AWARD}\하반기\5. 결과보고\선발 결과 보고.hwp", ["선발 결과를 보고함"], "2024-11-14"),
    (rf"2024년 {AWARD}\우수부서선발－주의사항.txt",
     ["1. 추천서 서식 보완\n- 해당 업무 수행기간 기입 칸 마련\n2. 발표자 시간제한 타이머 운영 : 3분 초과하면 알람"
      "\n3. 신관 2층 중회의실이라고 하면 제2청사 갈 수 있으니 주의"], "2024-06-28"),
    # 지난 회차 — 날짜 폴더
    (rf"[0. old]\[2023년도]\[{AWARD}]\1. [2023-10-05] 선발 계획\2023년 하반기 우수부서 선발 계획.hwp",
     ["우수부서 추천(전부서) : 10. 20.(금) 한"], "2023-10-05"),
    (rf"[0. old]\[2023년도]\[{AWARD}]\2. [2023-10-20] 추천 요청\우수부서 추천 요청.hwp", ["추천 요청"], "2023-10-20"),
    (rf"[0. old]\[2023년도]\[{AWARD}]\3. [2023-11-10] 결과 보고\선발 결과 보고.hwp", ["결과"], "2023-11-10"),
    # 위원회 — 선발을 심의한다
    (rf"{COMMITTEE}\2024년 제3회 환경정책위원회(하반기 우수부서 선발)\제3차 위원회 개최 계획.hwp",
     ["등록번호 기획실-17001", "위원 회의참석수당 지급 : 700,000원(100,000원 × 7명)",
      "추진절차 : 추천→1차 심사→위원회 개최→결과보고"], "2024-11-01"),
    (rf"{COMMITTEE}\2024년 제3회 환경정책위원회(하반기 우수부서 선발)\제3차 위원회 개최 결과 보고.hwp", ["결과"], "2024-11-13"),
    (rf"{COMMITTEE}\2023년 제2회 환경정책위원회\제2차 위원회 개최 계획.hwp", ["계획"], "2023-11-06"),
    (rf"{COMMITTEE}\2023년 제2회 환경정책위원회\제2차 위원회 개최 결과 보고.hwp", ["결과"], "2023-11-14"),
    (rf"{COMMITTEE}\환경정책위원회 명단(2025.1.).hwp", ["성명 | 생년월일 | 연락처"], "2025-01-03"),
    # 종합평가 — 받은 안내, 낸 보고서
    (rf"{REVIEW}\ref\2025년 지자체 환경정책 종합평가 안내.hwpx",
     ["○ (제출기한) 실적보고서를 2025. 1. 10.(금) 18시까지 행안부 공무원단체과로 공문 제출"], "2024-07-05"),
    (rf"{REVIEW}\제출\종합평가 추진실적 보고서.hwpx", ["실적"], "2025-01-09"),
    (rf"{REVIEW}\ref\2024년 지자체 환경정책 종합평가 계획.hwpx", ["전년도 계획"], "2023-05-26"),
    # 참고 묶음, 낱장
    (r"관련 법령\환경정책 운영 조례.hwp", ["제1조"], "2023-11-10"),
    (r"관련 법령\환경정책 운영규정.hwp", ["제1조"], "2023-06-27"),
    ("★구군 담당자 명단(24.09.기준).xlsx", ["기관명 | 성명 | 연락처"], "2024-09-11"),
    ("(24년 상) 규제애로 해소 사례 제출 건수.xlsx", ["건수"], "2024-08-13"),
    ("상반기 시도 책임관회의 자료.hwpx", ["회의 자료"], "2024-05-28"),
]


@pytest.fixture(scope="session", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def analysed(tmp_path: Path):
    db = Database(tmp_path / "p.db")
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
    worker = Pipeline(db.path)
    for stage in ("_roles", "_cycles", "_steps"):
        getattr(worker, stage)(db)
    rc.restore(db, BASIS, statements=False)
    db.con.commit()
    yield db
    db.close()


def test_a_folder_becomes_what_a_successor_needs_to_be_told(analysed):
    db = analysed
    con = sqlite3.connect(db.path)
    con.row_factory = sqlite3.Row
    try:
        found = task_report.handover(con)
    finally:
        con.close()

    assert set(found) == {AWARD, COMMITTEE, REVIEW, "관련 법령", bundles.LOOSE_NAME}
    scored = task_report.score_handover(found, {
        AWARD: {
            "purpose": True, "cycle_months": [10, 11], "partners": ["총무과"],
            "memo_lines_min": 2, "left_undone_min": 2, "changes_min": 1,
            "related": [COMMITTEE], "amount_absent": ["억"], "steps_min": 5, "bare_steps_max": 0},
        COMMITTEE: {
            "amount_contains": ["100,000"], "related": [AWARD], "rosters_min": 1, "care": ["생년월일"]},
        REVIEW: {"recipients": ["공무원단체과"], "outputs_min": 1, "cycle_months": [1]},
        "관련 법령": {"steps_max": 0, "gaps_max": 0},
        bundles.LOOSE_NAME: {"rosters_min": 1, "steps_max": 0, "gaps_max": 0},
    })
    assert scored["failed"] == []

    tasks = {row["name"]: row["id"] for row in db.tasks()}
    # 위원회 자료에 인용된 선발 절차는 선발 업무의 '적힌 절차'가 된다
    stated = [step["label"] for step in db.work_steps(tasks[AWARD]) if step["procedure_kind"] == "stated"]
    assert stated == ["추천", "1차 심사", "위원회 개최", "결과보고"]
    assert not [s for s in db.work_steps(tasks[COMMITTEE]) if s["procedure_kind"] == "stated"]
    # 선례는 번호 폴더 그대로이고, 받은 추천서는 그 단계의 입력이다
    precedent = [step for step in db.work_steps(tasks[AWARD]) if step["procedure_kind"] == "precedent"]
    assert [step["label"].split(" (")[0] for step in precedent] == [
        "선발계획", "부서제출", "1차 심사", "위원회 개최", "결과보고"]
    assert "추천서(녹지과)" in precedent[1]["inputs"] and not precedent[1]["output"]
    # 한 해 치뿐인 평가도 받은 안내의 기한으로 '해마다 1월 10일까지'가 된다
    cycle = db.task_cycle(tasks[REVIEW])
    assert (cycle["months"], cycle["day_hint"], cycle["confidence"]) == ("1", "10일까지", "low")
