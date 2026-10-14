"""절차·완료 기준·평가 구조 (규제개혁 개선계획서 P1-3, 회귀 C10~C12·C17~C19·C28).

  · 안내문의 '□ 추진방법'은 절차다. 단계마다 하는 기관이 다르면 우리 단계만 행동이 된다.
  · '□ 지자체 협조'는 우리에게 요청한 일이고, 우리 단계의 완료 기준이다.
  · 평가 문서는 평가·실적 해, 초안 여부, 배점을 따로 본다. 배점이 맞지 않으면 확정본 전 채점 보류.
  · 같은 일의 기한이 받는 곳마다 다르면(시 취합 11.4, 중앙 11.7) 따로 둔다.
문서 내용은 실제 꼴을 옮긴 합성이다.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.core import continuity, docrole, docyears, evaluation, guidance, reconstruction as rc, task_manual, workcard
from app.core import workstate as ws
from app.db import Database

ROOT = r"D:\전임자\[규제개혁]"
BASIS = date(2026, 10, 14)


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    yield database
    database.close()


def _task(db, name: str) -> int:
    db.con.execute("INSERT INTO tasks(name, origin, status, confidence) VALUES (?,'ai','proposed','medium')", (name,))
    return db.con.execute("SELECT MAX(id) FROM tasks").fetchone()[0]


def _doc(db, task_id, relative, sections, year=2025, role=""):
    source = db.add_source(ROOT)
    name = relative.rsplit("\\", 1)[-1]
    doc = db.upsert_document(source, {
        "path": ROOT + "\\" + relative, "filename": name, "ext": Path(name).suffix.lower(), "parse_status": "ok",
        "hash": relative, "eff_year": year, "eff_date": f"{year}-06-20", "eff_precision": "day", "eff_date_kind": "body"})
    rows = [(("paragraph", i, s[0], s[1], {}) if isinstance(s, tuple) else ("paragraph", i, f"{i}문단", s, {}))
            for i, s in enumerate(sections, start=1)]
    db.replace_sections(doc, rows)
    db.index_document(doc, name, ROOT + "\\" + relative, "", "\n".join(r[3] for r in rows))
    if task_id:
        db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin) VALUES (?, ?, 'ai')", (task_id, doc))
    if role:
        db.con.execute("INSERT INTO doc_roles(doc_id, role, basis, confidence, decided_by) VALUES (?, ?, '', 'medium', 'ai')",
                       (doc, role))
    return doc


_GUIDE = [
    "자치법규 등록규제 오류정보 정비 추진 협조",
    "□ 추진방법",
    "오류자료 추출 ➡ 지자체 규제정보 정비 ➡ 모니터링 ➡ 프로그램 개선",
    " ❶ (오류자료 추출) 법제처 API 연계를 통해 오류자료 추출",
    " ❷ (지자체 규제정보 정비) 시도/새올 행정시스템을 통해 등록규제 일괄 정비",
    "□ 지자체 협조",
    " ○ 등록규제 파일(엑셀)에서 오류로 체크된 경우 확인 및 수정, 조치결과 입력, 결과제출",
    "    * [별첨] 등록규제 오류파일",
    "□ 추진일정",
    " ○ (지자체)시도/새올 시스템을 통한 등록규제 정비 추진('25. 7월 ~ '25. 8월)",
    " ○ (행안부)정비현황 모니터링(수시) 및 규제정보 검증('25. 8월~)",
]


def _sections(lines):
    return [{"locator": f"{i}문단", "text": line} for i, line in enumerate(lines, start=1)]


# ── 안내문의 절차와 하는 기관 (C17·C18) ─────────────────────────────

def test_guidance_steps_carry_who_does_them():
    read = guidance.read(_sections(_GUIDE), own="가나구")
    assert [(guidance.labelled(s), s.actor) for s in read.steps] == [
        ("오류자료 추출", ""),                       # 적혀 있지 않으면 짐작하지 않는다
        ("지자체 규제정보 정비", guidance.OURS),
        ("(행안부) 모니터링", "행안부"),
        ("프로그램 개선", "")]
    assert [d for d, _ in read.duties] == ["등록규제 파일(엑셀)에서 오류로 체크된 경우 확인 및 수정, 조치결과 입력, 결과제출"]


@pytest.mark.parametrize("lines", [
    ["□ 추진방법", "역할 강화 *(예시) 규제개혁위원회 운영 조례 → 행정규제 혁신조례"],   # 이름 바꿈
    ["□ 추진방법", "계획 수립 → 시행"],                                            # 둘짜리
    ["□ 추진체계", "구청장 → 부구청장 → 기획실장 → 규제개혁팀"],                     # 조직도
])
def test_arrows_that_are_not_a_procedure(lines):
    assert guidance.read(_sections(lines)).steps == []


def test_duties_are_ours_only_when_addressed_to_us():
    ours = ["□ 지자체 협조", " ○ 정비 결과 제출"]
    generic = ["□ 협조사항", " ○ 정비 결과 제출"]
    assert guidance.read(_sections(ours)).duties
    assert guidance.read(_sections(generic)).duties == []                  # 우리 계획서의 부서 협조
    assert guidance.read(_sections(generic), received=True).duties          # 받은 공문의 협조사항
    noisy = ["□ 지자체 협조", " ○ 소관 부서는 관리카드 작성 및 제출", " ○ 17개 시·도 → 행정안전부 제출",
             " ○ 담당자 이메일(a@b.kr) 또는 팩스(FAX 220-0000)", " ○ 정비 결과 제출"]
    assert [d for d, _ in guidance.read(_sections(noisy)).duties] == ["정비 결과 제출"]


def test_only_our_step_is_an_action_and_the_duty_is_its_completion(db):
    task = _task(db, "등록규제 일제정비")
    _doc(db, task, r"2026년\등록규제 오류정보 정비 추진 협조(행안부).hwpx", _GUIDE, year=2026, role=docrole.DIRECTIVE)
    rc.restore(db, BASIS, statements=False)

    steps = [s for s in db.work_steps(task) if s["procedure_kind"] == ws.PROCEDURE_STATED]
    assert [(s["label"], s["actor"]) for s in steps] == [
        ("오류자료 추출", ""), ("지자체 규제정보 정비", guidance.OURS), ("(행안부) 모니터링", "행안부"), ("프로그램 개선", "")]
    assert steps[1]["done_criteria"].startswith("요청받은 일: 등록규제 파일(엑셀)에서")

    actions = continuity.next_actions(db, task)
    assert actions[0]["type"] == "check" and actions[0]["text"].startswith("[앞 단계 확인] '오류자료 추출'")
    assert actions[1] == {"type": "do", "step_id": steps[1]["id"], "gap_id": None,
                          "text": "'지자체 규제정보 정비' 단계를 진행할 수 있습니다."}
    assert not any("모니터링' 단계를 진행" in a["text"] for a in actions)


def test_a_labelled_procedure_beats_the_guidance_flow(db):
    task = _task(db, "등록규제 일제정비")
    _doc(db, task, r"2026년\등록규제 오류정보 정비 추진 협조(행안부).hwpx", _GUIDE, year=2026, role=docrole.DIRECTIVE)
    _doc(db, task, r"2026년\정비 계획.hwpx", ["처리 절차 : 대상 추출 → 부서 검토 → 새올 수정 → 결과 제출"],
         year=2026, role=docrole.PLAN)
    rc.restore(db, BASIS, statements=False)
    assert [s["label"] for s in db.work_steps(task) if s["procedure_kind"] == ws.PROCEDURE_STATED] == [
        "대상 추출", "부서 검토", "새올 수정", "결과 제출"]


# ── 받는 곳마다 다른 기한 (C28) ─────────────────────────────────────

def test_city_and_ministry_deadlines_are_kept_apart():
    text = ("2. 구․군에서는 실적서 및 증빙자료를 작성하여 11.4.(월)까지(기한엄수) 부산시로 제출하여 주시기 바랍니다.\n"
            "   ※ 행안부 제출기한(11.7.) 지연 시, 감점 혹은 평가 대상에서 제외")
    found = rc.read_section(text, 1, "1문단", 2024, title="성과평가 실적자료 제출 요청.odt")
    assert [(f.slot, f.value) for f in found if f.kind == rc.DEADLINE] == [
        ("일정 · 제출(부산시) · 성과평가 실적자료 제출 요청", "11월 4일"),
        ("일정 · 제출기한(행안부) · 성과평가 실적자료 제출 요청", "11월 7일")]


# ── 평가·실적의 해 (C10·C11) ───────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("2024년도구·군규제혁신실적을평가하여지역주도의", {("performance", 2024)}),
    ("2025년구·군규제혁신실적평가계획", {("evaluation", 2025)}),
    ("❍ (실적기간) 2024. 1. 1. ~ 12. 31.", {("performance", 2024)}),
    ("2026년(25실적) 지방규제혁신 성과평가", {("performance", 2025)}),
    ("24년 2025년 실적기간 ’22년 실적", set()),                  # 비교표 칸 머리
])
def test_years_of_an_evaluation(text, expected):
    found = {(m.kind, m.year) for m in docyears.read(text)}
    assert expected <= found and not ({("evaluation", 2024), ("performance", 2025)} & found - expected)


def test_a_labelled_performance_period_beats_loose_mentions(db):
    doc = _doc(db, None, r"평가\2025년 구군 규제혁신 실적평가 계획.pdf",
               ["2025년구·군규제혁신실적평가계획 24년 2025년 실적기간 2025년 실적 비교",
                "❍ (실적기간) 2024. 1. 1. ~ 12. 31."], year=2025)
    marks = {(m.kind, m.year) for m in docyears.collect(db, doc)}
    assert ("evaluation", 2025) in marks and ("performance", 2024) in marks and ("performance", 2025) not in marks


def test_year_rules_are_rerun_once_when_they_change(db):
    _doc(db, None, r"평가\(실적기간) 문서.hwpx", ["❍ (실적기간) 2024. 1. 1. ~ 12. 31."])
    assert docyears.refresh_outdated(db) == 1
    assert docyears.refresh_outdated(db) == 0


# ── 평가 구조와 배점 (C11·C12) ─────────────────────────────────────

def _row(table, row, cells, head=None):
    text = (f"첫 행: {' | '.join(head)}\n" if head else "") + " | ".join(f"{c}열: {v}" for c, v in enumerate(cells, 1))
    return (f"Contents/section0.xml · 표 {table} · {row}행", text)


_HEAD = ["연번", "평 가 지 표", "배 점"]


def _draft(cap: bool = True):
    rows = [("1문단", "2026년 구·군 규제혁신 평가(안)"),
            _row(2, 1, _HEAD), _row(2, 2, ["1", "추진계획 수립", "5"], _HEAD),
            _row(2, 3, ["2", "내부 추진체계 강화", "16"], _HEAD), _row(2, 4, ["합 계", "합 계", "21"], _HEAD)]
    if cap:
        rows.append(("5문단", " ▪ 세부항목의 합산 점수는 세부지표에 할당된 배점을 넘을 수 없음"))
    rows += [_row(10, 2, ["지표", "01. 내부 추진체계 강화", "01. 내부 추진체계 강화"]),
             _row(10, 5, ["진단 내용", "내부 교육, 포상, 홍보 실적 등", "16점"]),
             _row(10, 6, ["항목 설명", "<배점기준>\nㅇ 교육 이수(3점)\n  ‣ 이수율 35% 이상 시 2점\nㅇ 포상 실적(5점)\n"
                                    "ㅇ 홍보 실적(5점)\nㅇ 기관장 관심도(5점)", "같음"])]
    return rows


def test_an_evaluation_draft_is_read_with_its_years_and_points(db):
    task = _task(db, "성과평가")
    doc = _doc(db, task, r"2025년\2026년(25실적) 지방규제혁신 성과평가\2026년 구·군 규제혁신 평가(안).hwpx",
               _draft(), year=2026)
    docyears.mark_documents(db, [doc])
    reading = evaluation.read(db, doc)
    assert (reading.evaluation_year, reading.performance_year, reading.draft) == (2026, 2025, True)
    assert reading.indicators == [("추진계획 수립", 5.0), ("내부 추진체계 강화", 16.0)]
    assert reading.declared_total == 21.0
    [check] = reading.checks
    assert (check.indicator, check.declared, check.total) == ("01. 내부 추진체계 강화", 16.0, 18.0)

    rc.restore(db, BASIS, statements=False)
    items = {i["slot"]: i for i in db.task_items(task) if i["kind"] == rc.EVALUATION}
    structure = items["평가 · 2026년 구·군 규제혁신 평가(안).hwpx"]
    assert structure["content"] == "2026년 평가 · 2025년 실적 · 초안(안) — 확정본 아님 · 지표 2개 배점 합 21점 · 표의 합계 21점"
    capped = items["배점 확인 · 01. 내부 추진체계 강화"]
    assert capped["content"].endswith("— 문서의 상한 규정대로라면 16점이 상한")
    assert capped["basis_state"] == ws.INFERRED            # 문서가 스스로 상한을 밝혔다

    gap = next(g for g in db.info_gaps(task) if g["gap_key"] == "evaluation:confirm")
    assert gap["detail"] == "평가 기준이 초안(안)입니다. 배점이 맞지 않는 지표도 1개 있습니다."
    assert "자체 점수를 매기지 마세요" in gap["next_action"]
    section = next(s for s in task_manual.build(db, task).sections if s.key == "evaluation")
    assert any("16점이 상한" in line for line in section.lines)


def test_points_that_disagree_without_a_cap_rule_are_a_conflict(db):
    task = _task(db, "성과평가")
    doc = _doc(db, task, r"평가\2026년 구·군 규제혁신 평가계획.hwpx", _draft(cap=False), year=2026)
    docyears.mark_documents(db, [doc])
    [item] = [i for i in evaluation.items(db, task, BASIS) if evaluation.is_check(i)]
    assert item["basis_state"] == ws.CONFLICT and "상한" not in item["content"]


def test_our_own_score_sheet_is_not_an_evaluation_plan():
    assert not evaluation.is_evaluation("1. 2025년 구·군 규제혁신 실적평가표(사하구).hwp")
    assert evaluation.is_evaluation("2025년 구군 규제혁신 실적평가 계획.pdf")
    assert evaluation.is_evaluation("2026년 구·군 규제혁신 평가(안).hwpx")


def test_other_works_get_no_evaluation_section(db):
    task = _task(db, "홍보")
    _doc(db, task, r"홍보\보도자료 계획.hwpx", ["보도 계획"], role=docrole.PLAN)
    rc.restore(db, BASIS, statements=False)
    assert "evaluation" not in {s.key for s in task_manual.build(db, task).sections}
