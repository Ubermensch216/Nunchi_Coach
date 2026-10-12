"""문장으로 적힌 일정·기한 읽기와, 그림·스캔본 골라 읽기.

둘 다 실제 인수인계 자료에서 빠져 있던 것이다. 이름표('제출기한:')만 읽었을 때 기한이
3건 나왔고, 스캔한 의결서와 전임자가 남긴 화면 캡처는 본문 없이 지나갔다.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pymupdf
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core import pictures, reconstruction as rc, workstate as ws  # noqa: E402
from app.db import Database  # noqa: E402
from app.ingest import vision  # noqa: E402
from app.ingest.parsers.base import EMPTY, PARTIAL, ParseResult, Section  # noqa: E402
from app.jobs import pipeline as pipeline_module  # noqa: E402
from app.jobs.pipeline import STAGE_PICTURES, Pipeline  # noqa: E402

ROOT = r"D:\전임자\[환경정책]"


@pytest.fixture(scope="session", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    yield database
    database.close()


@pytest.mark.parametrize("budget", [0, 15, pictures.PAGES_PER_RUN, 60, 100, 100000, -1])
def test_picture_settings_show_the_saved_budget_and_save_changes(db, budget):
    from app.ui.views.settings import SettingsDialog

    db.set_meta("vision_pages_per_run", str(budget))
    dialog = SettingsDialog(db)
    try:
        assert dialog.picture_pages.currentData() == max(0, budget)
        # 설정창을 열기만 해서는 이전 PC에서 저장한 값을 덮어쓰지 않는다.
        assert db.get_meta("vision_pages_per_run") == str(budget)
        dialog.picture_pages.setCurrentIndex(dialog.picture_pages.findData(100 if budget <= 0 else 0))
        assert db.get_meta("vision_pages_per_run") == ("100" if budget <= 0 else "0")
    finally:
        dialog.close()
        dialog.deleteLater()
        QApplication.instance().processEvents()


def _read(text: str, year: int | None = 2024, title: str = "") -> list[rc.Found]:
    return rc.read_section(text, 1, "1문단", year, title=title)


# ── 일정·기한 ───────────────────────────────────────────────────────

def test_a_schedule_line_is_a_deadline_named_after_what_is_due():
    found = _read(" 우수부서 추천(전부서) : 6. 17.(월) 한")
    assert [(f.kind, f.slot, f.value, f.when) for f in found] == [
        (rc.DEADLINE, "일정 · 우수부서 추천(전부서)", "6월 17일", date(2024, 6, 17))]
    assert rc.is_schedule(found[0].slot)


@pytest.mark.parametrize("line, value, when", [
    ("□ 부서별 중점과제 제출 : 2024. 5. 27.(월) 한", "2024-05-27", date(2024, 5, 27)),
    ("□ 인센티브 부여계획 알림 : ‘24.7.22.(월)한.", "2024-07-22", date(2024, 7, 22)),
    ("❍ 선발방식 변경 알림(기획실⇒전부서) : 7월 한", "7월", None),
    ("□ 교육 종료 후 상시학습인정 : 2시간 ▷ 11월 초 한", "11월 초", None),
    ("❍ 위원회 심의ㆍ의결 : 2024. 6월 한", "6월", None),
])
def test_dates_with_years_and_vague_months(line, value, when):
    found = _read(line)
    assert len(found) == 1 and (found[0].value, found[0].when) == (value, when)


def test_a_year_is_not_written_in_when_the_document_did_not_write_it():
    found = _read("위원회 개최 : 11. 15.(금) 한", year=None)
    assert (found[0].value, found[0].when) == ("11월 15일", None)


def test_until_sentences_are_told_apart_by_the_document_they_come_from():
    text = ("2. 실행계획(붙임3 활용)을 수립하여 2024. 6. 14.(금)까지 제출하여 주시기 바랍니다.\n"
            "4. 제도의 시행을 희망하는 구·군은 2024. 4. 29.(월)까지 신청(붙임5)해주시기 바랍니다.\n"
            "○ (제출기한) ‘25. 1. 10. (금) 18시까지 행안부로 공문 제출")
    found = _read(text, title="2024년도 운영지침 알림 및 실행계획 등 제출 요청.hwp")
    assert [(f.slot, f.value) for f in found] == [
        ("일정 · 제출 · 운영지침 알림 및 실행계획 등 제출 요청", "2024-06-14"),
        ("일정 · 신청 · 운영지침 알림 및 실행계획 등 제출 요청", "2024-04-29"),
        ("일정 · 제출 · 운영지침 알림 및 실행계획 등 제출 요청", "2025-01-10")]


@pytest.mark.parametrize("line", [
    "공적 기간 : 2023. 11. 1. ~ 2024. 4. 30.",                # 기간
    "❑ 추천기간 : 2024. 6. 4.(화) ~ 6. 17.(월) ▷ 14일간",
    "교육 후 7일 이내 강사평가서 제출",                        # 날짜가 없다
    "사업비 : 513,000원 한도",
])
def test_periods_and_undated_phrases_are_not_deadlines(line):
    assert [f for f in _read(line) if f.kind == rc.DEADLINE] == []


def test_the_day_a_meeting_was_held_is_a_dated_record_of_when_the_work_happened():
    found = _read("❑ 일 시 : 2024. 6. 28.(금) 10:00", title="2024년 제3차 위원회 개최 계획.hwp")
    assert [(f.slot, f.value) for f in found] == [("일정 · 일시 · 위원회 개최 계획", "2024-06-28")]
    # 기간(~)이 적힌 줄은 여전히 읽지 않는다.
    assert _read("❑ 일 시 : 2024. 6. 4.(화) ~ 6. 7.(금)") == [] or all(
        f.value != "2024-06-07" for f in _read("❑ 일 시 : 2024. 6. 4.(화) ~ 6. 7.(금)"))


def test_the_captured_document_list_gives_the_dates_the_work_was_really_done():
    ledger = ("---|---|---|---|---\n"
              "6423 | 2023.04.03 | 2023년 적극행정 중점과제 선정을 위한 자료 요청 | 고01~고31 | 설정회\n"
              "7000 | 2023.04.12 | 2023년 적극행정 중점과제 선정(안) 보고 | 설정회 | 정영란")
    found = _read(ledger, year=2023)
    assert [(f.value, f.slot.split(" · ", 1)[1].startswith("기안 · ")) for f in found] == [
        ("2023-04-03", True), ("2023-04-12", True)]


def test_a_contact_cell_must_hold_a_person_an_office_or_a_phone():
    table = ("머리글: 구분 | 담당자 | 연락처\n"
             "1열: 광역 | 2열: 노정란 팀장 | 3열: 044-205-3293")
    assert [(f.slot, f.value) for f in _read(table) if f.kind == rc.CONTACT] == [
        ("담당자 · 광역", "노정란 팀장"), ("연락처 · 광역", "044-205-3293")]
    kinds = ("머리글: 구분 | 담당자\n1열: 추진분야 | 2열: ◇ 선제적 해결이 필요한 현안 ◇ 혁신·창의적 해법이 필요한 과제")
    assert [f for f in _read(kinds) if f.kind == rc.CONTACT] == []


def test_a_labeled_deadline_is_still_read_by_its_label_once():
    found = _read("제출기한: 2025. 9. 30.(화)까지")
    assert [(f.slot, f.value) for f in found] == [("제출기한", "2025-09-30")]


def test_the_same_line_twice_a_year_is_two_rounds_not_a_conflict():
    found = (_read("우수부서 추천(전부서) : 6. 17.(월) 한")
             + _read("우수부서추천(전부서) : 10. 18.(금) 한")       # PDF 사본은 띄어쓰기가 다르다
             + _read("우수부서 추천(전부서) : 6. 9.(금) 한", year=2023))
    chosen = rc.choose(found, date(2026, 10, 12))
    assert len(chosen) == 1
    assert chosen[0]["content"] == "6월 17일 / 10월 18일"          # 가장 최근 해, 날짜순
    assert chosen[0]["basis_state"] == ws.STATED


def _task_with(db, sections: dict[str, tuple[int, str]]) -> int:
    source_id = db.add_source(ROOT)
    db.con.execute("INSERT INTO tasks(name, origin, status, confidence) VALUES ('우수부서','ai','proposed','medium')")
    task_id = db.con.execute("SELECT id FROM tasks").fetchone()[0]
    for name, (year, text) in sections.items():
        doc_id = db.upsert_document(source_id, {
            "path": ROOT + "\\우수부서\\" + name, "filename": name, "ext": ".hwp",
            "parse_status": "ok", "hash": name, "eff_year": year,
            "eff_date": f"{year}-01-01", "eff_precision": "year", "eff_date_kind": "filename"})
        db.replace_sections(doc_id, [("paragraph", 1, "1문단", text, {})])
        db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin) VALUES (?, ?, 'ai')", (task_id, doc_id))
    return task_id


def test_the_months_a_plan_states_are_when_the_work_recurs(db):
    """달을 모르는 문서뿐이어도, 계획서가 적은 일정이 해마다 같은 달이면 반복이다."""
    task_id = _task_with(db, {
        "2023년 선발 계획.hwp": (2023, "우수부서 추천(전부서) : 6. 9.(금) 한\n위원회 개최 : 6. 30.(금) 한"),
        "2024년 선발 계획.hwp": (2024, "우수부서 추천(전부서) : 6. 17.(월) 한\n위원회 개최 : 6. 28.(금) 한"),
    })
    assert sorted(when.month for _doc, when in rc.schedule_dates(db, task_id)) == [6, 6, 6, 6]

    Pipeline(db.path)._cycles(db)
    cycle = db.task_cycle(task_id)
    assert (cycle["kind"], cycle["months"]) == ("yearly", "6")


def test_last_rounds_schedule_is_precedent_not_an_overdue_deadline(db):
    from app.core import continuity

    task_id = _task_with(db, {"2024년 선발 계획.hwp": (2024, "우수부서 추천(전부서) : 6. 17.(월) 한")})
    basis = date(2026, 10, 12)
    db.sync_task_items(task_id, rc.extract_task(db, task_id, basis))
    items = db.task_items(task_id)
    assert [(i["kind"], i["slot"], i["content"]) for i in items] == [
        (rc.DEADLINE, "일정 · 우수부서 추천(전부서)", "6월 17일")]
    gaps = continuity.find_gaps(db, task_id, basis)
    assert not [g for g in gaps if g["kind"] == "stale"]
    assert not [g for g in gaps if g["gap_key"] == f"missing:{rc.DEADLINE}"]   # 기한을 찾았다


# ── 그림·스캔본 고르기 ──────────────────────────────────────────────

def _file(db, relative: str, status: str, **extra) -> int:
    source_id = db.add_source(ROOT)
    name = relative.rsplit("\\", 1)[-1]
    return db.upsert_document(source_id, {
        "path": ROOT + "\\" + relative, "filename": name, "ext": Path(name).suffix.lower(),
        "parse_status": status, **extra})


def test_only_the_scans_and_captures_worth_reading_are_picked(db):
    capture = _file(db, r"[0. old]\[2023년도]\[위원회]\0. 연간 일정\연간 공문(일정).png", "skipped")
    note = _file(db, r"[0. old]\[2023년도]\[위원회]\[쪽지] 위원회 관련.png", "skipped")
    scan = _file(db, r"위원회\2024년 제3회\심사의결서.pdf", "empty")
    _file(db, r"위원회\2024년 제3회\개최 계획.hwp", "ok")
    lone = _file(db, r"위원회\2024년 제3회\안내문.png", "skipped")
    # 읽지 않는 것들
    _file(db, r"우수사례 공유\3월 소스\타기관 카드뉴스.pdf", "empty")          # 받은 자료
    _file(db, r"위원회\2024년 제3회\old\이전 의결서.pdf", "empty")            # 이전 판
    _file(db, r"교육\강사\신분증(앞).pdf", "empty")                           # 개인정보
    _file(db, r"교육\강사\통장 사본.jpg", "skipped")
    for index in range(6):
        _file(db, rf"위원회\개최 사진\IMG_{index}.jpg", "skipped")            # 사진
    for index in range(6):
        _file(db, rf"홍보\조각{index}.png", "skipped")                        # 그림만 모인 폴더
    _file(db, r"위원회\2024년 제3회\20241126_113857.jpg", "skipped")          # 카메라가 붙인 이름
    _file(db, r"위원회\2024년 제3회\글자 있는 문서.pdf", "ok")                # 이미 읽은 문서
    _file(db, r"위원회\2024년 제3회\읽어 본 스캔.pdf", "empty", parser=vision.PARSER)

    picked = pictures.candidates(db)
    assert [c.doc_id for c in picked] == [capture, note, scan, lone]      # 전임자의 캡처가 먼저
    assert [c.hinted for c in picked] == [True, True, False, False]


def test_a_small_capture_is_enlarged_before_the_model_sees_it(tmp_path):
    """작은 글씨의 화면 캡처를 그대로 보내면 모델이 없는 글자를 지어낸다."""
    small = tmp_path / "쪽지.png"
    pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 300), False).save(small)
    pages, total = vision.render_pages(small)
    assert total == 1
    enlarged = pymupdf.Pixmap(pages[0][1])
    # 그림의 쪽 크기는 포인트로 환산돼 있어(96dpi → 0.75배) 픽셀로는 2배 가까이 커진다.
    # 예전에는 같은 환산 때문에 오히려 원본보다 작게(300픽셀) 보내고 있었다.
    assert enlarged.width >= 700 and enlarged.width * 300 == pytest.approx(enlarged.height * 400, rel=0.01)

    large = tmp_path / "큰그림.png"
    pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 3200, 1600), False).save(large)
    shrunk = pymupdf.Pixmap(vision.render_pages(large)[0][0][1])
    assert shrunk.width == vision.MAX_SIDE


class _Seeing:
    embed_model = gen_model = "fake"

    def _supports(self, capability: str) -> bool:
        return capability == "vision"

    def cancel(self) -> None:
        pass


def test_the_stage_reads_within_its_budget_and_leaves_the_rest_for_next_time(db, tmp_path, monkeypatch):
    root = tmp_path / "자료"
    (root / "위원회").mkdir(parents=True)
    source_id = db.add_source(str(root))
    ids = {}
    for name, status in (("[쪽지] 위원회 관련.png", "skipped"), ("심사의결서.pdf", "empty"),
                         ("빈 스캔.pdf", "empty"), ("참석자 명부.pdf", "empty")):
        path = root / "위원회" / name
        path.write_bytes(name.encode("utf-8"))
        ids[name] = db.upsert_document(source_id, {
            "path": str(path), "filename": name, "ext": path.suffix, "parse_status": status,
            "parser": "pdf" if status == "empty" else None,
            "eff_year": 2024, "eff_date": "2024-01-01", "eff_precision": "year", "eff_date_kind": "filename"})
    db.set_meta("vision_pages_per_run", "3")

    def fake_read(client, path, should_stop=None, max_pages=20):
        if "빈" in Path(path).name:
            return ParseResult(status=EMPTY, parser=vision.PARSER, note="그림에서 읽을 글자를 찾지 못했습니다.")
        return ParseResult(status=PARTIAL, parser=vision.PARSER, note=vision.NOTE, sections=[
            Section("page", 1, "그림", "위촉기간은 23년 4월~26년 4월까지(3년)", {"ocr": True})])

    monkeypatch.setattr(pipeline_module, "project_client", lambda *_: _Seeing())
    monkeypatch.setattr(vision, "read", fake_read)
    reports = []
    worker = Pipeline(db.path)
    worker.stage_done.connect(reports.append)

    worker._pictures(db)

    note = db.document(ids["[쪽지] 위원회 관련.png"])
    assert (note["parse_status"], note["parser"]) == ("partial", vision.PARSER)
    assert note["hash"] and note["eff_date"] is None          # 본문이 생겼으니 시점을 다시 본다
    assert "모델" in note["parse_note"] or "AI" in note["parse_note"]
    body = db.con.execute("SELECT body FROM document_index WHERE doc_id = ?", (note["id"],)).fetchone()
    assert "26년 4월" in body["body"]
    blank = db.document(ids["빈 스캔.pdf"])
    assert (blank["parse_status"], blank["parser"]) == ("empty", vision.PARSER)   # 다시 읽지 않는다
    assert db.document(ids["참석자 명부.pdf"])["parser"] == "pdf"                 # 예산이 다해 남았다
    assert reports[-1].stage == STAGE_PICTURES
    assert reports[-1].note == "읽음 2건 · 글자 없음 1건 · 1건 남음"

    db.set_meta("vision_pages_per_run", "0")                                      # 꺼 둘 수 있다
    worker._pictures(db)
    assert reports[-1].note == "꺼 둠"
