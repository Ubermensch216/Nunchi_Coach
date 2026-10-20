"""업무노트 대화의 검색 다듬기 — 바꿔 쓴 질의, 후속 질문, 전체 정리."""

import hashlib
import json
import re
from types import SimpleNamespace

import pytest

from app.ai.schemas import EXPAND_SCHEMA, REWRITE_SCHEMA
from app.core import notebook
from app.db import Database
from app.search import expand, overview
from app.search.query import QueryScope
from app.search.rag import (GENERIC_WITHHELD, Answer, Citation, MIN_SIMILARITY, _with_variants, ask,
                            withheld_reason)
from app.search.vector import pack

import numpy as np


class Scripted:
    """스키마에 따라 정해 둔 응답을 내는 모델 대역. 무엇을 불렀는지 남긴다."""
    embed_model = "bge-m3"
    gen_model = "fake"
    cancelled = False

    def __init__(self, ready=True, **responses):
        self.ready, self.responses, self.calls = ready, responses, []

    def health(self):
        return SimpleNamespace(embedding_ready=self.ready, generation_ready=self.ready, message="")

    def embed(self, texts):
        # '마감'이 든 문장은 둘째 축, 나머지는 첫째 축 — 서로 직교한다.
        return [[0.0, 1.0, 0.0] if "마감" in text else [1.0, 0.0, 0.0] for text in texts], None

    def generate_json(self, prompt, schema, num_predict=400, temperature=0.0):
        name = {id(EXPAND_SCHEMA): "expand", id(REWRITE_SCHEMA): "rewrite",
                id(overview._MAP_SCHEMA): "map", id(overview._REDUCE_SCHEMA): "reduce"}.get(id(schema), "ask")
        self.calls.append((name, prompt))
        response = self.responses.get(name)
        if callable(response):
            response = response(prompt)
        if response is None:
            return None, "응답 없음", ""
        if isinstance(response, str):          # 생성 한도에 걸려 끊긴 응답
            return None, "생성 한도에 걸려 응답이 중간에 끊겼습니다", response
        return response, None, ""


@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path / "project.db")
    db.init()
    yield db
    db.close()


def add_doc(db, tmp_path, name, pieces, vector=None):
    source = db.add_source(tmp_path)
    path = tmp_path / name
    path.write_text("\n".join(pieces), encoding="utf-8")
    doc = db.upsert_document(source, {"path": str(path), "filename": name, "ext": ".txt",
                                      "hash": hashlib.sha256(path.read_bytes()).hexdigest(),
                                      "parse_status": "ok"})
    chunks = db.replace_document_chunks(doc, [(i, f"{i}문단", text) for i, text in enumerate(pieces, 1)])
    if vector is not None:
        for chunk_id, _text in chunks:
            db.save_chunk_embedding(chunk_id, "bge-m3", 3, pack(vector))
    return doc, [chunk_id for chunk_id, _ in chunks]


# ── 후속 질문 판정 ──────────────────────────────────────────────────

@pytest.mark.parametrize("question", ["그럼 제출처는 어디야?", "그 업무 담당 부서는?", "그리고 예산은?", "해당 계약 금액은 얼마야?"])
def test_follow_ups_are_recognised_by_rule(question):
    assert expand.needs_context(question)


@pytest.mark.parametrize("question", ["보관실 이전 제출기한은 언제야?", "2026년 예산요구액은 얼마야?", "그림 파일은 어떻게 올려?"])
def test_self_contained_questions_are_left_alone(question):
    assert not expand.needs_context(question)


# ── 바꿔 쓴 질의 ────────────────────────────────────────────────────

def test_variants_are_cleaned_deduplicated_and_never_echo_the_question():
    client = Scripted(expand={"variants": ["제출기한이 언제야?", " 회신 마감일 ", "회신 마감일", "", "가" * 400, 7]})
    assert expand.variants(client, "제출기한이 언제야?") == ["회신 마감일", "7"]
    assert expand.variants(Scripted(), "제출기한이 언제야?") == []          # 모델 실패는 빈 목록


def test_variant_ranking_adds_candidates_without_displacing_the_original(db, tmp_path):
    _, near = add_doc(db, tmp_path, "가.txt", ["제출기한은 9월 30일이다"], [1.0, 0.0, 0.0])
    _, far = add_doc(db, tmp_path, "나.txt", ["회신 마감일은 10월 30일이다"], [0.0, 1.0, 0.0])
    ids = np.array([near[0], far[0]])
    matrix = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
    combined, widened = _with_variants(
        db, ids, matrix, np.array([[0.0, 1.0, 0.0]], dtype=np.float32), ["회신 마감일"],
        [(near[0], 1.0), (far[0], 0.0)], [near[0]], None)
    assert combined[0][0] == near[0]              # 원래 질문이 찾은 것이 여전히 맨 위다
    assert far[0] in widened and 1.0 >= MIN_SIMILARITY


def test_expanded_query_reaches_a_passage_the_question_wording_misses(db, tmp_path):
    doc, _ = add_doc(db, tmp_path, "회신요청.txt", ["회신 마감일은 10월 30일이다"], [0.0, 1.0, 0.0])
    client = Scripted(ask={"answered": True, "sentences": [
        {"text": "회신 마감일은 10월 30일이다", "sources": [1]}]})
    scope = QueryScope(doc_ids=[doc])
    assert ask(db, "언제까지 답해야 해?", client=client, scope=scope).withheld
    answer = ask(db, "언제까지 답해야 해?", client=client, scope=scope, expander=lambda q: ["회신 마감일"])
    assert not answer.withheld and [c.doc_id for c in answer.citations] == [doc]
    assert answer.generation["expansions"] == 1


def test_expander_is_not_called_when_only_text_search_is_available(db, tmp_path):
    doc, _ = add_doc(db, tmp_path, "회신요청.txt", ["회신 마감일은 10월 30일이다"])

    def boom(_question):
        raise AssertionError("모델이 없는데 질의 확장을 불렀다")

    answer = ask(db, "회신 마감일", client=Scripted(ready=False), scope=QueryScope(doc_ids=[doc]), expander=boom)
    assert answer.generation["mode"] == "search_only"


# ── 대화: 후속 질문 ─────────────────────────────────────────────────

@pytest.fixture
def book(db, tmp_path):
    doc, _ = add_doc(db, tmp_path, "보관실.txt", ["보관실 이전 제출기한은 9월 30일이다", "제출처는 총무과이다"])
    key = notebook.create(db, "보관실 이전")
    notebook.add_sources(db, key, [doc])
    return key


def capture(monkeypatch):
    seen = []

    def fake(db, question, client, scope, **extra):
        seen.append((question, extra))
        return Answer(question, "답", False,
                      citations=[Citation(1, scope.doc_ids[0], "보관실.txt", "1문단", "")], model="fake")
    monkeypatch.setattr(notebook, "ask", fake)
    return seen


def test_follow_up_is_rewritten_from_earlier_questions_and_the_rewrite_is_recorded(db, book, monkeypatch):
    seen = capture(monkeypatch)
    client = Scripted(rewrite={"question": "보관실 이전 제출처는 어디야?"})
    notebook.query(db, book, "보관실 이전 제출기한은 언제야?", client)
    message, answer = notebook.query(db, book, "그럼 제출처는 어디야?", client)
    assert seen[1][0] == "보관실 이전 제출처는 어디야?"
    assert answer.question == "그럼 제출처는 어디야?"            # 화면에는 사용자가 쓴 그대로
    row = db.con.execute("SELECT * FROM notebook_messages WHERE id=?", (message,)).fetchone()
    assert row["standalone_question"] == "보관실 이전 제출처는 어디야?" and row["mode"] == "rag"
    rewrite_prompt = next(prompt for name, prompt in client.calls if name == "rewrite")
    assert "보관실 이전 제출기한은 언제야?" in rewrite_prompt and "답" not in rewrite_prompt.split("[앞선 질문]")[1]


def test_self_contained_question_is_never_rewritten(db, book, monkeypatch):
    seen = capture(monkeypatch)
    client = Scripted(rewrite={"question": "엉뚱하게 바꾼 질문"})
    notebook.query(db, book, "보관실 이전 제출기한은 언제야?", client)
    notebook.query(db, book, "보관실 이전 제출처는 어디야?", client)
    assert seen[1][0] == "보관실 이전 제출처는 어디야?"
    assert not any(name == "rewrite" for name, _ in client.calls)


def test_follow_up_without_a_model_falls_back_to_joining_the_previous_question(db, book, monkeypatch):
    seen = capture(monkeypatch)
    notebook.query(db, book, "보관실 이전 제출기한은 언제야?")
    notebook.query(db, book, "그럼 제출처는 어디야?")
    assert seen[1][0] == "보관실 이전 제출기한은 언제야?\n후속 질문: 그럼 제출처는 어디야?"
    assert seen[1][1] == {}                                       # 모델이 없으면 질의 확장도 없다


def test_history_from_a_different_source_selection_is_not_used(db, book, tmp_path, monkeypatch):
    seen = capture(monkeypatch)
    client = Scripted(rewrite={"question": "이전 선택의 질문을 끌어온 문장"})
    notebook.query(db, book, "보관실 이전 제출기한은 언제야?", client)
    other, _ = add_doc(db, tmp_path, "다른.txt", ["다른 자료"])
    notebook.add_sources(db, book, [other])
    notebook.query(db, book, "그럼 제출처는 어디야?", client)
    assert seen[1][0] == "그럼 제출처는 어디야?"


def test_query_expansion_can_be_switched_off_per_project(db, book, monkeypatch):
    seen = capture(monkeypatch)
    notebook.query(db, book, "보관실 이전 제출기한은 언제야?", Scripted())
    db.set_meta("notebook_query_expansion", "0")
    notebook.query(db, book, "보관실 이전 제출처는 어디야?", Scripted())
    assert "expander" in seen[0][1] and seen[1][1] == {}


# ── 전체 정리 ───────────────────────────────────────────────────────

def test_overview_extracts_verified_facts_from_every_batch_and_weaves_them(db, tmp_path):
    pieces = [f"{n}분기 집행액은 {n * 100}만원이다" for n in range(1, 7)]
    doc, _ = add_doc(db, tmp_path, "집행.txt", pieces)

    def extract(prompt):
        numbers = [int(n) for n in re.findall(r"^\[(\d+)\]", prompt, flags=re.M)]
        facts = [{"text": pieces[n - 1], "sources": [n]} for n in numbers]
        facts.append({"text": "7분기 집행액은 999만원이다", "sources": [numbers[0]]})   # 원문에 없는 숫자
        facts.append({"text": pieces[0], "sources": [40]})                               # 이 묶음에 없는 번호
        return {"facts": facts}

    client = Scripted(map=extract, reduce={"sentences": [
        {"text": "1분기 집행액은 100만원이다", "sources": [1]},
        {"text": "6분기 집행액은 600만원이다", "sources": [6]},
        {"text": "8분기 집행액은 800만원이다", "sources": [2]}]})          # 근거와 다른 숫자 — 버려진다
    answer = overview.overview(db, "분기별 집행액을 정리해 줘", client, QueryScope(doc_ids=[doc]))
    assert not answer.withheld and answer.generation["mode"] == "overview"
    assert answer.generation["batches"] == 2 and answer.generation["facts"] == 6
    assert answer.text.splitlines() == ["· 1분기 집행액은 100만원이다[1]", "· 6분기 집행액은 600만원이다[2]"]
    assert [c.locator for c in answer.citations] == ["1문단", "6문단"]
    assert "999" not in answer.text and "800" not in answer.text


def test_overview_falls_back_to_the_facts_when_weaving_fails(db, tmp_path):
    doc, _ = add_doc(db, tmp_path, "집행.txt", ["1분기 집행액은 100만원이다"])
    client = Scripted(map={"facts": [{"text": "1분기 집행액은 100만원이다", "sources": [1]}]})
    answer = overview.overview(db, "정리해 줘", client, QueryScope(doc_ids=[doc]))
    assert answer.text == "· 1분기 집행액은 100만원이다[1]" and answer.generation["reduce_fallback"]


def test_overview_withholds_when_nothing_relevant_is_found(db, tmp_path):
    doc, _ = add_doc(db, tmp_path, "집행.txt", ["1분기 집행액은 100만원이다"])
    answer = overview.overview(db, "인사 발령을 정리해 줘", Scripted(map={"facts": []}), QueryScope(doc_ids=[doc]))
    assert answer.withheld and answer.guidance and not answer.citations


def test_overview_needs_a_generation_model_and_says_so(db, tmp_path):
    doc, _ = add_doc(db, tmp_path, "집행.txt", ["1분기 집행액은 100만원이다"])
    answer = overview.overview(db, "정리해 줘", Scripted(ready=False), QueryScope(doc_ids=[doc]))
    assert answer.error and "로컬 AI" in answer.error


def test_oversized_selection_is_sampled_evenly_and_disclosed(db, tmp_path):
    assert overview._sample(list(range(100)), 4) == [0, 25, 50, 75]
    pieces = [f"{n}번 항목의 수량은 {n}개이다" for n in range(1, overview.MAX_CHUNKS + 9)]
    doc, _ = add_doc(db, tmp_path, "목록.txt", pieces)
    client = Scripted(map={"facts": []})
    answer = overview.overview(db, "정리해 줘", client, QueryScope(doc_ids=[doc]))
    assert answer.generation["chunks"] == overview.MAX_CHUNKS
    assert answer.generation["chunks_total"] == overview.MAX_CHUNKS + 8


def test_long_pieces_are_batched_by_size_so_the_prompt_fits_the_model_window():
    """실측: 긴 조각 넷을 한 묶음에 넣으면 문맥 창을 넘어 지시문이 잘려 나간다."""
    short = [{"doc_id": 1, "text": "가" * 100} for _ in range(5)]
    assert overview._batches(short) == [[1, 2, 3, 4], [5]]
    long = [{"doc_id": 1, "text": "가" * 1200} for _ in range(5)]
    assert overview._batches(long) == [[1, 2], [3, 4], [5]]


def test_notebook_overview_mode_is_stored_with_the_message(db, book):
    client = Scripted(map={"facts": [{"text": "제출처는 총무과이다", "sources": [2]}]})
    message, answer = notebook.query(db, book, "전체를 정리해 줘", client, mode="overview")
    row = db.con.execute("SELECT * FROM notebook_messages WHERE id=?", (message,)).fetchone()
    assert row["mode"] == "overview" and json.loads(row["generation"])["mode"] == "overview"
    assert json.loads(row["citations"])[0]["exact_quote"] == "제출처는 총무과이다"


# ── 주제 없는 요약 요청 ─────────────────────────────────────────────

@pytest.mark.parametrize("question", ["요약해줘.", "전체를 정리해 줘", "이 파일 무슨 내용이야?", "3줄로 간단히 요약해 주세요"])
def test_bare_summary_requests_are_recognised_by_rule(question):
    assert overview.asks_whole(question)


@pytest.mark.parametrize("question", ["예산 집행 내용을 정리해 줘", "해외 출장 요약", "제출기한은 언제야?",
                                      "이 보고서에서 핵심적으로 주장하는 바는 무엇인지 설명해줘."])
def test_requests_with_a_topic_stay_ordinary_questions(question):
    assert not overview.asks_whole(question)


def test_bare_summary_of_a_small_selection_is_answered_as_an_overview(db, book, monkeypatch):
    """실측: 올린 엑셀에 "요약해줘" — 조각 몇 개만 본 모델이 답을 거절해 '자료 부족'으로 보였다."""
    seen = capture(monkeypatch)
    client = Scripted(map={"facts": [{"text": "제출처는 총무과이다", "sources": [2]}]})
    message, answer = notebook.query(db, book, "요약해줘.", client)
    row = db.con.execute("SELECT * FROM notebook_messages WHERE id=?", (message,)).fetchone()
    assert not answer.withheld and row["mode"] == "overview" and not seen
    notebook.query(db, book, "보관실 이전 제출기한을 정리해 줘", client)
    assert len(seen) == 1                                   # 주제가 있으면 보통 질문이다


def test_bare_summary_is_an_overview_whatever_the_size_of_the_selection(db, tmp_path):
    """실측: 자료가 24조각을 넘자 "요약해줘"가 보통 질문으로 가서 보류됐고, 화면은
    '전체 정리를 켜고 다시 요청하세요'라고 했다. 무엇을 원하는지 알았으면 그렇게 한다."""
    pieces = [f"{n}번 항목의 수량은 {n}개이다" for n in range(1, 40)]
    doc, _ = add_doc(db, tmp_path, "목록.txt", pieces)
    key = notebook.create(db, "큰 자료")
    notebook.add_sources(db, key, [doc])
    client = Scripted(map={"facts": [{"text": pieces[0], "sources": [1]}]})
    steps = []
    message, answer = notebook.query(db, key, "요약해줘.", client, progress=lambda *step: steps.append(step))
    row = db.con.execute("SELECT * FROM notebook_messages WHERE id=?", (message,)).fetchone()
    assert not answer.withheld and row["mode"] == "overview"
    batches = answer.generation["batches"]
    assert batches > 6 and steps == [(done, batches) for done in range(batches)]


def test_bare_summary_without_a_model_says_the_model_is_missing_not_the_material(db, book):
    message, answer = notebook.query(db, book, "요약해줘.", Scripted(ready=False))
    row = db.con.execute("SELECT * FROM notebook_messages WHERE id=?", (message,)).fetchone()
    assert row["mode"] == "rag" and "로컬 AI" in answer.guidance
    assert "로컬 AI" in json.loads(row["generation"])["guidance"]


@pytest.mark.parametrize("code, word", [("subject_unknown", "대상"), ("model_declined", "확정"),
                                        ("unverified", "대조"), ("no_facts", "찾지 못")])
def test_withheld_reason_names_the_cause_instead_of_blaming_the_material(code, word):
    answer = Answer("질문", GENERIC_WITHHELD, True, generation={"withheld": code})
    assert word in withheld_reason(answer) and "부족" not in withheld_reason(answer)


def test_withheld_reason_keeps_specific_text_and_the_generic_line_for_no_evidence():
    assert withheld_reason(Answer("질문", GENERIC_WITHHELD, True)) == GENERIC_WITHHELD
    assert withheld_reason(Answer("질문", "수량을 확인할 수 없습니다.", True,
                                  generation={"withheld": "unverified"})) == "수량을 확인할 수 없습니다."


def test_overview_keeps_the_complete_facts_of_a_cut_off_response(db, tmp_path):
    doc, _ = add_doc(db, tmp_path, "집행.txt", ["1분기 집행액은 100만원이다", "2분기 집행액은 200만원이다"])
    cut = '{"facts": [{"text": "1분기 집행액은 100만원이다", "sources": [1]}, {"text": "2분기 집행액은 2'
    answer = overview.overview(db, "정리해 줘", Scripted(map=cut), QueryScope(doc_ids=[doc]))
    assert answer.text == "· 1분기 집행액은 100만원이다[1]" and answer.generation["map_recovered"] == 1


def test_sheet_is_outlined_by_rule_when_the_whole_material_is_asked(db, tmp_path):
    head = "번호\t곡 테마명\t키워드"
    pieces = ["[테마]\n작사용 테마 목록\n" + head + "\n1\t식어가는 커피\t머그컵, 라떼\n2\t자전거 벨소리\t따르릉, 바게트",
              "[테마]\n" + head + "\n3\t빨래 너는 오후\t수건, 빨랫줄"]
    source = db.add_source(tmp_path)
    path = tmp_path / "테마.xlsx"
    path.write_bytes(b"sheet")
    doc = db.upsert_document(source, {"path": str(path), "filename": path.name, "ext": ".xlsx",
                                      "hash": hashlib.sha256(b"sheet").hexdigest(), "parse_status": "ok"})
    db.replace_document_chunks(doc, [(i, f"'테마' A{i}:C{i}", text) for i, text in enumerate(pieces, 1)])
    answer = overview.overview(db, "요약해줘", Scripted(map={"facts": []}), QueryScope(doc_ids=[doc]))
    assert not answer.withheld and answer.generation["sheet_outlines"] == 1
    assert answer.text == ("· '테마' 시트는 번호·곡 테마명·키워드 열로 된 표이며 3개 행이 있다. "
                           "곡 테마명: 식어가는 커피, 자전거 벨소리, 빨래 너는 오후.[1][2]")
    topical = overview.overview(db, "커피 키워드를 정리해 줘", Scripted(map={"facts": []}), QueryScope(doc_ids=[doc]))
    assert topical.withheld                                 # 주제를 물으면 표 개요로 때우지 않는다
