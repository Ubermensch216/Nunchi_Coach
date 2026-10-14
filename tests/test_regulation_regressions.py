"""누구의 무엇인가 — 원문에 있는 문장이 후임자의 행동으로 잘못 바뀌지 않는다.

규제개혁 업무 자료의 비교연구(2026-10-14)에서 드러난 오류의 꼴을 옮긴 합성 자료다.
기관·사람·과제 이름은 지어낸 것이다. 개선계획서 8.2의 회귀시험 번호를 함께 적는다.

  C01~C03  전국 건의과제 취합표의 한 칸(남의 건)의 절차·제출서류가 '지금 할 일'이 됐다
  C04      쪽지가 두 위원회를 나란히 설명했는데 다른 위원회의 임기가 이 업무에 붙었다
  C06      AI가 쓴 평가 전략 보고서의 지표 설명이 결재된 계획의 목적을 덮었다
  C07~C08  정기간행물 본문이 신문고 업무의 목적·제출처가 됐고, 운영 매뉴얼은 안내되지 않았다
  C09      서식의 빈칸('0000과', '☎000-000-0000')이 제출처·연락처가 됐다
  C29      마지막 폴더 조사는 실패했는데 이전 자료로 만든 결과가 '준비됨'으로 보였다
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.core import authority, continuity, docrole, initial_analysis, reconstruction as rc, task_manual
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
    db.con.execute("INSERT INTO tasks(name, origin, status, confidence) VALUES (?,'ai','proposed','medium')",
                   (name,))
    return db.con.execute("SELECT MAX(id) FROM tasks").fetchone()[0]


def _doc(db, task_id: int, relative: str, sections: list, year: int = 2025, role: str = "") -> int:
    """sections는 글 조각 목록이거나, (위치, 글, meta) 묶음 목록이다."""
    source_id = db.add_source(ROOT)
    name = relative.rsplit("\\", 1)[-1]
    doc_id = db.upsert_document(source_id, {
        "path": ROOT + "\\" + relative, "filename": name, "ext": Path(name).suffix.lower(),
        "parse_status": "ok", "hash": relative, "eff_year": year,
        "eff_date": f"{year}-03-01", "eff_precision": "day", "eff_date_kind": "body"})
    rows = []
    for index, section in enumerate(sections, start=1):
        locator, text, meta = section if isinstance(section, tuple) else (f"{index}문단", section, {})
        rows.append(("paragraph", index, locator, text, meta))
    db.replace_sections(doc_id, rows)
    db.index_document(doc_id, name, ROOT + "\\" + relative, "",
                      "\n".join(row[3] for row in rows))
    if task_id:
        db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin) VALUES (?, ?, 'ai')", (task_id, doc_id))
    if role:
        db.con.execute("INSERT INTO doc_roles(doc_id, role, basis, confidence, decided_by) "
                       "VALUES (?, ?, '', 'medium', 'ai')", (doc_id, role))
    return doc_id


def _items(db, task_id: int, kind: str) -> list[str]:
    return [item["content"] for item in rc.extract_task(db, task_id, BASIS) if item["kind"] == kind]


_HEADER = "연번\t시도\t시군구\t제목\t주요 건의내용\t관련법령\t소관부처"


def _national_list(rows: int = 40) -> list[tuple]:
    """전국 건의과제 취합표 — 한 행이 한 지자체의 건의 한 건이다."""
    sections = [("'전체' A1:G2", f"[전체]\n2025년 상반기 지자체 규제개선 건의과제 목록\n{_HEADER}",
                 {"sheet": "전체", "rows": [1, 2], "header": _HEADER})]
    for row in range(3, rows + 3):
        body = f"\t{row - 2}\t가도\t나시\t과제 {row}\tㅇ (현황) 일반 현황\t「어느 법」\t어느부"
        if row == 20:
            body = ("\t18\t가도\t다시\t토지 경계조정 간소화\tㅇ (현황) 경계조정에 8단계가 필요함 現) 경계조정 "
                    "절차 : ① 개발행위(토지분할)허가 → ② 지적측량 → ③ 토지이동 정리 → ④ 소유권 이전\t"
                    "「공간정보관리법」\t국토부")
        if row == 30:
            body = ("\t28\t라도\t마시\t외국인근로자 신고 서류 개선\tㅇ (현황) 채용 14일 이내 신고(제출서류: "
                    "외국인등록증 또는 외국인등록사실증명)\t「건강보험법」\t복지부")
        sections.append((f"'전체' A{row}:G{row}", f"[전체]\n{_HEADER}\n{body}",
                         {"sheet": "전체", "rows": [row, row], "header": _HEADER}))
    return sections


# ── C01~C03 남의 건의 절차·서류 ─────────────────────────────────────

def test_a_case_row_of_a_national_list_gives_no_procedure_or_inputs(db):
    task_id = _task(db, "규제혁신 과제 제출")
    _doc(db, task_id, r"2025년\상반기 과제 제출\★2025년 상반기 건의과제 목록_최종.xlsx", _national_list())

    assert _items(db, task_id, rc.PROCEDURE) == []
    assert _items(db, task_id, rc.INPUT) == []


def test_the_current_rule_described_in_a_proposal_is_not_the_workers_procedure(db):
    """긴 목록이 아니어도(우리 구 건의서 한 장) '(현황) 現) 절차 :'는 개선 대상 규제의 절차다."""
    task_id = _task(db, "규제혁신 과제 제출")
    _doc(db, task_id, r"2025년\상반기 과제 제출\건의서(우리 구).hwpx", [
        "제목 : 토지 경계조정 간소화",
        "ㅇ (현황) 경계조정에 8단계가 필요함 現) 경계조정 절차 : ① 분할허가 → ② 지적측량 → ③ 소유권 이전",
        "ㅇ (현황) 채용 후 신고(제출서류: 외국인등록증 또는 외국인등록사실증명)",
    ], role=docrole.SUBMISSION)

    assert _items(db, task_id, rc.PROCEDURE) == []
    assert _items(db, task_id, rc.INPUT) == []


def test_a_workers_own_procedure_and_parenthesised_deadline_still_read(db):
    task_id = _task(db, "규제혁신 과제 제출")
    _doc(db, task_id, r"2026년\과제 발굴 계획.hwpx", [
        "처리 절차 : 부서 발굴 → 소관부서 검토 → 결재 후 제출",
        "구비서류 : 건의서, 과제 목록",
        "과제 제출(제출기한: 2026. 11. 20.)",
    ], year=2026, role=docrole.PLAN)

    assert _items(db, task_id, rc.PROCEDURE) == ["부서 발굴 → 소관부서 검토 → 결재 후 제출"]
    assert _items(db, task_id, rc.INPUT) == ["건의서, 과제 목록"]
    assert "2026-11-20" in _items(db, task_id, rc.DEADLINE)


def test_inputs_from_another_document_are_not_attached_to_a_stated_procedure(db):
    task_id = _task(db, "규제혁신 과제 제출")
    _doc(db, task_id, r"2026년\과제 발굴 계획.hwpx",
         ["처리 절차 : 부서 발굴 → 소관부서 검토 → 결재 후 제출"], year=2026, role=docrole.PLAN)
    _doc(db, task_id, r"2026년\다른 건 안내.hwpx", ["제출서류 : 사업자등록증 사본"], year=2026,
         role=docrole.DIRECTIVE)
    rc.restore(db, BASIS, statements=False)

    steps = [s for s in db.work_steps(task_id) if s["procedure_kind"] == ws.PROCEDURE_STATED]
    assert [s["label"] for s in steps] == ["부서 발굴", "소관부서 검토", "결재 후 제출"]
    assert not any(s["inputs"] for s in steps)
    # 붙이지 않았을 뿐 업무 항목으로는 남는다.
    assert [i["content"] for i in db.task_items(task_id) if i["kind"] == rc.INPUT] == ["사업자등록증 사본"]


def test_a_procedure_read_from_a_past_round_is_checked_before_it_is_run(db):
    task_id = _task(db, "규제혁신 과제 제출")
    _doc(db, task_id, r"2024년\과제 발굴 계획.hwpx",
         ["처리 절차 : 부서 발굴 → 소관부서 검토 → 결재 후 제출"], year=2024, role=docrole.PLAN)
    rc.restore(db, BASIS, statements=False)

    first = continuity.next_actions(db, task_id)[0]
    assert first["type"] == "check"
    assert first["text"].startswith("[현 회차 적용 확인]") and "'부서 발굴'부터" in first["text"]
    assert "진행할 수 있습니다" not in first["text"]

    # 담당자가 이번에도 같다고 확인하면 그때부터 진행 안내가 된다.
    step = next(s for s in db.work_steps(task_id) if s["procedure_kind"] == ws.PROCEDURE_STATED)
    db.con.execute("UPDATE work_steps SET human_state = ? WHERE id = ?", (ws.SUCCESSOR, step["id"]))
    first = continuity.next_actions(db, task_id)[0]
    assert first == {"type": "do", "step_id": step["id"], "gap_id": None,
                     "text": "'부서 발굴' 단계를 진행할 수 있습니다."}


# ── C04~C05 다른 위원회의 임기 ──────────────────────────────────────

_NOTE = ["1. 가나행정위원회는 붙임1 참고하시면 위촉기간이 23년 4월~ 26년 4월까지(3년)이고",
         "2. 규제개혁위원회는 비상설로 회의를 열 때 위촉하고 끝나면 해촉합니다."]


def test_a_term_of_another_committee_is_not_this_committees_expiry(db):
    task_id = _task(db, "규제개혁위원회")
    _doc(db, task_id, r"[규제개혁위원회] (비상설)\[쪽지] 위원회 관련.png", _NOTE, year=2024,
         role=docrole.MEMO)

    assert not any(slot.startswith(rc.EXPIRY_PREFIX) for slot in
                   (item["slot"] for item in rc.extract_task(db, task_id, BASIS)))


def test_the_same_term_belongs_to_the_committee_it_names(db):
    task_id = _task(db, "가나행정위원회 운영")
    # 글자 인식의 오자('가나행졍')도 같은 위원회로 본다.
    _doc(db, task_id, r"[가나행정위원회]\[쪽지] 위원회 관련.png",
         ["1. 가나행졍위원회는 위촉기간이 23년 4월~ 26년 4월까지(3년)"], year=2024, role=docrole.MEMO)

    expiry = [(i["slot"], i["content"]) for i in rc.extract_task(db, task_id, BASIS)
              if i["slot"].startswith(rc.EXPIRY_PREFIX)]
    assert expiry == [("만료 · 위촉기간", "2026년 4월")]


def test_foreign_subject_needs_a_committee_of_the_same_kind_on_this_side():
    assert rc._foreign("가나행정위원회", {"규제개혁위원회"})
    assert not rc._foreign("가나행졍위원회", {"가나행정위원회"})
    assert not rc._foreign("가나행정위원회", set())        # 견줄 것이 없으면 남긴다
    assert not rc._foreign("가나협의회", {"규제개혁위원회"})  # 종류가 다르면 가리지 않는다


# ── C06 AI 생성 보고서 ──────────────────────────────────────────────

def test_an_ai_written_strategy_does_not_become_the_plans_purpose(db):
    task_id = _task(db, "지방규제혁신 추진계획")
    _doc(db, task_id, r"2025년\추진계획\2025년 규제혁신 추진계획(결재).hwpx", [
        "2025년 규제혁신 추진계획",
        "불합리한 규제를 개선하여 주민 불편을 해소하고 지역 경제의 활력을 제고하고자 함.",
    ], role=docrole.PLAN)
    _doc(db, task_id, r"2025년\추진계획\(제출)\[ChatGPT-DeepResearch]2026년 평가 지표별 전략.hwpx", [
        f"목적:지표 {n}의 실적을 평가하는 지표입니다. 배점은 {n}점입니다." for n in range(1, 6)
    ], year=2026, role=docrole.CRITERIA)

    assert _items(db, task_id, rc.PURPOSE) == [
        "불합리한 규제를 개선하여 주민 불편을 해소하고 지역 경제의 활력을 제고하고자 함."]


def test_one_document_listing_many_purposes_gives_none(db):
    task_id = _task(db, "평가 대응")
    _doc(db, task_id, r"2025년\평가 지표 해설.hwpx",
         [f"목적: 지표 {n}의 실적을 평가한다" for n in range(1, 4)], role=docrole.CRITERIA)

    assert _items(db, task_id, rc.PURPOSE) == []


def test_authority_markers_read_only_explicit_tool_and_publication_names():
    assert authority.marker("[ChatGPT-DeepResearch]2026년 전략.hwpx") == authority.AI_GENERATED
    assert authority.marker("report.hwpx", r"D:\자료\gemini 초안\report.hwpx") == authority.AI_GENERATED
    assert authority.marker("0. KIPA 규제동향 25년 봄호 Vol.50.pdf") == authority.PUBLICATION
    assert authority.marker("[국내 이슈 분석] AI 규제정책 방향에 대한 소고.pdf") == authority.PUBLICATION
    # 주제어의 'AI'는 도구 이름이 아니다. 우리 계획서는 걸리지 않는다.
    assert authority.marker("AI 기본법 관련 규제 해소 방안.hwpx") is None
    assert authority.marker("2025년 규제혁신 추진계획.hwpx") is None


# ── C07~C08 발간물과 운영 매뉴얼 ────────────────────────────────────

def _channel(db) -> int:
    task_id = _task(db, "규제개혁신문고")
    _doc(db, task_id, r"2025년\02_규제개혁신문고 사용매뉴얼(지자체용)_v1.pdf",
         ["사용자 등록", "내부망 건의 신청", "접수 건 답변·재답변"], role=docrole.CRITERIA)
    _doc(db, task_id, r"2025년\03_2026년 합동평가 지표매뉴얼(신문고).pdf",
         ["지표 산식"], year=2026, role=docrole.CRITERIA)
    _doc(db, task_id, r"2025년\2. (제출) 신기술 규제 해소 방안\0. 규제동향 25년 봄호 Vol.50.pdf",
         ["머리말"] * 5 + ["목적은 경제안보 분야의 정보 유출 방지이다",
                            "사고가 나면 30일 이내에 금융감독원에 보고한다"])
    return task_id


def test_a_periodical_gives_neither_purpose_nor_recipient(db):
    task_id = _channel(db)
    assert _items(db, task_id, rc.PURPOSE) == []
    assert _items(db, task_id, rc.RECIPIENT) == []


def test_a_recipient_keeps_only_the_organisation_name():
    found = rc.read_section("사고가 나면 30일 이내에 금융감독원에 보고한다", 1, "1문단")
    assert [(f.kind, f.value) for f in found] == [(rc.RECIPIENT, "금융감독원")]
    found = rc.read_section("구·군은 시를 통해 행안부로 공문 제출", 1, "1문단")
    assert [(f.kind, f.value) for f in found] == [(rc.RECIPIENT, "행안부")]


def test_the_operating_manual_is_the_first_thing_to_read(db):
    task_id = _channel(db)
    rc.restore(db, BASIS, statements=False)

    first = continuity.next_actions(db, task_id)[0]
    assert first["type"] == "do"
    # 평가 지표 매뉴얼보다 운영 매뉴얼이 먼저다.
    assert first["text"].index("사용매뉴얼(지자체용)") < first["text"].index("지표매뉴얼")
    assert "(2025년 자료)" in first["text"]
    first_section = next(s for s in task_manual.build(db, task_id).sections if s.key == "first")
    assert first_section.lines[0].startswith("지금 할 일: 이 업무 자료에서 처리 절차를")


# ── C09 서식의 빈칸 ─────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["시·도/시·군·구 0000과", "(☎000-000-0000)", "044-000-0000",
                                   "OO과 ○○○ 주무관", "홍길동", "abc@korea.kr"])
def test_placeholders_are_not_values(value):
    assert authority.is_placeholder(value)


@pytest.mark.parametrize("value", ["행정안전부 지방규제혁신과", "051-220-4563", "02-2100-0000", "2000과제",
                                   "기획실 규제개혁팀"])
def test_real_values_are_not_placeholders(value):
    assert not authority.is_placeholder(value)


def test_blank_form_fields_are_not_recipient_or_contact(db):
    task_id = _task(db, "성과평가")
    _doc(db, task_id, r"2026년\(붙임4) 검토의견 서식.hwpx",
         ["제출기관 : 시·도/시·군·구 0000과", "담당자 : 홍길동 (☎000-000-0000)", "관련법령 : 「관광진흥법」"],
         year=2026, role=docrole.FORM)
    # 서식이 아닌 문서에서도 빈칸은 값이 아니다.
    _doc(db, task_id, r"2026년\제출 안내.hwpx", ["제출처 : OO과", "제출기한 : 2026. 11. 30."],
         year=2026, role=docrole.DIRECTIVE)

    items = rc.extract_task(db, task_id, BASIS)
    assert not [i for i in items if i["kind"] in (rc.RECIPIENT, rc.CONTACT, rc.REGULATION)]
    assert [i["content"] for i in items if i["kind"] == rc.DEADLINE] == ["2026-11-30"]


def test_a_regulation_needs_a_law_name():
    assert [f.value for f in rc.read_section("관련 법령 : 개정안>", 1, "1문단")] == []
    assert [f.value for f in rc.read_section("관련 법령 : 「관광진흥법」 제5조", 1, "1문단")] == ["「관광진흥법」 제5조"]


# ── C29 준비 상태 ───────────────────────────────────────────────────

def _stage(db, name: str, state: str = "completed", note: str = "", errors=()):
    db.con.execute("INSERT OR REPLACE INTO pipeline_stage_results(stage,state,done,total,note,errors) "
                   "VALUES (?,?,0,0,?,?)", (name, state, note, json.dumps(list(errors), ensure_ascii=False)))


def test_a_failed_scan_over_old_results_is_said_plainly(db):
    task_id = _task(db, "규제개혁신문고")
    _doc(db, task_id, r"2025년\안내.hwpx", ["본문"])
    _stage(db, "파일 찾기", "partial", "문서 0건", ["폴더에 접근할 수 없습니다"])
    _stage(db, "업무 파악")
    _stage(db, "의미 색인", "pending", "건너뜀 — Ollama에 연결할 수 없습니다.")
    db.set_meta("analysis_run_state", "completed")

    result = initial_analysis.outcome(db)
    assert result.state == "partial" and result.can_open
    assert result.title == "이전에 읽은 자료로 만든 결과입니다" and "1건" in result.message

    layers = {layer.name: layer for layer in initial_analysis.layers(db)}
    assert list(layers) == ["원본 접근", "본문 추출", "업무 구조", "검색 색인", "사람 검토"]
    assert layers["원본 접근"].state == "failed" and "이전에 읽어 둔 자료" in layers["원본 접근"].detail
    assert layers["검색 색인"].state == "pending" and "글자 검색" in layers["검색 색인"].detail
    assert layers["업무 구조"].state == "partial"
    assert "확인한 것 0건" in layers["사람 검토"].detail


def test_a_good_scan_does_not_claim_stale_results(db):
    task_id = _task(db, "규제개혁신문고")
    _doc(db, task_id, r"2025년\안내.hwpx", ["본문"])
    _stage(db, "파일 찾기", note="문서 1건")
    assert not initial_analysis.stale_sources(db)
    assert initial_analysis.layers(db)[0].state == "done"
