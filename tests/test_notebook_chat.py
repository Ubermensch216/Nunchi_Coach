"""노트북 대화의 검색 다듬기 — 바꿔 쓴 질의, 후속 질문, 전체 정리."""

import hashlib
import json
import re
from types import SimpleNamespace

import pytest

from app.ai.schemas import EXPAND_SCHEMA, MAP_SCHEMA, REDUCE_SCHEMA, REWRITE_SCHEMA
from app.core import notebook
from app.db import Database
from app.search import expand, overview
from app.search.query import QueryScope
from app.search.rag import Answer, Citation, MIN_SIMILARITY, _with_variants, ask
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
                id(MAP_SCHEMA): "map", id(REDUCE_SCHEMA): "reduce"}.get(id(schema), "ask")
        self.calls.append((name, prompt))
        response = self.responses.get(name)
        if callable(response):
            response = response(prompt)
        if response is None:
            return None, "응답 없음", ""
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


def test_notebook_overview_mode_is_stored_with_the_message(db, book):
    client = Scripted(map={"facts": [{"text": "제출처는 총무과이다", "sources": [2]}]})
    message, answer = notebook.query(db, book, "전체를 정리해 줘", client, mode="overview")
    row = db.con.execute("SELECT * FROM notebook_messages WHERE id=?", (message,)).fetchone()
    assert row["mode"] == "overview" and json.loads(row["generation"])["mode"] == "overview"
    assert json.loads(row["citations"])[0]["exact_quote"] == "제출처는 총무과이다"
