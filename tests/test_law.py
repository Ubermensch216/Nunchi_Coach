"""현행 법령 조회 — 인용 읽기, law.go.kr 호출, 조문만으로 답하기, 켜짐 관문.

실제 서버를 부르지 않는다. 응답 모양은 공개 API의 JSON을 본뜬 것이다.
"""

import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.core import notebook
from app.db import Database
from app.law import refs, service
from app.law.client import LawClient, LawError, mask
from app.law.refs import LawRef

OC = "secret-oc-key"
PIPA = "개인정보 보호법"
CONTRACT = "지방자치단체를 당사자로 하는 계약에 관한 법률"

SEARCH = {
    PIPA: {"LawSearch": {"law": [
        {"법령일련번호": "248020", "법령ID": "011360", "법령명한글": "개인정보 보호법 시행령",
         "법령구분명": "대통령령", "시행일자": "20230915"},
        {"법령일련번호": "248010", "법령ID": "011357", "법령명한글": PIPA,
         "법령구분명": "법률", "시행일자": "20230915"}]}},
    CONTRACT: {"LawSearch": {"law": [
        {"법령일련번호": "250001", "법령ID": "009999", "법령명한글": CONTRACT,
         "법령구분명": "법률", "시행일자": "20250101"}]}},
}
ARTICLES = {
    ("248010", "001500"): {"법령": {"기본정보": {"법령명_한글": PIPA, "시행일자": "20230915"}, "조문": {"조문단위": [
        {"조문번호": "15", "조문가지번호": "0", "조문제목": "개인정보의 수집ㆍ이용",
         "조문내용": "<![CDATA[제15조(개인정보의 수집ㆍ이용) ① 개인정보처리자는 다음 각 호의 어느 하나에 해당하는 경우에는 개인정보를 수집할 수 있다.]]>",
         "항": [{"항내용": "1. 정보주체의 동의를 받은 경우"},
                {"항내용": "2. 법률에 특별한 규정이 있는 경우 <개정 2023. 3. 14.>"}]}]}}},
    ("250001", "000900"): {"법령": {"기본정보": {"법령명_한글": CONTRACT, "시행일자": "20250101"}, "조문": {"조문단위": [
        {"조문번호": "9", "조문가지번호": "0", "조문제목": "계약의 방법",
         "조문내용": "제9조(계약의 방법) ① 지방자치단체의 장은 계약을 체결하려는 경우에는 일반입찰에 부쳐야 한다."}]}}},
}


class Server:
    """law.go.kr 대역. 받은 요청을 남긴다."""

    def __init__(self, ai=None, fail=None):
        self.requests, self.ai, self.fail = [], ai or [], fail

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.requests.append((url.netloc, url.path, params))
        if self.fail == "network":
            raise httpx.ConnectError("no route")
        if self.fail == "key":
            return httpx.Response(200, json={"LawSearch": {"result": "실패", "msg": f"OC={OC} 인증키가 유효하지 않습니다."}})
        if self.fail == "html":
            return httpx.Response(200, text="<html>사용자 정보 검증에 실패하였습니다</html>")
        if params.get("target") == "law":
            return httpx.Response(200, json=SEARCH.get(params["query"], {"LawSearch": {"law": []}}))
        if params.get("target") == "aiSearch":
            return httpx.Response(200, json={"aiSearch": {"법령조문": self.ai}})
        return httpx.Response(200, json=ARTICLES.get((params.get("MST"), params.get("JO")), {"법령": {}}))

    @property
    def transport(self):
        return httpx.MockTransport(self)

    def sent(self) -> str:
        return json.dumps(self.requests, ensure_ascii=False)


class Llm:
    def __init__(self, response=None, ready=True):
        self.response, self.ready, self.prompts = response, ready, []

    def health(self):
        return SimpleNamespace(generation_ready=self.ready, embedding_ready=False)

    def generate_json(self, prompt, schema, num_predict=400, temperature=0.0):
        self.prompts.append(prompt)
        return (self.response, None, "") if self.response is not None else (None, "응답 없음", "")


@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path / "project" / "project.db")
    db.init()
    yield db
    db.close()


@pytest.fixture
def user_settings(tmp_path, monkeypatch):
    path = tmp_path / "user" / "settings.json"
    monkeypatch.setenv(service.SETTINGS_ENV, str(path))
    return path


# ── 인용 읽기 ───────────────────────────────────────────────────────

def test_citations_are_read_with_article_branch_paragraph_and_item():
    found = refs.extract("이 건은 지방계약법 제9조제1항과 「개인정보 보호법」 제15조①, 민법 제758조의2 제3호를 본다.")
    assert found == [LawRef(CONTRACT, 9, None, 1), LawRef(PIPA, 15, None, 1), LawRef("민법", 758, 2, None, 3)]
    assert found[0].jo_code == "000900" and found[2].jo_code == "075802"
    assert found[2].label == "「민법」 제758조의2 제3호"


@pytest.mark.parametrize("text", ["보고서 작성 방법 제3조를 따른다", "사내 규정 제5조에 따라", "업무 지침 제2조", "제9조에 따른다"])
def test_words_that_only_look_like_statutes_are_not_sent_anywhere(text):
    assert refs.extract(text) == []


@pytest.mark.parametrize("text,name", [
    ("김철수 주무관 건인데 지방계약법", CONTRACT),
    ("우리 부서 계약은 지방자치단체를 당사자로 하는 계약에 관한 법률", CONTRACT),
    ("홍길동이 낸 민원은 민원 처리에 관한 법률", "민원 처리에 관한 법률"),
    ("올해 사업은 도시 및 주거환경정비법", "주거환경정비법"),            # 짧게 잡고 검색이 정식 이름을 찾는다
    ("담당자가 말한 근로기준법 시행령", "근로기준법 시행령"),
    ("그 건은 수도시설의 관리에 관한 법률", "수도시설의 관리에 관한 법률"),
])
def test_leading_words_never_ride_along_with_the_law_name(text, name):
    assert refs.tail_name(text) == name


def test_law_names_without_an_article_are_recognised():
    assert refs.names("개보법에서 수집 동의는 어떻게 정해?") == [PIPA]
    assert refs.names("「공공기록물 관리에 관한 법률」에 따른 보존기간") == ["공공기록물 관리에 관한 법률"]
    assert refs.names("처리 방법이 궁금해") == []


# ── 서버 호출 ───────────────────────────────────────────────────────

def test_article_lookup_resolves_the_exact_law_and_returns_current_text(db):
    server = Server()
    article = LawClient(OC, db, server.transport).article(LawRef(PIPA, 15))
    assert article.law_name == PIPA and article.article == "제15조" and article.title == "개인정보의 수집ㆍ이용"
    assert article.effective_date == "2023-09-15" and article.url.endswith("lsiSeq=248010")
    assert "정보주체의 동의를 받은 경우" in article.text and "<개정 2023. 3. 14.>" in article.text
    assert "CDATA" not in article.text and article.fetched_at
    assert {host for host, _, _ in server.requests} == {"www.law.go.kr"}
    assert [p["target"] for _, _, p in server.requests] == ["law", "lawjosub"]


def test_repeated_lookup_is_served_from_the_project_cache(db):
    server = Server()
    client = LawClient(OC, db, server.transport)
    first = client.article(LawRef(PIPA, 15))
    again = LawClient(OC, db, server.transport).article(LawRef(PIPA, 15))
    assert len(server.requests) == 2 and again.fetched_at == first.fetched_at
    assert OC not in json.dumps([dict(r) for r in db.con.execute("SELECT * FROM law_cache")], ensure_ascii=False)


def test_missing_article_and_unknown_law_are_reported_not_invented(db):
    client = LawClient(OC, db, Server().transport)
    with pytest.raises(LawError) as missing:
        client.article(LawRef(PIPA, 999))
    assert missing.value.kind == "not_found" and "제999조" in str(missing.value)
    with pytest.raises(LawError) as unknown:
        client.article(LawRef("존재하지않는법", 1))
    assert unknown.value.kind == "not_found"


@pytest.mark.parametrize("fail,kind", [("network", "network"), ("key", "upstream"), ("html", "upstream")])
def test_failures_become_plain_messages_that_never_expose_the_key(fail, kind):
    with pytest.raises(LawError) as caught:
        LawClient(OC, transport=Server(fail=fail).transport).search_law("민법")
    assert caught.value.kind == kind and OC not in str(caught.value)


def test_key_is_required_and_masked():
    with pytest.raises(LawError) as caught:
        LawClient("").search_law("민법")
    assert caught.value.kind == "not_configured"
    assert mask(f"https://www.law.go.kr/DRF/lawSearch.do?OC={OC}&target=law", OC).count(OC) == 0


# ── 조문 모으기와 답하기 ────────────────────────────────────────────

def test_cited_question_sends_only_the_law_name_and_article_number(db):
    server = Server()
    question = "김철수 주무관 건인데 지방계약법 제9조에 따르면 계약은 어떻게 체결해?"
    articles, problems = service.gather(LawClient(OC, db, server.transport), question)
    assert [(a.article, origin) for a, origin in articles] == [("제9조", "question")] and not problems
    assert "김철수" not in server.sent() and "주무관" not in server.sent()
    assert "aiSearch" not in server.sent()


def test_uncited_question_uses_the_search_and_reads_the_articles_it_finds(db):
    server = Server(ai=[{"법령명한글": PIPA, "조문번호": "0015", "조문가지번호": "00"}])
    articles, problems = service.gather(LawClient(OC, db, server.transport), "개인정보 수집 동의 요건")
    assert [(a.law_name, a.article, origin) for a, origin in articles] == [(PIPA, "제15조", "search")]
    assert any(p.get("target") == "aiSearch" and p["query"] == "개인정보 수집 동의 요건" for _, _, p in server.requests)


def test_statutes_cited_by_the_documents_are_checked_against_current_text(db):
    server = Server()
    articles, _ = service.gather(LawClient(OC, db, server.transport), "계약 방법은?",
                                 cited_in_documents=[LawRef(CONTRACT, 9)])
    assert [(a.article, origin) for a, origin in articles] == [("제9조", "document")]
    assert "aiSearch" not in server.sent()


def test_nothing_found_gives_a_next_step_and_network_failure_stops_early(db):
    _, problems = service.gather(LawClient(OC, db, Server().transport), "개보법에서 동의는 어떻게 정해?")
    assert "조문 번호" in problems[0]
    server = Server(fail="network")
    _, problems = service.gather(LawClient(OC, db, server.transport), f"{PIPA} 제15조와 지방계약법 제9조")
    assert len(server.requests) == 1 and "연결하지 못했습니다" in problems[0]


def test_answer_uses_only_the_fetched_articles_and_marks_statute_sources(db):
    llm = Llm({"answered": True, "sentences": [
        {"text": "정보주체의 동의를 받은 경우 개인정보를 수집할 수 있다.", "sources": [1]},
        {"text": "위반하면 5천만원 이하의 과태료를 부과한다.", "sources": [1]}]})       # 조문에 없는 숫자
    result = service.answer(db, f"{PIPA} 제15조의 수집 요건은?", llm, LawClient(OC, db, Server().transport))
    assert result["text"] == "정보주체의 동의를 받은 경우 개인정보를 수집할 수 있다.[법1]"
    assert result["citations"][0]["effective_date"] == "2023-09-15" and result["citations"][0]["origin"] == "question"
    assert "[조회한 조문]" in llm.prompts[0] and "정보주체의 동의" in llm.prompts[0]
    assert db.con.execute("SELECT COUNT(*) FROM audit_logs WHERE action='law.lookup'").fetchone()[0] == 1


def test_without_a_generation_model_the_articles_are_still_returned(db):
    result = service.answer(db, f"{PIPA} 제15조", Llm(ready=False), LawClient(OC, db, Server().transport))
    assert result["text"] == "" and result["citations"][0]["article"] == "제15조"


def test_unrelated_articles_do_not_produce_an_answer(db):
    result = service.answer(db, f"{PIPA} 제15조에서 휴가 일수는?", Llm({"answered": False, "sentences": []}),
                            LawClient(OC, db, Server().transport))
    assert result["text"] == "" and "조문 본문을 직접 확인" in result["problems"][0]


# ── 켜짐 관문 ───────────────────────────────────────────────────────

def test_key_lives_in_the_user_folder_and_never_in_the_project(db, user_settings):
    assert not service.available(db)
    service.save_api_key(OC)
    assert service.api_key() == OC and json.loads(user_settings.read_text(encoding="utf-8"))["law_oc"] == OC
    assert not service.available(db)                       # 키가 있어도 인수인계에서 켜야 한다
    service.set_enabled(db, True)
    assert service.available(db)
    assert OC not in json.dumps([tuple(r) for r in db.con.execute("SELECT key,value FROM meta")])
    service.save_api_key("")
    assert not service.available(db)
    with pytest.raises(ValueError):
        service.save_api_key("두 낱말 키")


def book_with_source(db, tmp_path):
    source = db.add_source(tmp_path)
    path = tmp_path / "계약.txt"
    path.write_text("이 계약은 지방계약법 제9조에 따라 일반입찰로 체결한다.", encoding="utf-8")
    doc = db.upsert_document(source, {"path": str(path), "filename": path.name, "ext": ".txt",
                                      "hash": hashlib.sha256(path.read_bytes()).hexdigest(), "parse_status": "ok"})
    db.replace_document_chunks(doc, [(1, "1문단", path.read_text(encoding="utf-8"))])
    key = notebook.create(db, "계약")
    notebook.add_sources(db, key, [doc])
    return key


class Offline:
    def health(self):
        return SimpleNamespace(embedding_ready=False, generation_ready=False)


def test_conversation_never_reaches_out_while_the_feature_is_off(db, tmp_path, user_settings, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("꺼져 있는데 법령 서버를 불렀다")
    monkeypatch.setattr(service, "LawClient", boom)
    key = book_with_source(db, tmp_path)
    message, _ = notebook.query(db, key, "지방계약법 제9조 계약 방법", Offline(), law=True)
    stored = json.loads(db.con.execute("SELECT law_citations FROM notebook_messages WHERE id=?", (message,)).fetchone()[0])
    assert stored["citations"] == [] and "꺼져 있습니다" in stored["problems"][0]
    message, _ = notebook.query(db, key, "지방계약법 제9조 계약 방법", Offline())
    assert json.loads(db.con.execute("SELECT law_citations FROM notebook_messages WHERE id=?", (message,)).fetchone()[0]) == {}


def test_conversation_stores_the_statutes_next_to_the_document_answer(db, tmp_path, user_settings, monkeypatch):
    service.save_api_key(OC)
    service.set_enabled(db, True)
    server = Server()
    monkeypatch.setattr(service, "LawClient", lambda oc, store=None: LawClient(oc, store, server.transport))
    key = book_with_source(db, tmp_path)
    # 질문에는 조문이 없다. 자료가 인용한 조문을 현행 본문으로 확인한다.
    message, answer = notebook.query(db, key, "계약은 어떤 방법으로 체결해?", Offline(), law=True)
    stored = json.loads(db.con.execute("SELECT law_citations FROM notebook_messages WHERE id=?", (message,)).fetchone()[0])
    assert [(c["article"], c["origin"]) for c in stored["citations"]] == [("제9조", "document")]
    assert "일반입찰" in stored["citations"][0]["text"]
    assert "일반입찰로 체결한다" not in server.sent()        # 자료의 문장은 나가지 않는다
    assert answer.citations                                  # 자료 검색 결과는 그대로다
