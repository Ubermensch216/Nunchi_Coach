"""생성 호출이 어떻게 끝났는지 가르고, 한도 탓의 빈 응답을 한 번 더 살린다 (B06).

실제 Ollama를 부르지 않는다. 통신 한 겹(_request)만 바꿔 끼워, 서버가 돌려준
종료 사유에 따라 클라이언트가 무엇을 하는지 본다.
"""

import json
from pathlib import Path

import pytest

from app.ai import analyze, schemas
from app.ai.client import OllamaClient
from app.db import Database
from app.jobs.ask_task import _filters_json
from app.search.rag import ask
from app.search.vector import pack

NAME = json.dumps({"name": "실적보고", "description": "정기 실적 보고", "coherent": True},
                  ensure_ascii=False)


class Reply:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def wire(client, replies, capabilities=(), show_fails=0):
    """generate 호출의 payload를 모아 돌려준다. 다른 주소는 정상 응답을 흉내 낸다."""
    calls = []
    failures = [show_fails]

    def request(method, endpoint, *, timeout, **kwargs):
        if endpoint == "/api/show":
            if failures[0]:
                failures[0] -= 1
                raise ValueError("잠시 응답 없음")
            return Reply({"capabilities": list(capabilities)})
        if endpoint == "/api/tags":
            return Reply({"models": [{"name": client.gen_model}, {"name": client.embed_model}]})
        if endpoint == "/api/embed":
            return Reply({"embeddings": [[1.0, 0.0, 0.0] for _ in kwargs["json"]["input"]]})
        calls.append(kwargs["json"])
        return Reply(replies[min(len(calls), len(replies)) - 1])

    client._request = request
    return calls


def test_thinking_is_switched_off_only_for_models_that_declare_it():
    thinker, plain = OllamaClient(), OllamaClient()
    with_think = wire(thinker, [{"response": NAME, "done_reason": "stop"}],
                      capabilities=["completion", "thinking"])
    without = wire(plain, [{"response": NAME, "done_reason": "stop"}], capabilities=["completion"])
    thinker.generate_json("제목", schemas.NAME_TASK_SCHEMA)
    plain.generate_json("제목", schemas.NAME_TASK_SCHEMA)
    assert with_think[0]["think"] is False and thinker.last_generation.think_disabled
    assert "think" not in without[0] and not plain.last_generation.think_disabled


def test_short_task_name_does_not_end_as_an_empty_answer():
    """사고 과정이 한도를 다 써 빈 응답이 오면 넉넉한 한도로 한 번 더 부른다."""
    client = OllamaClient()
    calls = wire(client, [
        {"response": "", "done_reason": "length", "eval_count": 200},
        {"response": NAME, "done_reason": "stop", "eval_count": 41, "prompt_eval_count": 90},
    ])
    naming = analyze.name_task(client, ["2025 실적보고", "2024 실적보고"])
    assert naming.error is None and naming.name == "실적보고"
    assert [c["options"]["num_predict"] for c in calls] == [200, 800]
    record = client.last_generation
    assert (record.outcome, record.attempts, record.done_reason) == ("ok", 2, "stop")
    assert (record.eval_count, record.prompt_eval_count, record.num_predict) == (41, 90, 800)


def test_still_empty_after_the_retry_is_reported_as_a_budget_failure():
    client = OllamaClient()
    calls = wire(client, [{"response": "  ", "done_reason": "length"}])
    data, error, raw = client.generate_json("제목", schemas.NAME_TASK_SCHEMA, num_predict=200)
    assert data is None and raw == "" and "한도" in error
    assert len(calls) == 2 and client.last_generation.outcome == "empty"


def test_empty_answer_without_a_limit_or_on_a_warmup_call_is_not_retried():
    stopped, warmup = OllamaClient(), OllamaClient()
    first = wire(stopped, [{"response": "", "done_reason": "stop"}])
    second = wire(warmup, [{"response": "", "done_reason": "length"}])
    _data, error, _raw = stopped.generate_json("제목", schemas.NAME_TASK_SCHEMA)
    warmup.generate_json("준비", {"type": "object"}, num_predict=1)
    assert len(first) == 1 and "빈 응답" in error and "한도" not in error
    assert len(second) == 1


def test_truncated_and_malformed_json_are_told_apart():
    cut, wrong = OllamaClient(), OllamaClient()
    cut_calls = wire(cut, [{"response": '{"name": "실적', "done_reason": "length"}])
    wire(wrong, [{"response": "이름은 실적보고입니다", "done_reason": "stop"}])
    _d, cut_error, cut_raw = cut.generate_json("제목", schemas.NAME_TASK_SCHEMA)
    _d, wrong_error, _raw = wrong.generate_json("제목", schemas.NAME_TASK_SCHEMA)
    # 끊긴 응답은 원문을 돌려준다 — 건질지는 호출자가 정한다. 다시 부르지 않는다.
    assert len(cut_calls) == 1 and cut_raw and "끊겼" in cut_error
    assert cut.last_generation.outcome == "truncated"
    assert wrong.last_generation.outcome == "invalid_json" and "JSON" in wrong_error


def test_connection_failure_is_an_error_outcome_and_unknown_capability_is_not_cached():
    dead = OllamaClient(base_url="http://127.0.0.1:1")
    assert dead.generate_json("제목", schemas.NAME_TASK_SCHEMA)[0] is None
    assert dead.last_generation.outcome == "error"

    client = OllamaClient()
    calls = wire(client, [{"response": NAME, "done_reason": "stop"}],
                 capabilities=["thinking"], show_fails=1)
    client.generate_json("제목", schemas.NAME_TASK_SCHEMA)
    client.generate_json("제목", schemas.NAME_TASK_SCHEMA)
    assert "think" not in calls[0] and calls[1]["think"] is False


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    source = database.add_source(r"D:\자료")
    doc = database.upsert_document(source, {
        "path": r"D:\자료\실적.hwpx", "filename": "실적.hwpx", "ext": ".hwpx",
        "parse_status": "ok", "hash": "a"})
    chunk_id, _ = database.replace_document_chunks(
        doc, [(1, "1문단", "실적 보고의 제출기한은 9월 30일이다.")])[0]
    database.save_chunk_embedding(chunk_id, "bge-m3", 3, pack([1.0, 0.0, 0.0]))
    yield database
    database.close()


def test_answer_recovered_from_a_cut_response_is_recorded_as_recovered(db):
    cut = ('{"answered": true, "sentences": [{"text": "실적 보고의 제출기한은 9월 30일이다.",'
           ' "sources": [1]}, {"text": "그리고 추가로')
    client = OllamaClient()
    wire(client, [{"response": cut, "done_reason": "length", "eval_count": 900}])
    answer = ask(db, "실적 보고 제출기한이 언제야?", client=client)
    assert not answer.withheld and "9월 30일" in answer.text
    assert answer.generation["outcome"] == "truncated" and answer.generation["recovered"] is True
    saved = json.loads(_filters_json(answer))["generation"]
    assert saved["done_reason"] == "length" and saved["eval_count"] == 900
    # 계측에는 질문도 답도 들어가지 않는다.
    assert "9월" not in json.dumps(saved, ensure_ascii=False)


def test_failed_generation_keeps_its_reason_with_the_withheld_answer(db):
    client = OllamaClient()
    wire(client, [{"response": "", "done_reason": "length"}])
    answer = ask(db, "실적 보고 제출기한이 언제야?", client=client)
    assert answer.withheld and "한도" in answer.error
    assert answer.generation["outcome"] == "empty" and answer.generation["attempts"] == 2


def test_answered_without_sentences_is_a_generation_failure_not_missing_material(db):
    """실제 모델(gemma4:e2b)에서 겪었다: 같은 질문·같은 근거에 빈 문장 목록이 돌아왔다."""
    client = OllamaClient()
    wire(client, [{"response": '{"answered": true, "sentences": []}', "done_reason": "stop",
                   "eval_count": 17}])
    answer = ask(db, "실적 보고 제출기한이 언제야?", client=client)
    assert answer.withheld and answer.text == ""
    # '자료가 부족하다'고 하지 않는다 — 자료는 있고 생성이 실패했다.
    assert "문장을 만들지 못했습니다" in answer.error and "자료가 없다는 뜻은 아닙니다" in answer.error
    assert [doc.filename for doc in answer.related_docs] == ["실적.hwpx"]
    assert answer.generation["empty_answer"] is True and answer.generation["outcome"] == "ok"
    assert answer.generation["empty_answer_retry"] is True      # 한 번은 다시 물었다


def test_answered_without_sentences_is_asked_once_more_for_at_least_one_sentence(db):
    """30문항 평가에서 6문항이 빈 문장 목록으로 끝났다. 문법을 좁혀 한 번 더 묻고,
    받은 문장은 여느 답처럼 근거 검증을 거친다."""
    empty = {"response": '{"answered": true, "sentences": []}', "done_reason": "stop"}
    good = {"response": '{"answered": true, "sentences": [{"text": "실적 보고의 제출기한은 '
                        '9월 30일이다.", "sources": [1]}]}', "done_reason": "stop"}
    client = OllamaClient()
    calls = wire(client, [empty, good])
    answer = ask(db, "실적 보고 제출기한이 언제야?", client=client)
    assert not answer.withheld and "9월 30일" in answer.text and answer.error is None
    assert answer.generation["empty_answer_retry"] is True
    assert "minItems" not in json.dumps(calls[0]["format"])
    assert calls[1]["format"]["properties"]["sentences"]["minItems"] == 1

    # 다시 받은 문장이 근거에 없으면 답으로 올리지 않는다.
    made_up = {"response": '{"answered": true, "sentences": [{"text": "제출기한은 12월 31일이며 '
                           '예산은 5,000만원이다.", "sources": [1]}]}', "done_reason": "stop"}
    client = OllamaClient()
    wire(client, [empty, made_up])
    answer = ask(db, "실적 보고 제출기한이 언제야?", client=client)
    assert answer.withheld and "12월 31일" not in answer.text

    # 다시 물었더니 답할 수 없다고 하면 처음의 빈 답으로 남는다(지어내게 몰지 않는다).
    declined = {"response": '{"answered": false, "sentences": [{"text": "자료 없음", '
                            '"sources": []}]}', "done_reason": "stop"}
    client = OllamaClient()
    wire(client, [empty, declined])
    answer = ask(db, "실적 보고 제출기한이 언제야?", client=client)
    assert answer.withheld and answer.text == "" and answer.generation["empty_answer"] is True
