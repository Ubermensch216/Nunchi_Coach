"""틀린 값을 내지 않는다 — 남의 사례 금액, 조각 회차의 선례, 남의 절차.

실제 인수인계 자료에서 금액 항목 15건 가운데 14건이 부서 우수사례·중점과제의 사업비였고,
'지금 할 일'의 선례는 발표자료 넉 장뿐인 최근 해에서, 위원회 업무의 '적힌 절차'는 선발
업무의 것에서 왔다. 꼴만 옮긴 합성 자료다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.core import bundles, casedocs, continuity, docrole, foldersteps, handover_notes, relations, reconstruction as rc, span, statements, task_manual, timeline
from app.db import Database

ROOT = r"D:\전임자\[환경정책]"
BASIS = date(2026, 10, 13)


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


def _doc(db, task_id: int, relative: str, sections: list[str], year: int = 2024, role: str = "",
         status: str = "ok") -> int:
    source_id = db.add_source(ROOT)
    name = relative.rsplit("\\", 1)[-1]
    doc_id = db.upsert_document(source_id, {
        "path": ROOT + "\\" + relative, "filename": name, "ext": Path(name).suffix.lower(),
        "parse_status": status, "hash": relative, "eff_year": year,
        "eff_date": f"{year}-01-01", "eff_precision": "year", "eff_date_kind": "filename"})
    db.replace_sections(doc_id, [("paragraph", index, f"{index}문단", text, {})
                                 for index, text in enumerate(sections, start=1)])
    db.index_document(doc_id, name, ROOT + "\\" + relative, "", "\n".join(sections))
    if task_id:
        db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin) VALUES (?, ?, 'ai')", (task_id, doc_id))
    if role:
        db.con.execute("INSERT INTO doc_roles(doc_id, role, basis, confidence, decided_by) "
                       "VALUES (?, ?, '', 'medium', 'ai')", (doc_id, role))
    return doc_id


def _amounts(db, task_id: int) -> list[tuple[str, str]]:
    return [(item["slot"], item["content"]) for item in rc.extract_task(db, task_id, BASIS)
            if item["kind"] == rc.AMOUNT]


_CASES = [f"우수사례 {n}\n사례명: 사업 {n}\n추진배경: 민원 증가\n사업비 : {n * 7}억원\n추진성과: 만족도 향상"
          for n in range(1, 8)]


# ── 금액 ────────────────────────────────────────────────────────────

def test_a_collection_of_other_peoples_cases_is_told_apart_from_the_works_own_paper():
    assert casedocs.is_case_collection("\n".join(_CASES))
    assert not casedocs.is_case_collection("소요예산 : 513,000원\n강사수당 230,000원 할증료 69,000원")
    # 사례를 말하지만 금액이 없는 선발 계획서는 사례 모음이 아니다
    assert not casedocs.is_case_collection("우수사례 추천 " * 20)


def test_amounts_of_the_cases_being_judged_are_not_the_cost_of_the_work(db):
    task_id = _task(db, "우수부서 선발")
    _doc(db, task_id, r"우수부서\추천 현황(우수사례).hwp", _CASES, role=docrole.OTHER)
    _doc(db, task_id, r"우수부서\위원회 개최 계획.hwp",
         ["위원 회의참석수당 지급 : 700,000원(100,000원 × 7명)"], role=docrole.PLAN)
    _doc(db, task_id, r"우수부서\발표자료(녹지과).pptx", ["소요예산 : 16백만원"], role=docrole.MEETING)

    assert _amounts(db, task_id) == [("회의참석수당지급", "700,000원(100,000원 × 7명)")]


def test_one_label_with_several_amounts_in_a_document_is_a_list_not_a_value(db):
    task_id = _task(db, "중점과제")
    _doc(db, task_id, r"중점과제\선정(안) 보고.hwp",
         ["사업비 : 총352억원", "사업비 : 43,500천원", "소요예산 : 3,000천원"], role=docrole.PLAN)

    assert _amounts(db, task_id) == [("소요예산", "3,000천원")]


def test_a_project_amount_counts_only_in_the_overview_of_its_own_plan(db):
    task_id = _task(db, "도서관 건립")
    _doc(db, task_id, r"도서관 건립\건립 계획.hwp",
         ["총사업비 : 120억원", "위치 : 하단동", "기간 : 2024~2026", "내용 : 지상 3층", "행정사항 : 없음"],
         role=docrole.PLAN)
    other = _task(db, "과제 선정")
    _doc(db, other, r"과제 선정\선정 계획.hwp",
         ["추진근거 : 운영지침", "선정기준 : 주민체감도", "선정절차 : 5월", "행정사항 : 알림",
          "참고 전년도 추진현황", "사업비 : 60억원"], role=docrole.PLAN)

    assert _amounts(db, task_id) == [("총사업비", "120억원")]
    assert _amounts(db, other) == []


def test_the_same_amount_with_a_trailing_note_is_one_value_and_plan_versus_revision_stays_visible(db):
    task_id = _task(db, "직원 교육")
    _doc(db, task_id, r"교육\실시 계획.hwp",
         ["소요예산 : 513,000원", "소요예산 : 513,000원 ※소요 경비(실비)에 따라 변동 가능"], role=docrole.PLAN)
    assert _amounts(db, task_id) == [("소요예산", "513,000원")]

    _doc(db, task_id, r"교육\실시 계획(수정).hwp", ["소요예산 : 600,000원"], role=docrole.PLAN)
    item = next(i for i in rc.extract_task(db, task_id, BASIS) if i["kind"] == rc.AMOUNT)
    assert (item["content"], item["basis_state"]) == ("513,000원 / 600,000원", "conflict")


def test_spaced_and_payment_labels_are_read(db):
    task_id = _task(db, "위원회")
    _doc(db, task_id, r"위원회\참석수당 지급 의뢰.hwp", ["2) 지 급 액 : 금600,000원(금육십만원정)"],
         role=docrole.EXPENSE)
    _doc(db, task_id, r"위원회\변호인 보수 지급.hwp", ["지급금액 : 금3,300,000원"], role=docrole.EXPENSE)

    assert _amounts(db, task_id) == [("지급금액", "금3,300,000원"), ("지급액", "금600,000원(금육십만원정)")]


# ── 받은 자료 ───────────────────────────────────────────────────────

def test_an_unpacked_archive_and_evidence_folders_are_someone_elses_material(db):
    task_id = _task(db, "성과점검")
    own = _doc(db, task_id, r"성과점검\추진현황.hwp", ["제출기한 : 2024. 7. 19."], role=docrole.PLAN)
    _doc(db, 0, r"성과점검\사전컨설팅\도서관(24.6).zip", [], status="partial")
    unpacked = _doc(db, task_id, r"성과점검\사전컨설팅\도서관(24.6)\과업지시서.hwp",
                    ["계약금액 : 금112,751,990원"])
    evidence = _doc(db, task_id, r"성과점검\1.신청\증빙서류\수사업무 협조 의뢰.hwp",
                    ["담당자 : 해양경찰서 수사과 (☎ 051-000-0000)"])
    attached = _doc(db, task_id, r"성과점검\市 공문\종합계획.hwpx", ["소요예산 : 2,546억원"], role=docrole.PLAN)
    letter = _doc(db, task_id, r"성과점검\市 공문\자료 제출 요청.hwp", ["7. 19.(금)까지 제출"],
                  role=docrole.DIRECTIVE)

    every = [own, unpacked, evidence, attached, letter]
    assert foldersteps.outside_the_work(db, every) == {unpacked, evidence, attached}
    assert foldersteps.outside_the_work(db, every, keep_requests=False) == {unpacked, evidence, attached, letter}
    kinds = {item["kind"] for item in rc.extract_task(db, task_id, BASIS)}
    assert rc.AMOUNT not in kinds and rc.CONTACT not in kinds


def test_a_request_kept_from_a_reference_folder_gives_dates_and_contacts_but_not_amounts(db):
    task_id = _task(db, "종합평가")
    _doc(db, task_id, r"종합평가\ref\심사기준.hwp",
         ["예산 : 12,000천원", "담당자 : 김도현 주무관", "제출기한 : 2025. 1. 10."], role=docrole.CRITERIA)

    found = {item["kind"] for item in rc.extract_task(db, task_id, BASIS)}
    assert found == {rc.DEADLINE, rc.CONTACT}


# ── 선례와 절차 ─────────────────────────────────────────────────────

def _steps(db, task_id: int, year: int, labels: list[str]) -> None:
    db.replace_task_steps(task_id, year, [
        {"ordinal": n, "label": label, "month": 10, "day_hint": "10월", "doc_id": None}
        for n, label in enumerate(labels, start=1)])


def test_the_precedent_is_the_round_that_ran_to_the_end_not_the_latest_scrap(db):
    task_id = _task(db, "우수부서 선발")
    _doc(db, task_id, r"우수부서\계획.hwp", ["개요"])
    _steps(db, task_id, 2023, ["선발 계획", "추천 요청", "1차 심사", "검증 요청", "결과 보고", "결과 알림"])
    _steps(db, task_id, 2024, ["선발계획", "부서제출", "1차 심사", "대상자 검증요청", "위원회 개최",
                               "결과보고", "보도자료", "시상식"])
    _steps(db, task_id, 2025, ["회의·행사 준비", "문서 처리", "회의·행사 준비"])

    labels = [step["label"] for step in rc.build_steps(db, task_id, [], BASIS)]
    assert len(labels) == 8 and labels[0].startswith("선발계획 (2024년")

    # 최근 해가 견줄 만큼 남아 있으면 최근 해가 선례다
    _steps(db, task_id, 2025, ["선발계획", "부서제출", "1차 심사", "위원회 개최", "결과보고", "시상식"])
    assert rc.build_steps(db, task_id, [], BASIS)[0]["label"].startswith("선발계획 (2025년")


def test_a_procedure_quoted_from_another_task_is_not_this_tasks_procedure(db):
    selection, committee = _task(db, "우수부서 선발"), _task(db, "환경위원회")
    quoted = "추진절차 : 추천→1차 심사평가→최종심사→인센티브 부여 및 시상"
    _doc(db, committee, r"환경위원회\제3차 심의 자료.hwp", [quoted], role=docrole.PLAN)
    _doc(db, selection, r"우수부서\선발 계획.hwp", [quoted], role=docrole.PLAN)
    _steps(db, selection, 2024, ["우수부서 추천 요청", "1차 심사평가 계획", "인센티브 부여 요청", "시상식"])
    _steps(db, committee, 2024, ["위원회 개최 계획", "서면심의 안내", "개최 결과 보고", "참석수당 지급 의뢰"])

    def stated(task_id):
        items = rc.extract_task(db, task_id, BASIS)
        steps = [s["label"] for s in rc.build_steps(db, task_id, items, BASIS)
                 if s["procedure_kind"] == "stated"]
        return [i["kind"] for i in items if i["kind"] == rc.PROCEDURE], steps

    assert stated(committee) == ([], [])
    assert stated(selection) == ([rc.PROCEDURE], ["추천", "1차 심사평가", "최종심사", "인센티브 부여 및 시상"])


def test_a_procedure_stays_where_there_is_nothing_to_compare_it_with(db):
    task_id = _task(db, "보조금 정산")
    _doc(db, task_id, r"보조금\정산 계획.hwp", ["처리절차 : 접수→검토→정산→통보"], role=docrole.PLAN)
    items = rc.extract_task(db, task_id, BASIS)
    assert [s["label"] for s in rc.build_steps(db, task_id, items, BASIS)] == ["접수", "검토", "정산", "통보"]


# ── 문장 ────────────────────────────────────────────────────────────

def test_sentences_come_from_the_works_own_instructions_not_from_cases_slides_or_statutes(db):
    task_id = _task(db, "우수부서 선발")
    kept = "공적기간 이전에 착수하였으나 공적기간 내 성과가 창출된 경우도 포함한다."
    _doc(db, task_id, r"우수부서\선발 계획.hwp", [kept], role=docrole.PLAN)
    _doc(db, task_id, r"우수부서\발표자료.pptx",
         ["디지털 혁신으로 구민 편의와 행정 효율성을 동시에 실현합니다."], role=docrole.MEETING)
    _doc(db, task_id, r"우수부서\운영 규정.hwp",
         ["지방자치단체의 장은 우수공무원에게 인사상 우대 조치를 부여해야 한다."], role=docrole.LEGAL)
    _doc(db, task_id, r"우수부서\우수사례 모음.hwp",
         [*_CASES, "어업인 대표와 약정을 체결하여 보상업무의 효율성을 제고함."], role=docrole.PLAN)
    _doc(db, task_id, r"우수부서\시스템 관리자 매뉴얼.pdf",
         ["등록 버튼을 클릭하면 저장되고 목록으로 이동함."], role=docrole.CRITERIA)
    _doc(db, task_id, r"우수부서\이름 모를 문서.hwp",
         ["충주시 유튜브 채널은 구독자 수가 매우 많다."], role=docrole.OTHER)
    _doc(db, task_id, r"우수부서\추천서 양식.pdf", ["개인정보를수집및이용합니다."], role=docrole.PLAN)

    assert [item["text"] for item in statements.sentences(db, task_id)] == [kept]


# ── 낼 곳과 목적 ────────────────────────────────────────────────────

_LETTER = ["부  산  광  역  시", "수신", "수신자 참조", "제목", "상반기 성과점검 자료 제출요청",
           "2. 실적점검 자료를 '24. 7. 19.(금)까지 제출해 주시기 바랍니다.", "부 산 광 역 시 장",
           "시행", "규제혁신추진단-4843", "(2024. 6. 26.)", "접수", "기획실-10745", "(2024. 6. 26.)"]


def _item(db, task_id: int, kind: str) -> list[tuple[str, str, str]]:
    return [(item["slot"], item["content"], item["basis_state"])
            for item in rc.extract_task(db, task_id, BASIS) if item["kind"] == kind]


def test_the_office_that_sent_the_request_is_where_the_work_goes(db):
    task_id = _task(db, "성과점검")
    _doc(db, task_id, r"성과점검\참고\자료 제출요청.hwp", _LETTER, role=docrole.DIRECTIVE)

    assert _item(db, task_id, rc.RECIPIENT) == [
        (rc.SENDER_SLOT, "부산광역시 규제혁신추진단", "inferred")]      # 문서가 말한 것이 아니라 읽어 낸 것

    # 중앙부처의 안내와 그것을 전달한 시의 공문이 함께 있으면 엇갈림이 아니라 둘 다다
    ministry = ["행 정 안 전 부", "실적보고서를 1. 10.(금)까지 제출", "시행 공무원단체과-1616 (2024. 6. 25.) 접수 기획실-10700"]
    _doc(db, task_id, r"성과점검\참고\평가 안내.hwpx", ministry, role=docrole.DIRECTIVE)
    assert _item(db, task_id, rc.RECIPIENT) == [
        (rc.SENDER_SLOT, "부산광역시 규제혁신추진단 / 행정안전부 공무원단체과", "inferred")]


def test_a_letter_we_sent_or_a_notice_that_asks_for_nothing_names_no_recipient(db):
    task_id = _task(db, "우수부서 선발")
    ours = ["사      하      구", "수신 재무과장", "참석수당 지급을 요청하오니 조치하여 주시기 바랍니다.",
            "시행 기획실-11843", "접수"]
    notice = ["사      하      구", "인센티브 부여계획을 붙임과 같이 알려드립니다.",
              "시행 총무과-26091 (2024. 7. 11.) 접수 기획실-12001"]
    _doc(db, task_id, r"우수부서\참석수당 지급 의뢰.hwp", ours, role=docrole.DIRECTIVE)
    _doc(db, task_id, r"우수부서\인센티브 부여계획 알림.hwp", notice, role=docrole.DIRECTIVE)

    assert _item(db, task_id, rc.RECIPIENT) == []


def test_the_purpose_is_the_plans_own_opening_sentence(db):
    task_id = _task(db, "우수부서 선발")
    whole = "우수부서 선발 및 인센티브 제공으로 성과를 창출한 부서를 우대하여 적극적인 공직문화를 조성하고자 함."
    _doc(db, task_id, r"우수부서\선발 계획.hwp", ["2024년 우수부서 선발 계획", whole, "추진근거", "선발개요",
                                                 "심사기준", "행정사항", "붙임", "끝"], role=docrole.PLAN)
    _doc(db, task_id, r"우수부서\위원회 개최 계획.hwp", ["확정을 위해 위원회를 개최하고자 함.", "일시", "장소"],
         role=docrole.PLAN)
    # 사례 안의 '…하고자 함'은 목적이 아니다
    _doc(db, task_id, r"우수부서\우수사례 모음.hwp",
         ["주민 불편을 해소하고 쾌적한 환경을 조성하고자 함.", *_CASES], role=docrole.PLAN)

    assert _item(db, task_id, rc.PURPOSE) == [(rc.PURPOSE_SLOT, whole, "stated")]


def test_submit_by_official_letter_names_the_recipient(db):
    task_id = _task(db, "종합평가")
    _doc(db, task_id, r"종합평가\평가 안내.hwpx",
         ["○ (제출기한) '25. 1. 10. (금) 18시까지 행안부 공무원단체과로 공문 제출"], role=docrole.DIRECTIVE)
    assert _item(db, task_id, rc.RECIPIENT) == [("제출처", "행안부 공무원단체과", "stated")]


# ── 돈의 과목, 엮이는 부서, 명단, 결과물 ────────────────────────────

def test_the_budget_line_runs_on_to_the_next_line_when_it_ends_with_a_comma(db):
    task_id = _task(db, "직원 교육")
    _doc(db, task_id, r"교육\실시 계획.hwp",
         ["소요예산 : 513,000원", " 예산과목 : 행정역량강화, 직원능력향상,", "일반운영비, 사무관리비", "행정사항"],
         role=docrole.PLAN)
    assert _item(db, task_id, rc.AMOUNT) == [
        ("소요예산", "513,000원", "stated"),
        (rc.BUDGET_SLOT, "행정역량강화, 직원능력향상, 일반운영비, 사무관리비", "stated")]


def test_partner_offices_come_from_our_own_papers_with_what_they_do(db):
    task_id = _task(db, "우수부서 선발")
    _doc(db, task_id, r"우수부서\선발 계획.hwp",
         ["등록번호 기획실-16857", "우수부서 인센티브 부여 요청(총무과) : 12월 초",
          "견책 이상 징계자 등 제한 대상 여부 검토(소통감사실, 총무과)", "선발방식 변경 계획수립(기획실) : 6월 한",
          "장소 : 본관 2층 (중회의실)", "1열: 구분 | 2열: 담당(재무과)"], role=docrole.PLAN)
    _doc(db, task_id, r"우수부서\참석수당 지급 의뢰.hwp",
         ["수신 재무과장", "제목 제2차 위원회 회의 참석수당 지급 의뢰", "1. 기획실-11712호와 관련입니다."],
         role=docrole.EXPENSE)
    # 받은 공문의 괄호 안 부서는 보낸 쪽의 사정이다
    _doc(db, task_id, r"우수부서\추천 요청.hwp", ["후보자 추천(공무원단체과)"], role=docrole.DIRECTIVE)

    assert _item(db, task_id, rc.AGENCY) == [
        ("협조 부서 · 소통감사실", "견책 이상 징계자 등 제한 대상 여부 검토", "stated"),
        ("협조 부서 · 재무과", "제2차 위원회 회의 참석수당 지급 의뢰", "stated"),
        ("협조 부서 · 총무과", "우수부서 인센티브 부여 요청 / 견책 이상 징계자 등 제한 대상 여부 검토", "stated")]


def test_a_roster_is_pointed_to_not_copied_out(db):
    task_id = _task(db, "환경위원회")
    _doc(db, task_id, r"위원회\위원회 명단(2024.1.).hwp", ["담당자 : 홍길동 010-0000-0000"], year=2024,
         role=docrole.ROSTER)
    _doc(db, task_id, r"위원회\위원회 명단(2025.1.).hwp", ["연락처 : 010-1111-2222"], year=2025,
         role=docrole.ROSTER)

    assert _item(db, task_id, rc.CONTACT) == [(rc.ROSTER_SLOT, "위원회 명단(2025.1.).hwp", "stated")]
    assert docrole.classify("환경위원회 명단(2025.1.).hwp").role == docrole.ROSTER
    assert docrole.classify("제3차 위원회 참석자 명단(예정).hwp").role == docrole.MEETING


def test_what_sits_in_the_submitted_folder_is_the_output_unless_others_handed_it_in(db):
    task_id = _task(db, "종합평가")
    _doc(db, task_id, r"종합평가\제출\추진실적 보고서.hwpx", ["실적"], role=docrole.PERFORMANCE)
    _doc(db, task_id, r"종합평가\의결서\제출\심의의결서(위원).hwp", ["의결"], role=docrole.DECISION)
    _doc(db, task_id, r"종합평가\작업 중 보고서.hwpx", ["초안"], role=docrole.PERFORMANCE)

    assert _item(db, task_id, rc.OUTPUT) == [(rc.OUTPUT_SLOT, "추진실적 보고서.hwpx", "stated")]


def test_a_folder_step_carries_its_forms_received_papers_and_what_was_made(db):
    task_id = _task(db, "우수부서 선발")
    plan = _doc(db, task_id, r"우수부서\하반기\4. 대상자 검증요청\검증 요청.hwp", ["요청"], role=docrole.PLAN)
    _doc(db, task_id, r"우수부서\하반기\4. 대상자 검증요청\검증서식.hwp", ["서식"], role=docrole.FORM)
    _doc(db, task_id, r"우수부서\하반기\4. 대상자 검증요청\old\검증 요청(이전).hwp", ["이전"], role=docrole.PLAN)
    got = _doc(db, task_id, r"우수부서\하반기\2. 부서제출\추천서(녹지과).hwp", ["추천"], role=docrole.RECEIVED)
    _doc(db, task_id, r"우수부서\하반기\1. 선발계획\선발 계획.hwp", ["계획"], role=docrole.PLAN)
    db.replace_task_steps(task_id, 2024, [
        {"ordinal": 1, "label": "부서제출", "month": 10, "day_hint": "10월", "doc_id": got},
        {"ordinal": 2, "label": "대상자 검증요청", "month": 10, "day_hint": "10월", "doc_id": plan}])

    received, verify = rc.build_steps(db, task_id, [], BASIS)
    assert (received.get("inputs"), received["output"]) == ("추천서(녹지과).hwp", "")
    assert (verify.get("forms"), verify["output"]) == ("검증서식.hwp", "검증 요청.hwp")


def test_files_do_not_each_become_a_step_when_the_folder_gives_no_order():
    made = [timeline.StepCandidate(1, 8, None, "문서 처리"), timeline.StepCandidate(2, 8, 2, "계획 수립"),
            timeline.StepCandidate(3, 10, None, "문서 처리"), timeline.StepCandidate(4, 10, None, "문서 처리"),
            timeline.StepCandidate(5, 10, 15, "검토"), timeline.StepCandidate(6, 10, 20, "검토")]
    assert [(c.doc_id, c.label) for c in timeline.condense(made, "문서 처리")] == [
        (2, "계획 수립"), (5, "검토")]
    # 이름 붙은 단계가 없으면 있는 것이라도 남긴다(달마다 하나)
    bare = [timeline.StepCandidate(n, 7, None, "문서 처리") for n in (1, 2, 3)]
    assert [c.doc_id for c in timeline.condense(bare, "문서 처리")] == [1]


def test_the_manual_opens_with_what_the_work_is_for_then_the_folder_count(db):
    task_id = _task(db, "우수부서 선발")
    db.con.execute("UPDATE tasks SET description = ? WHERE id = ?", ("‘우수부서’ 폴더의 문서 1건입니다.", task_id))
    purpose = "우수부서 선발 및 인센티브 제공으로 성과를 창출한 부서를 우대하여 공직문화를 조성하고자 함."
    _doc(db, task_id, r"우수부서\선발 계획.hwp", ["선발 계획", purpose, "추진근거", "선발개요"], role=docrole.PLAN)
    rc.restore(db)

    overview = task_manual.build(db, task_id).sections[0]
    assert overview.lines[0].startswith(purpose)
    assert overview.lines[1] == "자료 현황: ‘우수부서’ 폴더의 문서 1건입니다."


# ── 때 ──────────────────────────────────────────────────────────────

def _grid(counts: dict[tuple[int, int], int]) -> timeline.Grid:
    cells, next_id = {}, 1
    for key, count in counts.items():
        cells[key] = list(range(next_id, next_id + count))
        next_id += count
    return timeline.Grid(years=sorted({year for year, _month in counts}), cells=cells)


def test_a_month_with_a_few_stray_papers_is_not_a_month_the_work_recurs_in():
    # 6월과 10~11월에 몰려 있고, 9월에는 두 해 모두 늦어진 상장 문서가 몇 건 있다
    grid = _grid({(2023, 6): 40, (2023, 9): 2, (2023, 11): 30,
                  (2024, 6): 60, (2024, 9): 4, (2024, 10): 50, (2024, 11): 45})
    assert timeline.detect_cycle(grid).months == [6, 11]
    # 문서가 적은 업무는 있는 달이 그대로 반복 달이다
    small = _grid({(2023, 1): 1, (2023, 7): 2, (2024, 1): 2, (2024, 7): 1})
    assert timeline.detect_cycle(small).months == [1, 7]
    # 같은 달이 없어도 해마다 한 달 안쪽으로 붙어 있으면 같은 일이다(최근 해의 달로 말한다)
    drifting = _grid({(2023, 2): 1, (2023, 4): 3, (2024, 5): 4})
    assert timeline.detect_cycle(drifting).months == [5]
    apart = _grid({(2023, 2): 2, (2024, 9): 2})
    assert timeline.detect_cycle(apart) is None


def test_an_event_day_inside_a_collected_case_is_not_this_works_schedule(db):
    task_id = _task(db, "우수부서 선발")
    _doc(db, task_id, r"우수부서\추천 현황(우수사례).hwp",
         [*_CASES, "❑ 일 시 : 2024. 3. 13.(수) 14:00", "우수부서 추천(전부서) : 6. 17.(월) 한"],
         role=docrole.PERFORMANCE)
    assert [when.isoformat() for _doc_id, when in rc.schedule_dates(db, task_id)] == ["2024-06-17"]


def test_a_term_that_runs_out_is_raised_as_a_date_but_is_not_a_month_of_work(db):
    task_id = _task(db, "환경위원회")
    _doc(db, task_id, r"위원회\[쪽지] 위원회 관련.txt",
         ["1. 위원회는 위촉기간이 23년 4월~ 26년 4월까지(3년) 이므로 그대로이고, 당연직은 업데이트하시면 됩니다."],
         role=docrole.MEMO)
    _doc(db, task_id, r"위원회\선발 계획.hwp", ["공적기간 : 2024. 5. 1. ~ 2024. 10. 31."], role=docrole.PLAN)

    assert _item(db, task_id, rc.DEADLINE) == [("만료 · 위촉기간", "2026년 4월", "stated")]
    assert rc.schedule_dates(db, task_id) == []
    rc.restore(db, BASIS)
    gap = next(g for g in db.info_gaps(task_id) if g["kind"] == "stale")
    assert gap["field"] == "만료 · 위촉기간" and "2026년 4월에 끝났습니다" in gap["detail"]
    assert gap["next_action"].startswith("위촉기간을(를) 다시 정했는지(재위촉·연장·갱신) 확인하세요.")


def test_a_request_with_a_deadline_suggests_a_yearly_task_even_from_one_round(db):
    task_id = _task(db, "종합평가")
    letter = _doc(db, task_id, r"종합평가\ref\평가 안내.hwpx",
                  ["○ (제출기한) 실적보고서를 2025. 1. 10.(금) 18시까지 제출"], year=2025, role=docrole.DIRECTIVE)
    _doc(db, task_id, r"종합평가\실적 보고서 초안.hwpx", ["작성기한 : 2024. 12. 20."], year=2024,
         role=docrole.PERFORMANCE)

    assert rc.request_deadlines(db, task_id) == [(letter, date(2025, 1, 10))]

    # 우리가 위원·부서에 낸 요청의 기한은 '해마다 오는 요청'의 근거가 아니다
    ours = _task(db, "소송 지원")
    _doc(db, ours, r"소송 지원\서면심의 요청.hwp",
         ["수신 위원", "심의의결서를 2024. 4. 11.(목)까지 회신하여 주시기 바랍니다.", "시행 기획실-6001", "접수"],
         role=docrole.DIRECTIVE)
    assert rc.request_deadlines(db, ours) == []


def test_several_empty_years_are_named_as_missing_rounds(db):
    task_id = _task(db, "우수부서 선발")
    _doc(db, task_id, r"우수부서\선발 계획.hwp", ["계획"], year=2024, role=docrole.PLAN)
    db.replace_task_cycles(task_id, [{"kind": "yearly", "months": "6,11", "day_hint": None,
                                      "years_observed": 3, "confidence": "high", "evidence": ""}])
    after = next(i for i in rc.record_items(db, task_id, BASIS) if i["slot"] == rc.SLOT_AFTERWARDS)
    assert after["content"].startswith(
        "마지막 기록은 2024년입니다. 반복하는 일인데 2025년부터 2026년까지 2개 연도의 자료가 없습니다.")

    # 한 해만 비었으면 예전 문장 그대로다
    after = next(i for i in rc.record_items(db, task_id, date(2025, 3, 1)) if i["slot"] == rc.SLOT_AFTERWARDS)
    assert after["content"].startswith("2025년 자료가 없습니다.")


def test_the_manual_tells_a_newcomer_to_reapply_for_access(db):
    task_id = _task(db, "성과점검")
    _doc(db, task_id, r"성과점검\참고\붙임2 적극행정 온(on) 접근권한 신청서(서식).xlsx", ["서식"], role=docrole.FORM)
    _doc(db, task_id, r"성과점검\추진현황.hwp", ["현황"], role=docrole.PERFORMANCE)
    rc.restore(db)

    first = next(s for s in task_manual.build(db, task_id).sections if s.key == "first")
    assert first.lines[-1] == ("담당자가 바뀌면 본인 이름으로 다시 내야 하는 신청서가 있습니다: "
                               "붙임2 적극행정 온(on) 접근권한 신청서(서식).xlsx")


def test_the_home_says_where_the_material_stops(db):
    task_id = _task(db, "우수부서 선발")
    for index in range(30):
        _doc(db, task_id, rf"우수부서\2023\문서{index}.hwp", ["본문"], year=2023)
        _doc(db, task_id, rf"우수부서\2024\문서{index}.hwp", ["본문"], year=2024)
    _doc(db, task_id, r"우수부서\발표자료.hwp", ["본문"], year=2025)

    assert span.summary(db, BASIS) == (
        "이 자료는 2023~2024년이 중심이고 2025년 이후의 자료는 1건뿐입니다. 그 뒤에 한 일과 바뀐 것은 "
        "온나라 문서와 현 담당자에게 확인하세요.")
    assert span.summary(db, date(2024, 12, 1)) == ""        # 올해까지 이어지면 말하지 않는다


# ── 전임자의 말, 바뀐 것, 조심할 자료 ───────────────────────────────

def _notes(db, task_id: int) -> list[tuple[str, str]]:
    return [(item["slot"], item["content"]) for item in handover_notes.items(db, task_id, BASIS)]


def test_a_memo_is_carried_line_by_line_and_unfinished_work_is_set_apart(db):
    task_id = _task(db, "우수부서 선발")
    _doc(db, task_id, r"우수부서\선발－주의사항.txt",
         ["1. 제출 서식 보완\n- 해당 업무 수행기간 기입 칸 마련\n2. 발표자 시간제한 타이머 운영 : 3분 초과하면 알람"
          "\n(알람 울리면 감점됨을 공지)\n8. 신관 2층 중회의실이라고 하면 제2청사 갈 수 있으니 주의\n<행사>"],
         role=docrole.MEMO)

    assert _notes(db, task_id) == [
        ("전임자가 남긴 과제 · 선발－주의사항 · 01", "1. 제출 서식 보완"),
        ("전임자가 남긴 과제 · 선발－주의사항 · 02", "- 해당 업무 수행기간 기입 칸 마련"),
        ("전임자 메모 · 선발－주의사항 · 03",
         "2. 발표자 시간제한 타이머 운영 : 3분 초과하면 알람 (알람 울리면 감점됨을 공지)"),
        ("전임자 메모 · 선발－주의사항 · 04", "8. 신관 2층 중회의실이라고 하면 제2청사 갈 수 있으니 주의")]
    # 메모는 줄마다 올라가므로 모델이 다시 고르지 않는다
    assert statements.sentences(db, task_id) == []


def test_what_changed_is_read_from_our_own_plans(db):
    task_id = _task(db, "우수부서 선발")
    _doc(db, task_id, r"우수부서\하반기 선발 계획.hwp",
         ["선발인원 : 1차 심사평가 통과자 전원 ※변경", "- 특별휴가 3일 (기존 2일)",
          "○ (기존 구제제도 한계) 이의신청 등 기존 기능을 보완", "선발대상 : 전 직원"], role=docrole.PLAN)
    _doc(db, task_id, r"우수부서\제4차 위원회 개최 계획.hwp",
         ["첫 행: 구분 | 기존 | 변경\n1열: 선발방식 | 2열: 상위점수 7명 선발 | 3열: 75점 이상 전원 선발"],
         role=docrole.PLAN)
    # 받은 공문과 사례 모음에 적힌 '변경'은 우리 일의 변경이 아니다
    _doc(db, task_id, r"우수부서\市 공문\운영지침 알림.hwp", ["평가방식 Pass/Fail로 ※변경"], role=docrole.PLAN)

    assert sorted(content for _slot, content in _notes(db, task_id)) == [
        "- 특별휴가 3일 (기존 2일)", "선발방식: 상위점수 7명 선발 → 75점 이상 전원 선발",
        "선발인원 : 1차 심사평가 통과자 전원 ※변경"]
    assert all(slot.startswith("바뀐 것 · 2024년 · ") for slot, _content in _notes(db, task_id))


def test_a_sheet_the_predecessor_ticked_is_pointed_to(db):
    task_id = _task(db, "종합평가")
    ticks = "\n".join(f"1열: 지표 {n} | 2열: 정량 | 3열: {mark}" for n, mark in enumerate("●◐?○●(4)".split("(")[0] + "●", 1))
    _doc(db, task_id, r"종합평가\[평가표]종합평가.hwpx", ["첫 행: 지표 | 방법 | 자체점검\n" + ticks], role=docrole.CRITERIA)
    _doc(db, task_id, r"종합평가\평가 계획.hwpx", ["1열: 지표 | 2열: 정량 | 3열: 4"], role=docrole.CRITERIA)

    assert _notes(db, task_id) == [("전임자의 점검 표시가 있는 문서", "[평가표]종합평가.hwpx")]


def test_sensitive_material_is_counted_by_name_and_table_head_without_being_read(db):
    task_id = _task(db, "직원 교육")
    _doc(db, task_id, r"교육\강사 자료\신분증 통장\신분증(앞).pdf", [], status="empty")
    _doc(db, task_id, r"교육\강사 자료\신분증 통장\통장.jpg", [], status="skipped")
    _doc(db, task_id, r"교육\참고\참고인진술서(해양경찰서).hwp", ["진술"])
    _doc(db, task_id, r"교육\위원 명단.hwp", ["성명 | 생년월일 | 소속 | 연락처"], role=docrole.ROSTER)
    # 걸리면 안 되는 이름들
    _doc(db, task_id, r"교육\추천서(민원여권과).hwp", ["추천"])
    _doc(db, task_id, r"교육\2024년 적극행정 우수사례.hwp", ["사례"])
    _doc(db, task_id, r"교육\소송 지원 절차.xlsx", ["절차"])

    assert _notes(db, task_id) == [
        ("취급주의 · 신분증·통장 사본", "2건 (신분증 통장 폴더). 보존이 필요한지 확인하고, 내보내거나 공유할 때 빼세요."),
        ("취급주의 · 수사·소송·징계 자료", "1건 (참고 폴더). 보존이 필요한지 확인하고, 내보내거나 공유할 때 빼세요."),
        ("취급주의 · 생년월일·연락처가 든 문서", "1건 (교육 폴더). 보존이 필요한지 확인하고, 내보내거나 공유할 때 빼세요.")]


def test_the_notes_reach_the_manual(db):
    task_id = _task(db, "우수부서 선발")
    _doc(db, task_id, r"우수부서\주의사항.txt", ["7. 마우스 포인터(발표자용) + 여분 건전지"], role=docrole.MEMO)
    rc.restore(db)

    notes = next(s for s in task_manual.build(db, task_id).sections if s.key == "notes")
    assert any("7. 마우스 포인터(발표자용) + 여분 건전지" in line for line in notes.lines)


# ── 엮인 업무, 일이 아닌 묶음 ───────────────────────────────────────

def _related(db) -> dict[str, dict[str, list[str]]]:
    names = {row["id"]: row["name"] for row in db.con.execute("SELECT id, name FROM tasks")}
    return {names[a]: {names[b]: [why for why, _doc, _quote in reasons] for b, reasons in others.items()}
            for a, others in relations.find(db).items()}


def test_tasks_are_linked_by_folder_names_shared_papers_and_indicators_not_merged(db):
    selection, committee = _task(db, "환경정책 우수부서"), _task(db, "환경정책위원회")
    lawsuit, review, plan = _task(db, "소송 지원 관련"), _task(db, "환경정책 종합평가"), _task(db, "환경정책 실행계획")
    shared = _doc(db, selection, r"우수부서\선발 계획.hwp", ["계획"], role=docrole.PLAN)
    db.con.execute("UPDATE documents SET hash = 'same' WHERE id = ?", (shared,))
    copy = _doc(db, committee, r"환경정책위원회\제3회 환경정책위원회(상반기 우수부서 선발)\(붙임5)선발 계획.hwp",
                ["계획"], role=docrole.PLAN)
    db.con.execute("UPDATE documents SET hash = 'same' WHERE id = ?", (copy,))
    _doc(db, lawsuit, r"환경정책위원회\소송 지원 관련\1.신청\신청서.hwp", ["신청"], role=docrole.PLAN)
    _doc(db, plan, r"환경정책 실행계획\실행계획.hwp", ["계획"], role=docrole.PLAN)
    sheet = "\n".join(f"3열: {n:02d}. {name}" for n, name in enumerate(
        ["환경정책 실행계획 수립", "제도개선 노력", "기관장 실행역량", "성과창출 노력", "공공기관 확산",
         "환경정책위원회 현안처리 건수", "현안처리 사례", "사전컨설팅 건수", "우수부서 선발 실적 및 노력"], 1))
    _doc(db, review, r"환경정책 종합평가\[평가표]환경정책 종합평가.hwpx", [sheet], role=docrole.DECISION)

    found = _related(db)
    assert found["환경정책 우수부서"]["환경정책위원회"] == [
        "‘환경정책위원회’의 ‘제3회 환경정책위원회(상반기 우수부서 선발)’ 폴더에 이 업무의 자료가 있습니다",
        "같은 문서가 두 업무에 있습니다: 선발 계획.hwp"]
    assert found["소송 지원 관련"]["환경정책위원회"] == ["이 업무의 폴더가 ‘환경정책위원회’ 폴더 안에 있습니다"]
    assert found["환경정책 실행계획"]["환경정책 종합평가"] == [
        "평가지표 ‘01. 환경정책 실행계획 수립’의 실적입니다. 증빙을 모아 두세요"]
    assert found["환경정책 우수부서"]["환경정책 종합평가"] == [
        "평가지표 ‘09. 우수부서 선발 실적 및 노력’의 실적입니다. 증빙을 모아 두세요"]
    assert "환경정책 실행계획" not in found["환경정책 우수부서"]
    assert len(db.con.execute("SELECT id FROM tasks").fetchall()) == 5          # 합치지 않는다

    rc.restore(db)
    item = next(i for i in db.task_items(selection) if i["slot"] == "관련 업무 · 환경정책위원회")
    assert (item["kind"], item["basis_state"]) == (rc.AGENCY, "inferred")


def test_a_folder_of_statutes_is_reference_not_work(db):
    laws = _task(db, "관련 법령 등")
    _doc(db, laws, r"법령\운영 조례.hwp", ["제1조"], year=2023, role=docrole.LEGAL)
    _doc(db, laws, r"법령\지방공무원법_제75조의2.hwp", ["제75조의2"], year=2023, role=docrole.LEGAL)
    work = _task(db, "실행계획")
    _doc(db, work, r"실행계획\계획.hwp", ["계획"], year=2023, role=docrole.PLAN)
    _doc(db, work, r"실행계획\운영지침.hwp", ["지침"], year=2023, role=docrole.LEGAL)

    assert (bundles.kind(db, laws), bundles.kind(db, work)) == (bundles.REFERENCE, None)
    assert docrole.classify("지방공무원법_제75조의2(적극행정의 장려).hwp").role == docrole.LEGAL
    assert docrole.classify("작성 방법 안내.hwp").role != docrole.LEGAL
    rc.restore(db)
    assert continuity.find_gaps(db, laws, BASIS) == []
    records = [i["content"] for i in db.task_items(laws) if i["kind"] == rc.RECORD]
    assert len(records) == 1 and records[0].startswith("참고 자료 묶음입니다.")
    # 사람이 업무로 확정하면 보통 업무다
    db.con.execute("UPDATE tasks SET status = 'confirmed' WHERE id = ?", (laws,))
    assert bundles.kind(db, laws) is None


def test_loose_papers_outside_every_task_folder_are_gathered_but_not_treated_as_work(db):
    from app.ai.discover import discover

    for folder in ("환경정책위원회", "환경정책 실행계획"):
        for index in range(2):
            _doc(db, 0, rf"{folder}\문서{index}.hwp", ["본문"], role=docrole.PLAN)
    loose = [_doc(db, 0, name, ["본문"]) for name in
             ("★구군 담당자 명단(24.08.기준).xlsx", "(24년 상) 규제애로 해소 사례 제출 건수.xlsx",
              "상반기 책임관회의 자료.hwpx")]

    discover(db, client=None)

    tasks = {row["name"]: row["id"] for row in db.tasks()}
    bundle = tasks[bundles.LOOSE_NAME]
    assert {doc["id"] for doc in db.task_documents(bundle)} == set(loose)
    assert bundles.kind(db, bundle) == bundles.LOOSE and bundles.kind(db, tasks["환경정책위원회"]) is None
    rc.restore(db)
    assert continuity.find_gaps(db, bundle, BASIS) == []
    assert db.work_steps(bundle) == []
