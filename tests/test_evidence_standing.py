"""근거 중심 질문 응답 — 어떤 문서를 먼저 근거로 쓰고, 무엇을 알리는가 (개선계획서 P1-4).

  · AI 생성 보고서·발간물·여러 기관 취합표·압축 묶음은 참고 근거다. 다른 근거가 있으면 자리를
    몇 개까지만 쓰고, 근거 목록에 무슨 자료인지 밝힌다.
  · '2025년 실적', '2026년 평가'는 문서의 작성 연도가 아니라 실적·평가의 해로 찾는다.
  · 읽지 못한 관련 문서가 있으면 '자료에 없다'고 하지 않고 그렇다고 알린다.
  · 자료가 끝난 뒤의 해를 물으면 자료가 어디서 끝나는지와 확인할 곳을 말한다.
문서는 지어낸 것이다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db import Database
from app.search import rag, standing
from app.search.query import QueryScope, infer_scope
from app.search.rag import _select_context


@pytest.fixture()
def db(tmp_path: Path):
    project = Database(tmp_path / "project.db")
    project.init()
    yield project
    project.close()


def _doc(db, name, year=2025, status="ok", parser=None, task=None):
    source = db.add_source("/자료")
    doc = db.upsert_document(source, {"path": f"/자료/{name}", "filename": name, "ext": Path(name).suffix,
                                      "parse_status": status, "eff_year": year, "hash": name, "parser": parser})
    if task:
        db.con.execute("INSERT OR IGNORE INTO tasks(id, name) VALUES (?, '평가 대응')", (task,))
        db.assign_document(task, doc)
    return doc


def _chunk(db, doc, text):
    chunk_id, _ = db.replace_document_chunks(doc, [(1, "1문단", text)])[0]
    db.index_document(doc, "", "", "", text)
    return chunk_id


# ── 참고 근거 ───────────────────────────────────────────────────────

def test_reference_kinds(db):
    ai = _doc(db, "[ChatGPT-DeepResearch]평가 전략.hwpx")
    paper = _doc(db, "규제동향 25년 봄호 Vol.50.pdf")
    bundle = _doc(db, "증빙자료.zip", parser="archive")
    national = _doc(db, "전국 건의과제 목록.xlsx")
    ours = _doc(db, "우리 구 건의과제 목록.xlsx")
    for doc, own, other in ((national, 6, 809), (ours, 6, 0)):
        db.con.execute("INSERT INTO matter_scans(doc_id, hash, state, rows_own, rows_other) VALUES (?, 'h', 'read', ?, ?)",
                       (doc, own, other))
    plan = _doc(db, "2025년 규제혁신 추진계획(결재).hwpx")
    assert standing.references(db, [ai, paper, bundle, national, ours, plan]) == {
        ai: standing.AI, paper: standing.PUBLICATION, bundle: standing.ARCHIVE, national: standing.COMPILED}
    assert standing.note(db, ai) == "2025년 자료 · 참고자료 · AI 생성 표시"
    assert standing.note(db, plan) == "2025년 자료"


def test_references_do_not_push_out_guidance(db):
    refs = [_doc(db, f"[ChatGPT]전략 {n}.hwpx") for n in range(4)]
    plan = _doc(db, "평가계획.hwpx")
    chunks = [_chunk(db, doc, f"평가 지표 배점 설명 {n}") for n, doc in enumerate(refs)]
    plan_chunk = _chunk(db, plan, "평가 지표 배점 설명 계획")
    combined = [(cid, 1.0 - i / 10) for i, cid in enumerate(chunks)] + [(plan_chunk, 0.1)]
    picked = [cid for cid, _ in _select_context(db, combined, set(chunks) | {plan_chunk}, QueryScope(years=[]))]
    assert plan_chunk in picked
    assert len(set(picked) & set(chunks)) == standing.REFERENCE_SLOTS


def test_only_references_are_still_used(db):
    refs = [_doc(db, f"[ChatGPT]전략 {n}.hwpx") for n in range(3)]
    chunks = [_chunk(db, doc, f"평가 지표 배점 설명 {n}") for n, doc in enumerate(refs)]
    combined = [(cid, 1.0 - i / 10) for i, cid in enumerate(chunks)]
    assert len(_select_context(db, combined, set(chunks), QueryScope(years=[]))) == 3


# ── 실적·평가의 해 ─────────────────────────────────────────────────

@pytest.mark.parametrize("question, years, basis", [
    ("2025년 실적 평가지표 배점은?", [2025], "performance"),
    ("2026년 구·군 규제혁신 평가 배점은?", [2026], "evaluation"),
    ("2024년 위원회 개최일은?", [2024], "document_date"),
])
def test_question_year_basis(question, years, basis):
    scope = infer_scope(question)
    assert (scope.years, scope.year_basis) == (years, basis)


def test_a_chosen_task_keeps_the_question_year_basis_but_chosen_years_win():
    inferred = infer_scope("2025년 실적 평가지표 배점은?")
    assert inferred.merged_with(QueryScope(task_id=3)).year_basis == "performance"
    assert inferred.merged_with(QueryScope(years=[2024], task_id=3)).year_basis == "document_date"


def test_performance_year_finds_the_right_document_and_falls_back_when_unmarked(db):
    marked = _doc(db, "2026년 평가(안).hwpx", year=2026)
    other = _doc(db, "2025년 다른 문서.hwpx", year=2025)
    db.con.execute("INSERT INTO doc_years(doc_id, kind, year, raw, locator) VALUES (?, 'performance', 2025, '(25실적)', '')",
                   (marked,))
    _chunk(db, marked, "평가 지표 배점 110점")
    _chunk(db, other, "평가 지표 배점 다른 내용")
    scope = infer_scope("2025년 실적 평가 배점은?")
    assert scope.search_documents(db) == [marked]                 # 작성 연도 2025인 문서가 아니라
    db.con.execute("DELETE FROM doc_years")
    answer = rag.search_only(db, "2025년 실적 평가 배점은?")
    assert {c.doc_id for c in answer.citations} == {other}        # 해 표시가 없으면 작성 연도로 찾는다


# ── 읽지 못한 문서와 자료의 범위 ────────────────────────────────────

def test_unread_related_documents_are_named(db):
    _doc(db, "심층간담회 결과보고.hwp", status="encrypted", task=1)
    _doc(db, "2025 숙박확인서.xlsx", status="encrypted", task=1)
    _doc(db, "심층간담회 사진.pdf", status="empty")                   # 업무 밖
    found = standing.unread_related(db, "심층간담회 결과는 어땠어?", QueryScope(task_id=1))
    assert [(d["filename"], d["reason"]) for d in found] == [("심층간담회 결과보고.hwp", "암호·보호 문서")]
    assert "‘심층간담회 결과보고.hwp’(암호·보호 문서)" in standing.unread_line(found)
    assert standing.unread_related(db, "어떻게 해야 하나?", None) == []   # 흔한 말로는 엮지 않는다


def test_a_question_beyond_the_material_says_where_it_stops(db):
    _doc(db, "2023 계획.hwp", year=2023, task=1)
    _doc(db, "2025 결과.hwp", year=2025, task=1)
    line = standing.material_line(db, QueryScope(years=[2026], task_id=1))
    assert line.startswith("자료는 2023~2025년 것입니다. 질문한 2026년 회차의 자료는 이 폴더에 없습니다")
    assert standing.material_line(db, QueryScope(years=[2025], task_id=1)) == ""


def test_answers_carry_notes_and_caveats(db):
    ai = _doc(db, "[ChatGPT]심층간담회 정리.hwpx", task=1)
    _chunk(db, ai, "심층간담회는 자문단과 함께 연다")
    _doc(db, "심층간담회 결과보고.hwp", status="encrypted", task=1)
    answer = rag.search_only(db, "심층간담회", QueryScope(task_id=1))
    assert answer.citations and answer.citations[0].note.endswith("참고자료 · AI 생성 표시")
    caveats = answer.generation["caveats"]
    assert any("본문을 읽지 못한 문서" in c for c in caveats)
    assert any("참고자료(AI 생성" in c for c in caveats)


def test_a_year_outside_the_material_is_not_called_unprepared(db):
    doc = _doc(db, "2025 등록규제 정비 요청.hwp", year=2025, task=1)
    _chunk(db, doc, "등록규제 정비 결과 8.22.까지 제출")
    answer = rag.search_only(db, "등록규제 정비 제출 기한", QueryScope(task_id=1, years=[2026]))
    assert answer.error is None and answer.withheld
    assert answer.text == rag.OUT_OF_SCOPE
    assert answer.guidance.startswith("자료는 2025년 것입니다. 질문한 2026년 회차의 자료는 이 폴더에 없습니다")
