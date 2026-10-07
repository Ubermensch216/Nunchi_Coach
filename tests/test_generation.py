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


# ── 30문항 평가에 남았던 두 가지 ────────────────────────────────────

def test_a_why_question_answered_with_a_bare_figure_is_withheld(db):
    """이유를 물었는데 숫자만 답했다. 근거에 있는 숫자여도 묻는 것에 대한 답이 아니다."""
    figure = {"response": '{"answered": true, "sentences": [{"text": "실적 보고의 제출기한은 '
                          '9월 30일이다.", "sources": [1]}]}', "done_reason": "stop"}
    client = OllamaClient()
    wire(client, [figure])
    answer = ask(db, "실적 보고 제출기한이 9월 30일인 이유가 뭐야?", client=client)
    assert answer.withheld and answer.text == "확인 가능한 자료가 부족합니다."
    assert answer.generation["reason_missing"] is True
    assert [doc.filename for doc in answer.related_docs] == ["실적.hwpx"]

    # 같은 답이 이유를 묻지 않는 질문에는 그대로 나간다.
    client = OllamaClient()
    wire(client, [figure])
    assert not ask(db, "실적 보고 제출기한이 언제야?", client=client).withheld


def test_a_why_question_keeps_an_answer_that_states_a_reason(tmp_path):
    database = Database(tmp_path / "why.db")
    database.init()
    doc = database.upsert_document(database.add_source(r"D:\자료"), {
        "path": r"D:\자료\안내.hwpx", "filename": "안내.hwpx", "ext": ".hwpx",
        "parse_status": "ok", "hash": "w"})
    chunk_id, _ = database.replace_document_chunks(
        doc, [(1, "1문단", "결산 일정 때문에 실적 보고의 제출기한을 9월 30일로 앞당겼다.")])[0]
    database.save_chunk_embedding(chunk_id, "bge-m3", 3, pack([1.0, 0.0, 0.0]))
    client = OllamaClient()
    wire(client, [{"response": '{"answered": true, "sentences": [{"text": "결산 일정 때문에 '
                               '제출기한을 9월 30일로 앞당겼다.", "sources": [1]}]}',
                   "done_reason": "stop"}])
    answer = ask(database, "제출기한을 앞당긴 이유가 뭐야?", client=client)
    assert not answer.withheld and "결산 일정 때문에" in answer.text
    database.close()


def test_the_largest_value_in_a_table_row_is_answered_by_rule_without_the_model(tmp_path):
    database = Database(tmp_path / "t.db")
    database.init()
    doc = database.upsert_document(database.add_source(r"D:\자료"), {
        "path": r"D:\자료\실적.xlsx", "filename": "실적.xlsx", "ext": ".xlsx",
        "parse_status": "ok", "hash": "t"})
    chunk_id, _ = database.replace_document_chunks(doc, [(1, "'집계' A1:E3", (
        "[집계]\n구분\t1분기\t2분기\t3분기\t4분기\n완료율\t92\t88\t95\t90"))])[0]
    database.save_chunk_embedding(chunk_id, "bge-m3", 3, pack([1.0, 0.0, 0.0]))
    client = OllamaClient()
    calls = wire(client, [{"response": '{"answered": true, "sentences": [{"text": "4분기의 '
                                       '완료율은 90이다.", "sources": [1]}]}',
                           "done_reason": "stop"}])
    answer = ask(database, "완료율이 가장 높았던 분기의 완료율은 얼마야?", client=client)
    assert not answer.withheld and "3분기의 95이(가) 가장 큽니다" in answer.text
    assert [c.filename for c in answer.citations] == ["실적.xlsx"]
    assert answer.generation == {"rule_answer": "table_extreme"}
    assert calls == []                                    # 생성 모델을 부르지 않았다
    database.close()


def test_a_count_in_one_table_cell_is_answered_by_rule_without_the_model(tmp_path):
    database = Database(tmp_path / "c.db")
    database.init()
    doc = database.upsert_document(database.add_source(r"D:\자료"), {
        "path": r"D:\자료\요구자료접수.docx", "filename": "요구자료접수.docx", "ext": ".docx",
        "parse_status": "ok", "hash": "c"})
    chunk_id, _ = database.replace_document_chunks(doc, [(1, "표1", (
        "항목\t수량\t비고\n요구자료\t12\t제출완료\n추가질의\t3\t답변완료"))])[0]
    database.save_chunk_embedding(chunk_id, "bge-m3", 3, pack([1.0, 0.0, 0.0]))
    client = OllamaClient()
    calls = wire(client, [{"response": '{"answered": true, "sentences": [{"text": "수량은 표를 '
                                       '통해 확인할 수 있습니다.", "sources": [1]}]}',
                           "done_reason": "stop"}])
    answer = ask(database, "요구자료는 몇 건이었어?", client=client)
    assert not answer.withheld and "'요구자료' 행 '수량' 칸 값은 12" in answer.text
    assert answer.generation == {"rule_answer": "table_cell"}
    assert calls == []                                    # 생성 모델을 부르지 않았다
    database.close()


def test_a_refusal_is_asked_once_more_and_a_second_refusal_stands(db):
    """같은 질문·같은 근거에 모델이 답했다 안 했다 한다. 한 번만 다시 묻는다."""
    no = {"response": '{"answered": false, "sentences": []}', "done_reason": "stop"}
    yes = {"response": '{"answered": true, "sentences": [{"text": "실적 보고의 제출기한은 '
                       '9월 30일이다.", "sources": [1]}]}', "done_reason": "stop"}
    client = OllamaClient()
    calls = wire(client, [no, yes])
    answer = ask(db, "실적 보고 제출기한이 언제야?", client=client)
    assert not answer.withheld and "9월 30일" in answer.text
    assert answer.generation["asked_twice"] is True and len(calls) == 2

    client = OllamaClient()
    calls = wire(client, [no, no, yes])
    answer = ask(db, "실적 보고 제출기한이 언제야?", client=client)
    assert answer.withheld and answer.text == "확인 가능한 자료가 부족합니다."
    assert len(calls) == 2                                 # 두 번을 넘겨 묻지 않는다

    # 다시 받은 답도 검증을 거친다.
    made_up = {"response": '{"answered": true, "sentences": [{"text": "제출기한은 12월 31일이다.",'
                           ' "sources": [1]}]}', "done_reason": "stop"}
    client = OllamaClient()
    wire(client, [no, made_up])
    assert ask(db, "실적 보고 제출기한이 언제야?", client=client).withheld


# ── 묻는 것 대신 곁가지를 답함 (60문항 평가) ────────────────────────

def _one_document(tmp_path, name, text):
    database = Database(tmp_path / "p.db")
    database.init()
    doc = database.upsert_document(database.add_source(r"D:\자료"), {
        "path": "D:/자료/" + name, "filename": name, "ext": ".txt",
        "parse_status": "ok", "hash": name})
    chunk_id, _ = database.replace_document_chunks(doc, [(1, "1문단", text)])[0]
    database.save_chunk_embedding(chunk_id, "bge-m3", 3, pack([1.0, 0.0, 0.0]))
    return database


def _answering(client, text):
    return wire(client, [{"response": '{"answered": true, "sentences": [{"text": "%s", '
                                      '"sources": [1]}]}' % text, "done_reason": "stop"}])


CONTRACT = ("2025년 시설물 점검 용역 계약 체결 보고\n계약금액: 48,000천원\n납기: 2025. 11. 28.\n"
            "대금은 준공검사를 마친 뒤 30일 이내에 정산한다.")


def test_an_answer_about_something_else_than_what_was_asked_is_withheld(tmp_path):
    database = _one_document(tmp_path, "2025_용역계약_체결보고.txt", CONTRACT)
    client = OllamaClient()
    # 정산액을 물었는데 계약금액을 답했다. '정산액'은 그 문서에 없다.
    _answering(client, "2025년 시설물 점검 용역 계약금액은 48,000천원이다.")
    answer = ask(database, "시설물 점검 용역 정산액은 얼마야?", client=client)
    assert answer.withheld and answer.generation.get("focus_missing")
    # 묻는 것이 문서에 있으면 그대로 답한다.
    _answering(client, "2025년 시설물 점검 용역 계약금액은 48,000천원이다.")
    answer = ask(database, "시설물 점검 용역 계약금액은 얼마야?", client=client)
    assert not answer.withheld and "48,000" in answer.text
    database.close()


def test_a_when_question_needs_a_time_in_the_answer(tmp_path):
    database = _one_document(tmp_path, "2025_용역계약_체결보고.txt", CONTRACT)
    client = OllamaClient()
    _answering(client, "대금은 준공검사를 마친 뒤 30일 이내에 정산한다.")
    answer = ask(database, "용역의 준공검사는 언제 했어?", client=client)
    assert answer.withheld and answer.generation.get("when_missing")
    # '언제까지'는 기간으로 답할 수 있다.
    _answering(client, "대금은 준공검사를 마친 뒤 30일 이내에 정산한다.")
    assert not ask(database, "용역 대금은 언제까지 정산해야 해?", client=client).withheld
    _answering(client, "납기는 2025. 11. 28.이다.")
    assert not ask(database, "용역의 납기는 언제야?", client=client).withheld
    database.close()


def test_the_focus_of_a_question_is_read_only_when_it_is_clear():
    from app.search.rag import _focus

    assert _focus("2025년 시설물 점검 용역 정산액은 얼마야?") == "정산액"
    assert _focus("3분기 실적 보고를 늦게 내면 어떤 불이익이 있어?") == "불이익"
    assert _focus("의원 질의에는 어떻게 대응했어?") == "질의"
    for unclear in ("작년 예산 요구자료는 어떤 내용이야?", "계약관리 업무는 어떤 순서로 처리해?",
                    "주요업무계획 발표 때 강조할 점이 뭐라고 적혀 있어?", "행정사무감사 담당자 전화번호 알려줘"):
        assert _focus(unclear) == "", unclear
