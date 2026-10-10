"""문서 역할·연도 갈래·판 묶음·지표 표 (행동형 업무 비서 계획서 1단계, T04·T05)."""

import json

import pytest

from app.core import docrole, docyears, indicators, versions
from app.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "project.db")
    database.init()
    database.source_id = database.con.execute(
        "INSERT INTO sources(path) VALUES (?)", (str(tmp_path / "자료"),)).lastrowid
    database.task_id = database.con.execute("INSERT INTO tasks(name) VALUES ('평가 대응')").lastrowid
    yield database
    database.close()


def add(db, filename, body="", *, digest=None, year=None, date=None, kind="body", mtime=None,
        sections=None, title=None, task=True):
    """문서 하나를 분석이 끝난 모양으로 넣는다."""
    doc_id = db.con.execute(
        "INSERT INTO documents(source_id, path, filename, ext, hash, doc_title, eff_year, eff_date, "
        "eff_date_kind, fs_mtime, parse_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok')",
        (db.source_id, f"/자료/{filename}", filename, "." + filename.rsplit(".", 1)[-1],
         digest or filename, title, year, date, kind if year else None, mtime)).lastrowid
    db.con.execute("INSERT INTO document_index(doc_id, filename, body) VALUES (?, ?, ?)",
                   (doc_id, filename, body))
    for ordinal, section in enumerate(sections or ([("paragraph", "1문단", body, {})] if body else [])):
        section_kind, locator, text, meta = section
        db.con.execute(
            "INSERT INTO document_sections(doc_id, kind, ordinal, locator, text, meta) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (doc_id, section_kind, ordinal, locator, text, json.dumps(meta, ensure_ascii=False)))
    if task:
        db.con.execute("INSERT INTO task_docs(task_id, doc_id) VALUES (?, ?)", (db.task_id, doc_id))
    return doc_id


# ── 문서 역할 ───────────────────────────────────────────────────────

@pytest.mark.parametrize("filename, role", [
    ("2025_평가_시행계획_알림.hwpx", docrole.DIRECTIVE),
    ("2025_실적_제출_요청.hwpx", docrole.DIRECTIVE),          # 실적이기 전에 요청이다
    ("2025_평가결과_통보.hwpx", docrole.RESULT),              # 통보이기 전에 결과다
    ("2025_규제혁신평가_결과_통보.hwpx", docrole.RESULT),     # 밑줄로 이은 낱말도 붙여 읽는다
    ("2025_평가지표_및_매뉴얼.hwpx", docrole.CRITERIA),
    ("부서별_실적취합_서식.xlsx", docrole.FORM),              # 실적이기 전에 서식이다
    ("2025_평가자료_제출본.hwpx", docrole.SUBMISSION),
    ("2025_증빙자료_목록.xlsx", docrole.EVIDENCE),
    ("2025_부서별_실적.xlsx", docrole.PERFORMANCE),
    ("2025_추진계획.hwpx", docrole.PLAN),
    ("회의_메모.txt", docrole.OTHER),
])
def test_role_is_read_from_the_name_in_priority_order(filename, role):
    assert docrole.classify(filename).role == role


def test_unknown_document_is_left_undecided():
    found = docrole.classify("스캔0001.pdf", head="내용을 알 수 없는 글")
    assert (found.role, found.confidence, found.basis) == (docrole.OTHER, "low", "")


def test_body_shape_alone_gives_low_confidence():
    letter = "수신: 각 부서장\n제목: 자료 제출\n붙임: 서식 1부. 끝."
    assert docrole.classify("문서1.hwpx", head=letter) == docrole.Role(
        docrole.DIRECTIVE, "본문이 공문 꼴(수신·붙임)", "low")
    assert docrole.classify("문서2.hwpx", table_headers="지표 | 배점").role == docrole.CRITERIA


def test_agreeing_body_raises_confidence():
    letter = "수신: 각 부서장\n붙임: 서식 1부. 끝."
    assert docrole.classify("자료_제출_요청.hwpx").confidence == "medium"
    assert docrole.classify("자료_제출_요청.hwpx", head=letter).confidence == "high"


def test_roles_are_stored_and_human_choice_survives_reanalysis(db):
    first = add(db, "2025_평가지표.hwpx", sections=[
        ("table", "표1 2행", "첫 행: 지표 | 배점\n1열: 규제 발굴 | 2열: 30", {"header": "지표 | 배점"})])
    second = add(db, "메모.txt", "잡다한 글")
    assert docrole.classify_documents(db) == {docrole.CRITERIA: 1, docrole.OTHER: 1}
    assert docrole.roles(db, [first])[first]["confidence"] == "high"

    docrole.set_role(db, second, docrole.EVIDENCE)
    docrole.classify_documents(db)
    stored = docrole.roles(db, [second])[second]
    assert (stored["role"], stored["decided_by"]) == (docrole.EVIDENCE, "user")
    with pytest.raises(ValueError):
        docrole.set_role(db, second, "letter")


# ── 연도 갈래 ───────────────────────────────────────────────────────

def test_performance_year_and_evaluation_year_are_kept_apart():
    """T05. 2025년 실적을 2026년에 평가한다."""
    marks = {(m.kind, m.year) for m in docyears.read(
        "2025년도 실적에 대한 2026년 지방규제혁신 평가 자료\n제출일: 2026. 3. 14.")}
    assert marks == {(docyears.PERFORMANCE, 2025), (docyears.EVALUATION, 2026),
                     (docyears.SUBMITTED, 2026)}


def test_year_before_performance_is_not_the_evaluation_year():
    marks = {(m.kind, m.year) for m in docyears.read("2025년도 실적 평가 결과")}
    assert marks == {(docyears.PERFORMANCE, 2025)}


def test_labelled_years_are_read():
    marks = {(m.kind, m.year) for m in docyears.read("평가연도: 2026\n실적 기준연도: 2025")}
    assert marks == {(docyears.EVALUATION, 2026), (docyears.PERFORMANCE, 2025)}


def test_years_are_not_guessed_from_the_authored_year(db):
    doc_id = add(db, "회의록.txt", "일반 회의 내용입니다.", year=2026, date="2026-03-02")
    docyears.mark_documents(db)
    assert docyears.years(db, [doc_id]) == {doc_id: {docyears.AUTHORED: [2026]}}


def test_file_time_alone_is_not_an_authored_year(db):
    doc_id = add(db, "복사된파일.txt", "내용", year=2026, date="2026-03-02", kind="fs")
    docyears.mark_documents(db)
    assert docyears.years(db, [doc_id]) == {}


def test_stored_years_keep_their_source_and_human_choice(db):
    doc_id = add(db, "2026_평가자료.hwpx", "2025년도 실적 기준으로 작성함", year=2026,
                 date="2026-03-02")
    assert docyears.mark_documents(db) == 1
    row = db.con.execute("SELECT raw, locator FROM doc_years WHERE doc_id = ? AND kind = 'performance'",
                         (doc_id,)).fetchone()
    assert "2025년도 실적" in row["raw"] and row["locator"] == "1문단"
    # 이름의 '2026_평가'는 '2026년 평가'라고 적힌 것이 아니다. 평가의 해로 읽지 않는다.
    assert docyears.years(db, [doc_id])[doc_id] == {
        docyears.AUTHORED: [2026], docyears.PERFORMANCE: [2025]}

    docyears.set_year(db, doc_id, docyears.PERFORMANCE, 2024)
    docyears.mark_documents(db)
    assert docyears.years(db, [doc_id])[doc_id][docyears.PERFORMANCE] == [2024]


def test_year_for_refuses_to_pick_between_two():
    assert docyears.year_for({"evaluation": [2026], "authored": [2025]}, "evaluation", "authored") == 2026
    assert docyears.year_for({"authored": [2025]}, "evaluation", "authored") == 2025
    assert docyears.year_for({"evaluation": [2025, 2026]}, "evaluation", "authored") is None


# ── 판 묶음 ─────────────────────────────────────────────────────────

BODY = "지방규제혁신 평가 대응 계획 부서별 실적 취합 일정 증빙 제출 방법 안내 " * 5


def test_skeleton_drops_version_marks_but_keeps_the_year():
    names = ["2025_평가계획.hwpx", "2025_평가계획_수정.hwpx", "2025_평가계획_최종.hwpx",
             "2025_평가계획_최종_진짜.hwpx", "2025_평가계획(2).hwpx", "2025_평가계획_v2.hwpx",
             "2025_평가계획_final.hwpx", "2025_평가계획_20250301.hwpx"]
    assert len({versions.skeleton(name) for name in names}) == 1
    assert versions.skeleton("2024_평가계획.hwpx") != versions.skeleton("2025_평가계획.hwpx")
    assert versions.skeleton("3월_실적보고.hwpx") != versions.skeleton("4월_실적보고.hwpx")


def test_five_revisions_are_one_group_and_final_alone_does_not_decide(db):
    """T04. 수정본 다섯 — '최종'이 둘이면 최신본 후보를 세우지 않는다."""
    names = ["2025_요청공문.hwpx", "2025_요청공문_수정.hwpx", "2025_요청공문_최종.hwpx",
             "2025_요청공문_최종_진짜.hwpx", "2025_요청공문(2).hwpx"]
    ids = [add(db, name, BODY + f" 판 {index}", year=2025, date=f"2025-09-0{index + 1}")
           for index, name in enumerate(names)]
    assert versions.rebuild(db) == 1
    [group] = versions.groups_for(db, ids)
    assert [m["doc_id"] for m in group["members"]] == ids
    assert group["latest_doc_id"] is None and group["confidence"] == "low"
    assert "둘 이상" in group["latest_reason"]
    assert set(versions.representatives(db, ids).values()) == {ids[-1]}


def test_final_mark_on_the_newest_revision_is_a_medium_candidate(db):
    old = add(db, "2025_계획.hwpx", BODY + " 처음", year=2025, date="2025-03-01")
    new = add(db, "2025_계획_최종.hwpx", BODY + " 고침", year=2025, date="2025-03-09")
    versions.rebuild(db)
    [group] = versions.groups_for(db, [old])
    assert (group["latest_doc_id"], group["confidence"]) == (new, "medium")


def test_later_edit_than_the_final_mark_is_flagged(db):
    marked = add(db, "2025_계획_최종.hwpx", BODY + " 최종이라 적힘", year=2025, date="2025-03-01")
    later = add(db, "2025_계획_수정.hwpx", BODY + " 그 뒤에 고침", year=2025, date="2025-03-09")
    versions.rebuild(db)
    [group] = versions.groups_for(db, [marked, later])
    assert group["latest_doc_id"] is None and "나중에 고친" in group["latest_reason"]


def test_same_name_in_other_years_or_with_other_content_is_not_a_revision(db):
    add(db, "2024_평가계획_20240301.hwpx", BODY, year=2024, date="2024-03-01")
    add(db, "2025_평가계획_20250301.hwpx", BODY + " 올해", year=2025, date="2025-03-01")
    add(db, "회의자료.hwpx", "주차장 운영 개선 회의 안건 예산 편성", year=2025, date="2025-04-01")
    add(db, "회의자료(2).hwpx", "청사 방역 일정 용역 업체 선정 결과", year=2025, date="2025-04-02")
    assert versions.rebuild(db) == 0


def test_exact_copies_are_not_revisions(db):
    add(db, "계획.hwpx", BODY, digest="same", year=2025, date="2025-03-01")
    add(db, "계획 - 복사본.hwpx", BODY, digest="same", year=2025, date="2025-03-01")
    assert versions.rebuild(db) == 0


def test_rebuild_replaces_its_own_groups_and_keeps_human_ones(db):
    a = add(db, "2025_계획.hwpx", BODY + " 가", year=2025, date="2025-03-01")
    b = add(db, "2025_계획_수정.hwpx", BODY + " 나", year=2025, date="2025-03-02")
    versions.rebuild(db)
    versions.rebuild(db)
    assert db.con.execute("SELECT COUNT(*) FROM version_groups").fetchone()[0] == 1
    db.con.execute("UPDATE version_groups SET decided_by = 'user', latest_doc_id = ?", (a,))
    versions.rebuild(db)
    groups = versions.groups_for(db, [a, b])
    assert len(groups) == 1 and groups[0]["latest_doc_id"] == a


# ── 지표 표 ─────────────────────────────────────────────────────────

def test_indicator_rows_are_read_from_hwpx_tables(db):
    head = "첫 행: 평가지표 | 배점 | 산출 방법 | 증빙자료"
    doc_id = add(db, "2025_평가지표.hwpx", sections=[
        ("paragraph", "1문단", "2025년 평가지표", {}),
        ("table", "표1 2행", f"{head}\n1열: 1. 규제 발굴 | 2열: 30 | 3열: 발굴 건수 × 2점 | 4열: 발굴 목록",
         {"header": "평가지표 | 배점 | 산출 방법 | 증빙자료"}),
        ("table", "표1 3행", f"{head}\n1열: 2. 규제 개선 | 2열: 40 | 3열: 개선 완료 건수 | 4열: 시행 공문",
         {"header": "평가지표 | 배점 | 산출 방법 | 증빙자료"}),
    ])
    rows = indicators.read_document(db, doc_id)
    assert [(r.name, r.score, r.formula, r.proof) for r in rows] == [
        ("1. 규제 발굴", "30", "발굴 건수 × 2점", "발굴 목록"),
        ("2. 규제 개선", "40", "개선 완료 건수", "시행 공문")]
    assert rows[0].key() == "규제발굴" and rows[0].locator == "표1 2행"
    assert "30" in rows[0].quote


def test_indicator_rows_are_read_from_sheets(db):
    header = "지표\t배점\t정의"
    doc_id = add(db, "평가지표.xlsx", sections=[
        ("sheet", "'지표' A1:C3", f"[지표]\n{header}\n규제 발굴\t30\t새 과제 발굴\n규제 개선\t40\t개선 완료",
         {"sheet": "지표", "header": header})])
    assert [(r.name, r.score, r.definition) for r in indicators.read_document(db, doc_id)] == [
        ("규제 발굴", "30", "새 과제 발굴"), ("규제 개선", "40", "개선 완료")]


def test_tables_that_are_not_indicator_tables_are_not_read(db):
    doc_id = add(db, "예산표.hwpx", sections=[
        ("table", "표1 2행", "첫 행: 사업 | 예산 | 집행액\n1열: 시범사업 | 2열: 820 | 3열: 610",
         {"header": "사업 | 예산 | 집행액"}),
        ("table", "표2 2행", "첫 행: 항목\n1열: 혼자 있는 칸", {})])
    assert indicators.read_document(db, doc_id) == []


def test_method_column_is_not_mistaken_for_the_name():
    assert indicators.columns(["평가방법", "평가항목", "배점"]) == {
        indicators.FORMULA: 0, indicators.NAME: 1, indicators.SCORE: 2}
    assert indicators.columns(["항목"]) is None and indicators.columns(["부서", "실적"]) is None
