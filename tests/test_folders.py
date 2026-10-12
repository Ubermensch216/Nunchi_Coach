"""전임자의 폴더 구조를 업무·회차·단계·자료 성격으로 읽는다.

실제 인수인계 자료에서 드러난 꼴을 합성 이름으로 옮긴 것이다. 본문 유사도만으로
묶었을 때 한 업무가 수십 개로 흩어지고 단계 이름의 60%가 '문서 처리'였다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ai.discover import _merge_targets, discover
from app.core import dating, docrole, folders, foldersteps, timeline
from app.db import Database
from app.ingest.parsers._aes import decrypt_ecb
from app.search import pack

ROOT = r"D:\전임자\[환경정책]"


# ── 폴더 이름 읽기 ──────────────────────────────────────────────────

@pytest.mark.parametrize("name, label, order, date, year, half, session, modifier", [
    ("2024년 환경정책 우수부서", "환경정책 우수부서", None, None, 2024, None, None, None),
    ("[0. old]", "old", 0, None, None, None, None, folders.OLD),
    ("[2023년도]", "", None, None, 2023, None, None, None),
    ("하반기", "", None, None, None, "하", None, None),
    ("3. 1차 심사", "1차 심사", 3, None, None, None, None, None),
    ("13. [2023-11-14] 2023년 제3차 환경위원회 개최 결과 보고", "환경위원회 개최 결과 보고",
     13, "2023-11-14", 2023, None, 3, None),
    ("2024년 제5회 환경위원회(하반기 우수부서 선발)", "환경위원회", None, None, 2024, None, 5, None),
    ("2. 부서제출", "부서제출", 2, None, None, None, None, folders.RECEIVED),
    ("ref", "ref", None, None, None, None, None, folders.REFERENCE),
    ("bak", "bak", None, None, None, None, None, folders.BACKUP),
    ("pdf_old", "pdf old", None, None, None, None, None, folders.OLD),
    ("3월 소스", "3월 소스", None, None, None, None, None, folders.RECEIVED),
    ("제출", "제출", None, None, None, None, None, folders.SUBMITTED),
])
def test_folder_names_are_read(name, label, order, date, year, half, session, modifier):
    part = folders.parse_part(name)
    assert (part.label, part.order, part.date, part.year, part.half, part.session, part.modifier) == (
        label, order, date, year, half, session, modifier)


def test_a_year_in_the_name_is_not_mistaken_for_a_step_number():
    assert folders.parse_part("2024년 실행계획").order is None
    assert folders.parse_part("10월 소스").order is None


def _tree(paths):
    return folders.Tree.build(ROOT, [ROOT + "\\" + p for p in paths])


def test_top_folder_is_the_task_and_wrappers_are_skipped():
    paths = [r"2024년 환경정책 실행계획\市 공문\요청.hwp",
             r"[0. old]\[2023년도]\[환경정책 실행계획]\1. [2023-03-30] 제출 요청\요청.hwp",
             r"2023\메모.txt", r"기타\잡다.hwp", "낱장.hwp"]
    tree = _tree(paths)
    place = lambda p: tree.place(ROOT + "\\" + p)  # noqa: E731

    assert place(paths[0]).label == "환경정책 실행계획"
    old = place(paths[1])
    assert old.label == "환경정책 실행계획" and old.round_key == "2023"
    assert old.step.date == "2023-03-30" and old.step.order == 1
    assert folders.OLD in old.modifiers
    # 연도만 적힌 폴더, 기타 폴더, 자료 폴더 바로 아래는 업무 폴더가 아니다.
    assert all(place(p).loose for p in paths[2:])


def test_a_differently_named_folder_with_its_own_numbered_steps_is_its_own_task():
    paths = [r"환경위원회\2024년 제2회 환경위원회\계획.hwp",
             r"환경위원회\소송 지원 관련\1.지원 신청\신청서.hwp",
             r"환경위원회\소송 지원 관련\2.사실 확인\의견서.hwp",
             r"환경위원회\소송 지원 관련\old\받은자료.hwp",
             r"환경위원회\평가\ref\묶음\1. 언론\기사.pdf",
             r"환경위원회\평가\ref\묶음\2. 설명회\결과.pdf"]
    tree = _tree(paths)
    assert tree.place(ROOT + "\\" + paths[0]).label == "환경위원회"       # 회차는 같은 일
    assert tree.place(ROOT + "\\" + paths[1]).label == "소송 지원 관련"
    assert tree.place(ROOT + "\\" + paths[3]).label == "소송 지원 관련"
    # 참고 폴더 안의 번호 폴더는 남이 정리한 것이다. 업무로 떼어 내지 않는다.
    assert tree.place(ROOT + "\\" + paths[4]).label == "환경위원회"


def test_the_deepest_folder_names_the_round():
    tree = _tree([r"2024년 환경정책 우수부서\하반기\2024년 하반기 카드뉴스\시안.pdf"])
    assert tree.place(ROOT + "\\" + r"2024년 환경정책 우수부서\하반기\2024년 하반기 카드뉴스\시안.pdf"
                      ).round_key == "2024 하반기"


def test_the_word_every_folder_shares_does_not_tell_tasks_apart():
    labels = ["환경정책 실행계획", "환경정책 중점과제 선정계획", "환경정책 중점과제",
              "환경정책위원회", "환경정책 위원회", "교육", "환경정책 위원회 우수부서 선발",
              "환경정책 우수부서"]
    common = folders.common_tokens(labels)
    assert common == ["환경정책"]
    cores = sorted({folders.core(label, common) for label in labels})
    target = _merge_targets(cores)
    assert target["중점과제선정계획"] == "중점과제"
    assert folders.core("환경정책위원회", common) == folders.core("환경정책 위원회", common) == "위원회"
    # 둘을 품으면 뒤의 것 — '위원회 우수부서 선발'은 우수부서를 뽑는 일이다.
    assert target["위원회우수부서선발"] == "우수부서"


# ── 폴더 구조로 업무 찾기 ───────────────────────────────────────────

@pytest.fixture
def project(tmp_path: Path):
    db = Database(tmp_path / "p.db")
    db.init()
    yield db
    db.close()


def _add(db, relative: str, status: str = "ok", vector=None, **extra) -> int:
    source_id = db.add_source(ROOT)
    name = relative.rsplit("\\", 1)[-1]
    doc_id = db.upsert_document(source_id, {
        "path": ROOT + "\\" + relative, "filename": name, "ext": Path(name).suffix,
        "parse_status": status, "hash": relative, "char_count": 500 if status == "ok" else 0,
        **extra})
    if vector is not None:
        db.save_doc_embedding(doc_id, "bge-m3", len(vector), pack(vector), 100)
    return doc_id


def _tasks(db) -> dict[str, set[int]]:
    return {row["name"]: {doc["id"] for doc in db.task_documents(row["id"])} for row in db.tasks()}


def test_folders_decide_the_tasks_even_when_the_bodies_do_not_look_alike(project):
    """한 업무의 계획서·추천서·견적서는 서로 닮지 않았다. 벡터는 전부 다르게 준다."""
    award = [_add(project, rf"2024년 환경정책 우수부서\하반기\{i}. 단계{i}\문서{i}.hwp",
                  vector=[1.0 if k == i else 0.0 for k in range(12)]) for i in range(1, 5)]
    award_old = [_add(project, rf"[0. old]\[2023년도]\[환경정책위원회 우수부서 선발]\{i}. [2023-06-0{i}] 계획\문서.hwp",
                      vector=[1.0 if k == i + 4 else 0.0 for k in range(12)]) for i in range(1, 3)]
    plan = [_add(project, rf"2024년 환경정책 실행계획\계획{i}.hwp",
                 vector=[1.0 if k == i + 6 else 0.0 for k in range(12)]) for i in range(1, 3)]
    plan_old = [_add(project, r"[0. old]\[2023년도]\[환경정책 실행계획]\2023 계획.hwp", vector=[0.0] * 11 + [1.0])]
    scanned = _add(project, r"2024년 환경정책 실행계획\결재본 스캔.pdf", status="empty")
    loose_named = _add(project, "2024년 환경정책 실행계획(배포용).pdf", vector=[0.5] * 12)
    loose_other = _add(project, "담당자 명단.xlsx", vector=[0.3] * 12)
    for label in ("환경정책위원회", "환경정책 중점과제", "교육"):
        for i in range(2):
            _add(project, rf"{label}\문서{i}.hwp", vector=[0.1 * (i + 1)] * 12)

    result = discover(project, client=None)

    found = _tasks(project)
    assert result.named_by_folder == len(found) == 5
    assert found["환경정책 우수부서"] == set(award + award_old)        # 지난 회차와 한 업무
    assert found["환경정책 실행계획"] == set(plan + plan_old + [scanned, loose_named])
    # 이름에 단서가 없는 낱장은 어느 업무에도 밀어 넣지 않는다.
    assert all(loose_other not in docs for docs in found.values())
    task = next(row for row in project.tasks() if row["name"] == "환경정책 실행계획")
    assert task["confidence"] == "medium" and "폴더" in task["description"]
    # 본문을 읽지 못한 문서는 위치만 보고 붙였다고 남긴다.
    weak = project.con.execute(
        "SELECT confidence, evidence FROM task_docs WHERE doc_id = ?", (scanned,)).fetchone()
    assert (weak["confidence"], weak["evidence"]) == ("low", "폴더 위치")

    discover(project, client=None)                                     # 다시 돌려도 같다
    assert _tasks(project) == found


def test_a_flat_folder_still_goes_by_the_bodies(project):
    for index in range(4):
        _add(project, f"2024_수질통계_자료{index}.hwp", vector=[1.0, 0.02, 0.02])
    result = discover(project, client=None, min_size=3)
    assert result.named_by_folder == 0 and result.clusters == 1


def test_the_predecessors_memo_is_read_first(project):
    memo = _add(project, r"2024년 환경정책 우수부서\우수부서선발－주의사항.txt", vector=[1.0, 0.0])
    for i in range(3):
        _add(project, rf"2024년 환경정책 우수부서\2024년 선발 계획{i}.hwp", vector=[1.0, 0.0],
             eff_year=2024, eff_date="2024-06-01", eff_date_kind="body")
    for i in range(2):
        _add(project, rf"교육\교육 계획{i}.hwp", vector=[0.0, 1.0])
    discover(project, client=None)
    task = next(row for row in project.tasks() if row["name"] == "환경정책 우수부서")
    first = project.task_reading(task["id"])[0]
    assert first["doc_id"] == memo and "메모" in first["reason"]


# ── 번호·날짜 폴더가 처리 단계다 ────────────────────────────────────

def test_numbered_folders_are_the_steps_in_the_predecessors_order(project):
    labels = ["선발계획", "부서제출", "1차 심사", "위원회 개최", "시상식"]
    for order, label in enumerate(labels, start=1):
        _add(project, rf"2024년 환경정책 우수부서\하반기\{order}. {label}\문서.hwp",
             eff_year=2024, eff_date="2024-01-01", eff_precision="year", eff_date_kind="filename",
             fs_mtime=f"2024-1{0 if order < 4 else 1}-1{order}T10:00:00")
    _add(project, r"2024년 환경정책 우수부서\하반기\0. ref\타기관 계획.hwp", eff_year=2024)
    _add(project, r"2024년 환경정책 우수부서\하반기\3. 1차 심사\old\이전 총괄표.hwp", eff_year=2024)
    # 압축을 푼 사본에 같은 단계가 한 벌 더 있다. 단계가 둘이 되지 않는다.
    _add(project, r"2024년 환경정책 우수부서\하반기\[공유]사본\1. 선발계획\문서.hwp", eff_year=2024)
    project.con.execute("INSERT INTO tasks(name, origin, status, confidence) VALUES ('우수부서','ai','proposed','medium')")
    task_id = project.con.execute("SELECT id FROM tasks").fetchone()[0]
    project.con.execute("INSERT INTO task_docs(task_id, doc_id, origin) SELECT ?, id, 'ai' FROM documents", (task_id,))

    steps = foldersteps.plan(project, task_id)

    assert list(steps) == [2024]
    assert [s["label"] for s in steps[2024]] == labels
    assert [s["ordinal"] for s in steps[2024]] == [1, 2, 3, 4, 5]
    # 달은 그 해에 고친 파일의 날짜에서 왔다고 밝힌다.
    assert steps[2024][0]["day_hint"] == "10월(파일 날짜)" and steps[2024][4]["month"] == 11
    assert all(s["doc_id"] for s in steps[2024])


def test_dated_folders_give_the_step_its_day(project):
    for order, (day, label) in enumerate([("2023-03-30", "제출 요청"), ("2023-04-24", "청장 보고"),
                                          ("2023-06-20", "자료 제출")], start=1):
        _add(project, rf"[0. old]\[2023년도]\[환경정책 실행계획]\{order}. [{day}] {label}\문서.hwp",
             eff_year=2023, eff_month=int(day[5:7]), eff_date=day, eff_precision="day", eff_date_kind="folder")
    project.con.execute("INSERT INTO tasks(name, origin, status, confidence) VALUES ('실행계획','ai','proposed','medium')")
    task_id = project.con.execute("SELECT id FROM tasks").fetchone()[0]
    project.con.execute("INSERT INTO task_docs(task_id, doc_id, origin) SELECT ?, id, 'ai' FROM documents", (task_id,))

    steps = foldersteps.plan(project, task_id)[2023]
    assert [(s["label"], s["day_hint"]) for s in steps] == [
        ("제출 요청", "3월 말"), ("청장 보고", "4월 말"), ("자료 제출", "6월 중")]
    assert project.task_years(task_id) == [2023]


# ── 시점 ────────────────────────────────────────────────────────────

def test_a_year_only_date_has_no_month():
    found = dating.collect(filename="2024년 선발 계획.hwp")
    assert (found.year, found.precision, found.month) == (2024, "year", None)


def test_the_date_the_predecessor_wrote_on_the_folder_wins():
    found = dating.collect(
        text="2022년 실행계획 2022. 1. 5.", filename="2022년 실행계획.hwp",
        path=ROOT + r"\[환경정책 실행계획]\2. [2023-03-30] 부서별 자료 제출 요청\2022년 실행계획.hwp")
    assert (found.value, found.kind, found.precision) == ("2023-03-30", "folder", "day")
    assert {c.kind for c in found.candidates} >= {"body", "filename", "folder"}   # 후보는 남긴다


def test_a_template_creation_date_is_not_the_documents_date():
    shared = dating.collect(filename="추천서(건설과).hwp", meta_created="2004-10-20 01:31:06",
                            fs_mtime="2024-06-13T10:00:00", meta_shared=True)
    assert shared.kind == "fs"                       # 서식 날짜를 버리고, 믿지 못할 근거로 남는다
    named = dating.collect(filename="2024년 선발 계획.hwp", meta_created="2004-10-20 01:31:06")
    assert (named.year, named.kind) == (2024, "filename")
    alone = dating.collect(filename="회의록.hwp", meta_created="2024-09-12 10:00:00")
    assert (alone.value, alone.kind) == ("2024-09-12", "meta")


def test_a_body_date_far_from_the_named_year_is_history_inside_the_text():
    found = dating.collect(text="사업 연혁 1993. 5. 1. 착공", filename="2024년 우수사례 발표자료.pptx")
    assert (found.year, found.kind) == (2024, "filename")
    labeled = dating.collect(text="작성일: 2022. 3. 4.", filename="2024년 자료.hwp")
    assert labeled.value == "2022-03-04"            # 작성일을 밝힌 것은 그대로 믿는다


def test_a_date_after_the_file_was_last_saved_is_a_plan_not_a_date():
    found = dating.collect(text="2026년 목표", filename="발표자료.pptx", fs_mtime="2025-10-15T09:00:00")
    assert found.kind == "fs" and found.year == 2025


def test_the_month_of_work_comes_from_the_file_when_it_is_the_same_year():
    # 본문 앞머리의 4월은 공적 기간의 끝이고, 실제로 일한 달은 파일을 고친 6월이다.
    assert timeline.work_month(2024, 4, "body", "day", "2024-06-18T09:00:00") == (6, True)
    # 이듬해에 폴더째 복사한 것은 파일 날짜를 쓰지 않는다.
    assert timeline.work_month(2023, 4, "body", "day", "2024-01-05T09:00:00") == (4, True)
    # 전임자가 폴더·파일 이름에 적은 날짜는 그대로다.
    assert timeline.work_month(2023, 6, "folder", "day", "2023-09-01T09:00:00") == (6, True)
    assert timeline.work_month(2024, None, "filename", "year", "2024-10-02T09:00:00") == (10, True)
    assert timeline.work_month(2024, None, "filename", "year", None) == (None, False)
    assert timeline.work_month(2024, 3, "fs", "day", "2024-03-01T09:00:00") == (None, False)
    # 여러 폴더가 한날에 바뀌었으면 복사한 날이다.
    assert timeline.work_month(2024, None, "filename", "year", "2024-10-02T09:00:00",
                               {"2024-10-02"}) == (None, False)


def test_a_day_when_many_folders_changed_at_once_is_a_copy_day():
    stamps = [("2024-01-05T10:00:00", f"폴더{i % 8}") for i in range(40)]
    stamps += [("2024-01-09T10:00:00", "내려받은 폴더")] * 60        # 한 폴더에 받은 것은 복사가 아니다
    assert timeline.bulk_copy_days(stamps) == {"2024-01-05"}


# ── 갈래 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("filename, role", [
    ("우수부서선발－주의사항.txt", docrole.MEMO),
    ("3. 참고자료(행사준비).hwp", docrole.MEMO),
    ("2024년 제3차 위원회 심의의결서.hwp", docrole.DECISION),
    ("좌석배치도(2024년 제3차).hwp", docrole.MEETING),
    ("[견적서]하반기 현수막.hwp", docrole.EXPENSE),
    ("[보도자료]2024년 하반기 우수부서.pdf", docrole.PUBLICITY),
    ("○○구 환경정책 운영 조례.hwp", docrole.LEGAL),
    # 연도별 비교가 기대는 갈래는 바뀌지 않는다.
    ("2024년 선발 계획.hwp", docrole.PLAN),
    ("심사 결과 알림.hwp", docrole.RESULT),
    ("추천서 서식.hwp", docrole.FORM),
])
def test_roles_from_names(filename, role):
    assert docrole.classify(filename).role == role


def test_the_folder_says_what_an_unnamed_document_is():
    assert docrole.classify("건설과.hwp", modifiers=(folders.RECEIVED,)).role == docrole.RECEIVED
    assert docrole.classify("타기관 사례.pdf", modifiers=(folders.REFERENCE,)).role == docrole.REFERENCE
    assert docrole.classify("건설과.hwp").role == docrole.OTHER
    assert docrole.path_modifiers(ROOT + r"\우수부서\2. 부서제출\bak\건설과.hwp", ROOT) == (
        folders.RECEIVED, folders.BACKUP)


def test_other_peoples_material_is_not_this_works_record(project):
    own = _add(project, r"우수부서\2024년 선발 계획.hwp")
    received = _add(project, r"우수부서\부서제출\건설과 추천서.hwp")
    backup = _add(project, r"우수부서\bak\2024년 선발 계획_결재전.hwp")
    last_round = _add(project, r"[0. old]\[2023년도]\[우수부서]\2023년 선발 계획.hwp")
    request = _add(project, r"우수부서\ref\2024년 평가 계획 안내.hwp")       # 받은 요청 공문
    other_plan = _add(project, r"우수부서\ref\이웃 구 실행계획.pdf")
    docrole.classify_documents(project)
    outside = foldersteps.outside_the_work(
        project, [own, received, backup, last_round, request, other_plan])
    # 지난 회차의 기록은 이 일의 선례다. 참고 폴더에 넣어 둔 요청 공문은 이 일의 기한이 적힌 문서다.
    assert outside == {received, backup, other_plan}


# ── 배포용 한글 문서 ────────────────────────────────────────────────

def test_aes_128_decryption_matches_the_published_vectors():
    key = bytes(range(16))
    assert decrypt_ecb(key, bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")).hex() == (
        "00112233445566778899aabbccddeeff")
    # NIST SP 800-38A F.1.2 (ECB-AES128.Decrypt), 두 블록
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    cipher = bytes.fromhex("3ad77bb40d7a3660a89ecaf32466ef97f5d3d58503b9699de785895a96fdbaaf")
    assert decrypt_ecb(key, cipher + b"\x00" * 5).hex() == (
        "6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51")
