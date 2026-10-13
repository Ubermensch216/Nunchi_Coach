"""Windows 글자 인식으로 그림 읽기, 그리고 인수인계 내용의 채점.

로컬 모델이 그림을 읽지 못하는 PC에서(실측: 글자가 든 그림을 모두 '행과 열이 많은 표'로
보았다) Windows에 든 글자 인식이 같은 캡처를 읽었다. 이 시험은 인식기를 실제로 부르지
않는다 — 인식기가 돌려주는 낱말 상자를 흉내 내어 그 뒤의 처리만 본다.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core import pictures, reconstruction as rc  # noqa: E402
from app.db import Database  # noqa: E402
from app.ingest import vision, winocr  # noqa: E402
from app.jobs import pipeline as pipeline_module  # noqa: E402
from app.jobs.pipeline import Pipeline  # noqa: E402
from app.tools import task_report  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    yield database
    database.close()


def _word(text: str, x: int, y: int, width: int | None = None, height: int = 20) -> list:
    return [text, x, y, width or len(text) * 14, height]


def test_word_boxes_become_lines_and_wide_gaps_become_cells():
    words = [
        # 둘째 줄이 먼저 와도 높이로 줄을 가른다
        _word("6423", 60, 62), _word("2023.04.03", 160, 60), _word("중점과제", 345, 61),
        _word("자료", 410, 61, 30), _word("제출", 446, 61, 30), _word("요청", 482, 61, 30),
        _word("설정회", 900, 60),
        _word("7000", 60, 22), _word("2023.04.12", 160, 20), _word("선정(안)", 345, 21), _word("보고", 420, 21, 30),
    ]
    assert winocr.lines_of(words) == [
        "7000 | 2023.04.12 | 선정(안) 보고",
        "6423 | 2023.04.03 | 중점과제 자료 제출 요청 | 설정회"]
    assert winocr.lines_of([]) == []


def test_the_recognizer_reads_before_any_model_is_asked(tmp_path, monkeypatch):
    picture = tmp_path / "쪽지.png"
    picture.write_bytes(vision.canary_png())
    monkeypatch.setattr(winocr, "_available", True)
    monkeypatch.setattr(winocr, "read_pngs", lambda pngs: ["위원회는 위측기간이 23년 4월~ 26년 4월 까지(3년)"])

    class NoModel:
        gen_model = "none"

        def _supports(self, capability):       # 모델이 그림을 못 읽어도 된다
            return False

        def generate_text(self, *args, **kwargs):
            raise AssertionError("인식기가 읽었으면 모델을 부르지 않는다")

    result = vision.read(NoModel(), picture)
    assert result.ok and result.note.startswith("Windows 글자 인식으로")
    assert result.sections[0].meta == {"ocr": True, "engine": "winocr"}
    assert vision.reader_mark(NoModel()) == f"{vision.READER_RULES}:winocr"

    # 인식기를 부르지 못하면 예전처럼 모델로 넘어간다
    monkeypatch.setattr(winocr, "read_pngs", lambda pngs: None)
    assert vision.read(NoModel(), picture).note.startswith("그림의 글자를 읽으려면")


def test_the_pictures_stage_uses_the_recognizer_without_a_vision_model(db, tmp_path, monkeypatch):
    root = tmp_path / "자료" / "위원회"
    root.mkdir(parents=True)
    source_id = db.add_source(str(tmp_path / "자료"))
    path = root / "[쪽지] 위원회 관련.png"
    path.write_bytes(vision.canary_png())
    doc_id = db.upsert_document(source_id, {
        "path": str(path), "filename": path.name, "ext": ".png", "parse_status": "skipped"})

    class TextOnly:
        embed_model = gen_model = "text-only"

        def _supports(self, capability):
            return False

        def cancel(self):
            pass

    monkeypatch.setattr(winocr, "_available", True)
    monkeypatch.setattr(winocr, "read_pngs", lambda pngs: ["위촉기간이 23년 4월~ 26년 4월까지(3년) 입니다"] * len(pngs))
    monkeypatch.setattr(pipeline_module, "project_client", lambda *_: TextOnly())
    monkeypatch.setattr(vision, "selfcheck", lambda client: (_ for _ in ()).throw(AssertionError("모델 점검 불필요")))
    notes = []
    worker = Pipeline(db.path)
    worker.stage_done.connect(lambda report: notes.append(report.note))

    worker._pictures(db)

    assert notes[-1] == "읽음 1건"
    assert db.document(doc_id)["parse_status"] == "partial"
    assert '"model": "Windows 글자 인식"' in db.get_meta("vision_selfcheck")
    assert pictures.candidates(db, vision.reader_mark(TextOnly())) == []


def test_recognizer_typos_and_capture_debris_do_not_hide_the_dates():
    # 인식기는 '위촉'을 '위측·위족'으로 읽는다
    found = rc.read_section("1. 적극험정위원회는 위측기간이 23년 4월~ 26년 4월 까지(3년) 이므로 그대로이고.", 1, "그림", 2024)
    assert [(f.slot, f.value, f.when) for f in found] == [("만료 · 위촉기간", "2026년 4월", date(2026, 4, 1))]
    # 공문 목록 캡처: 줄 머리의 체크 칸, 날짜 뒤의 아이콘 조각
    ledger = ("口 | 6423 | 2023.04.03 | 4 2023년 적극행정 중점과제 선정을 위한 자료 제출 요청 | 고01~고31 | 설정회\n"
              "口 | 7000 | 2023.04.12 [4 2023년 적극행정 중점과제 선정(안) 보고 | 설정회")
    found = rc.read_section(ledger, 1, "그림", 2023)
    assert [(f.slot, f.value) for f in found] == [
        ("일정 · 기안 · 2023년 적극행정 중점과제 선정을 위한 자료 제출 요청", "2023-04-03"),
        ("일정 · 기안 · 2023년 적극행정 중점과제 선정(안) 보고", "2023-04-12")]


def test_the_report_scores_what_a_handover_needs(db):
    db.con.execute("INSERT INTO tasks(name, origin, status, confidence) VALUES ('환경정책위원회','ai','proposed','medium')")
    task_id = db.con.execute("SELECT id FROM tasks").fetchone()[0]
    context = db.start_work_context()
    db.sync_task_items(task_id, [
        {"kind": rc.PURPOSE, "slot": "목적", "content": "위원회를 개최하고자 함.", "basis_state": "stated",
         "applicability": "past", "evidence": []},
        {"kind": rc.AMOUNT, "slot": "회의참석수당지급", "content": "700,000원(100,000원 × 7명)",
         "basis_state": "stated", "applicability": "past", "evidence": []},
        {"kind": rc.AGENCY, "slot": "협조 부서 · 재무과", "content": "참석수당 지급 의뢰", "basis_state": "stated",
         "applicability": "past", "evidence": []},
        {"kind": rc.DEADLINE, "slot": "만료 · 위촉기간", "content": "2026년 4월", "basis_state": "stated",
         "applicability": "past", "evidence": []},
        {"kind": rc.AGENCY, "slot": "관련 업무 · 우수부서 선발", "content": "폴더", "basis_state": "inferred",
         "applicability": "unknown", "evidence": []},
    ], context, extractor=rc.EXTRACTOR)
    db.replace_task_cycles(task_id, [{"kind": "yearly", "months": "1,6,11", "day_hint": None,
                                      "years_observed": 3, "confidence": "high", "evidence": ""}])
    db.con.commit()

    con = sqlite3.connect(db.path)
    con.row_factory = sqlite3.Row
    try:
        found = task_report.handover(con)
    finally:
        con.close()
    row = found["환경정책위원회"]
    assert (row["purpose"], row["partners"], row["expiry"], row["related"], row["cycle_months"]) == (
        True, ["재무과"], ["2026년 4월"], ["우수부서 선발"], [1, 6, 11])

    scored = task_report.score_handover(found, {
        "위원회": {"purpose": True, "partners": ["재무과"], "amount_contains": ["100,000"],
                   "amount_absent": ["억"], "expiry": ["2026년 4월"], "cycle_months": [6, 11],
                   "memo_lines_min": 3, "gaps_max": 0},
        "없는 업무": {"purpose": True}})
    assert (scored["passed"], scored["total"]) == (7, 9)
    assert [(c["task"], c["field"]) for c in scored["failed"]] == [
        ("환경정책위원회", "memo_lines_min"), ("없는 업무", "purpose")]
