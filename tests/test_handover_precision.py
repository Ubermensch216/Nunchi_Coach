"""틀린 값을 내지 않는다 — 남의 사례 금액, 조각 회차의 선례, 남의 절차.

실제 인수인계 자료에서 금액 항목 15건 가운데 14건이 부서 우수사례·중점과제의 사업비였고,
'지금 할 일'의 선례는 발표자료 넉 장뿐인 최근 해에서, 위원회 업무의 '적힌 절차'는 선발
업무의 것에서 왔다. 꼴만 옮긴 합성 자료다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.core import casedocs, docrole, foldersteps, reconstruction as rc, statements
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
