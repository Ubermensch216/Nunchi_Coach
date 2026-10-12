"""실제 인수인계 폴더의 꼴을 본뜬 합성 자료로 분석을 끝까지 돌린다.

make_fixtures의 표본은 파일명에 업무가 들어 있는 깨끗한 자료라서, 번호·날짜 폴더, old·ref·
부서제출 폴더, 스캔본·캡처·압축파일, HWPML, 폴더 밖 낱장이 있는 실제 자료의 문제를 잡지
못했다. 이 시험이 그 틈을 막는다. AI 모델 없이(의미 색인 없이) 돈다 — 폴더 구조만으로
업무가 나와야 한다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core import docrole, schedule  # noqa: E402
from app.db import Database  # noqa: E402
from app.jobs.pipeline import Pipeline  # noqa: E402
from app.tools import make_handover_fixture as fixture  # noqa: E402
from app.tools import task_report  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def analysed(tmp_path_factory, qapp):
    base = tmp_path_factory.mktemp("handover")
    made = fixture.build(base / "자료")
    db = Database(base / "project" / "project.db")
    db.init()
    db.add_source(str(base / "자료"))
    worker = Pipeline(db.path)
    for stage in ("_scan", "_hash", "_parse", "_date", "_discover", "_roles", "_cycles", "_steps", "_restore"):
        getattr(worker, stage)(db)
    yield db, made
    db.close()


def _doc(db: Database, path: Path):
    return db.con.execute("SELECT * FROM documents WHERE path = ?", (str(path),)).fetchone()


def _task_of(db: Database, path: Path) -> str | None:
    row = db.con.execute(
        "SELECT t.name FROM task_docs td JOIN tasks t ON t.id = td.task_id "
        "JOIN documents d ON d.id = td.doc_id WHERE d.path = ?", (str(path),)).fetchone()
    return row["name"] if row else None


def test_folders_decide_the_tasks_without_any_model(analysed):
    db, made = analysed
    names = {row["name"]: row["doc_count"] for row in db.tasks()}
    assert set(names) == {"환경정책 우수부서", "환경정책 실행계획", "환경정책 위원회", "환경정책 법령", "교육"}
    assert db.con.execute("SELECT COUNT(*) FROM tasks t WHERE (SELECT COUNT(*) FROM task_docs d "
                          "WHERE d.task_id = t.id) < 2").fetchone()[0] == 0
    # 지난 회차와 올해 회차는 한 업무다.
    assert _task_of(db, made["old_plan"]) == _task_of(db, made["plan_h2"]) == "환경정책 우수부서"
    assert _task_of(db, made["exec_old"]) == _task_of(db, made["exec_final"]) == "환경정책 실행계획"
    # 폴더 밖 낱장은 이름에 업무가 있을 때만 붙는다.
    assert _task_of(db, made["incentive"]) == "환경정책 우수부서"
    assert _task_of(db, made["loose_unclear"]) is None
    assert db.task_rounds(db.con.execute("SELECT id FROM tasks WHERE name = '환경정책 우수부서'").fetchone()[0])[0]["round"]


def test_everything_the_parsers_could_not_read_still_lands_in_its_task(analysed):
    db, made = analysed
    scan, capture, archive, law = (_doc(db, made[k]) for k in ("scan", "capture", "zip", "law"))
    assert scan["parse_status"] == "empty" and _task_of(db, made["scan"]) == "환경정책 위원회"
    weak = db.con.execute("SELECT confidence, evidence FROM task_docs WHERE doc_id = ?", (scan["id"],)).fetchone()
    assert (weak["confidence"], weak["evidence"]) == ("low", "폴더 위치")
    assert capture["parse_status"] == "skipped"                    # 그림은 읽는 단계(그림·스캔본 읽기)가 따로 있다
    assert (archive["parse_status"], archive["parser"]) == ("partial", "archive")
    body = db.con.execute("SELECT body FROM document_index WHERE doc_id = ?", (archive["id"],)).fetchone()["body"]
    assert "재무과 증빙/매매계약서.pdf" in body
    assert (law["parse_status"], law["parser"]) == ("ok", "hwp")   # HWPML이 'OLE 복합문서가 아님'으로 빠지지 않는다
    assert "위원의 임기는 3년" in db.con.execute(
        "SELECT body FROM document_index WHERE doc_id = ?", (law["id"],)).fetchone()["body"]


def test_roles_tell_the_predecessors_own_work_from_other_peoples_papers(analysed):
    db, made = analysed
    role = lambda key: db.con.execute(  # noqa: E731
        "SELECT role FROM doc_roles WHERE doc_id = ?", (_doc(db, made[key])["id"],)).fetchone()["role"]
    assert role("memo") == docrole.MEMO
    assert role("received") == docrole.RECEIVED
    assert role("meeting") == docrole.MEETING
    task_id = db.con.execute("SELECT id FROM tasks WHERE name = '환경정책 우수부서'").fetchone()["id"]
    first = db.task_reading(task_id)[0]
    assert (first["filename"], "메모" in first["reason"]) == ("우수부서선발－주의사항.txt", True)


def test_the_numbered_folders_are_the_steps_and_old_or_received_papers_are_left_out(analysed):
    db, _made = analysed
    task_id = db.con.execute("SELECT id FROM tasks WHERE name = '환경정책 우수부서'").fetchone()["id"]
    labels = [s["label"] for s in db.task_steps(task_id, 2024)]
    assert labels[:4] == ["선발계획", "부서제출", "1차 심사", "위원회 개최"]
    assert db.task_steps(task_id, 2023)           # 지난 회차는 날짜 폴더 그대로 단계가 된다
    assert [s["label"] for s in db.task_steps(task_id, 2023)][:2] == [
        "2023년 상반기 우수부서 선발 계획", "1차 심사평가 계획"]


def test_dates_the_plans_state_become_deadlines_cycles_and_the_monthly_calendar(analysed):
    db, _made = analysed
    task_id = db.con.execute("SELECT id FROM tasks WHERE name = '환경정책 우수부서'").fetchone()["id"]
    items = {i["slot"]: i["content"] for i in db.task_items(task_id) if i["kind"] == "deadline"}
    assert any("10월 18일" in v for k, v in items.items() if "추천" in k)
    # 남에게 받은 추천서의 사업비(34억)와 이전 판의 99억은 이 업무의 금액이 아니다.
    assert not [i for i in db.task_items(task_id) if i["kind"] == "amount"]
    cycle = db.task_cycle(task_id)
    assert cycle and cycle["months"] == "6"
    entries = schedule.entries(db)
    months = {e.month for e in entries}
    assert {6, 10, 11} <= months
    assert any(e.what.startswith("일시") and e.month == 11 and e.day == 15 for e in entries)   # 회의가 열린 날
    assert all(e.path for e in entries if e.item_id)


def test_the_report_shows_one_proposal_per_real_task(analysed):
    import sqlite3

    db, _ = analysed
    con = sqlite3.connect(db.path)
    con.row_factory = sqlite3.Row
    try:
        report = task_report.build(con, [
            ("2024년 환경정책 우수부서", "우수부서"), ("[0. old]", "우수부서"),
            ("2024년 환경정책 실행계획", "실행계획"), ("환경정책 위원회", "위원회"),
            ("환경정책 법령", "법령"), ("교육", "교육"), ("2024년 환경정책 우수부서 인센티브", "우수부서")])
    finally:
        con.close()
    truth = report["against_truth"]
    assert (truth["max_spread"], truth["max_mixed"]) == (2, 2) or truth["max_spread"] <= 2
    assert report["single_document_tasks"] == 0 and report["task_count"] == 5
