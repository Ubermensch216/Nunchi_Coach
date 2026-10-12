"""로컬 브리지 서버 — 실제 소켓으로 요청이 지나는 길을 시험한다.

여기서 지키는 것: 이 PC의 확장만 닿는다, 웹 페이지는 응답을 읽지 못한다,
오류가 내용을 흘리지 않는다.
"""

import http.client
import json
import re
import socket

import pytest

from app.bridge import BridgeError, BridgeServer, Client, Route
from app.bridge import policy
from app.bridge.routes import status_route

EXTENSION = "a" * 32
OTHER = "b" * 32
TOKEN = "t" * 43
READ_ONLY = "r" * 43
ORIGIN = f"chrome-extension://{EXTENSION}"


def authenticate(token):
    return {TOKEN: Client("client-1", EXTENSION, ("read", "write")),
            READ_ONLY: Client("client-2", EXTENSION, ("read",))}.get(token)


def boom(context):
    raise RuntimeError(r"C:\Users\secret\project.db 를 열 수 없습니다")


def refuse(context):
    raise BridgeError(409, "busy", "지금은 받을 수 없습니다.")


ROUTES = [
    status_route(lambda: {"project": "시험 프로젝트"}),
    Route("POST", re.compile(r"/bridge/v1/echo"), lambda c: (200, {"body": c.body, "origin": c.origin_extension_id}), auth="write"),
    Route("GET", re.compile(r"/bridge/v1/items/(?P<item>[^/]+)"), lambda c: (200, {"item": c.params["item"], "q": c.query}), auth="read"),
    Route("GET", re.compile(r"/bridge/v1/boom"), boom),
    Route("GET", re.compile(r"/bridge/v1/busy"), refuse),
]


@pytest.fixture
def server():
    server = BridgeServer(0, ROUTES, authenticate, auth_failures_per_minute=3)
    server.start()
    yield server
    server.stop()


def call(server, method, path, headers=None, body=None, host=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", host if host is not None else f"127.0.0.1:{server.port}")
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            connection.putheader("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            connection.putheader(name, value)
        connection.endheaders(data)
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else None, dict(response.getheaders())
    finally:
        connection.close()


def auth(token=TOKEN, **extra):
    return {"Authorization": f"Bearer {token}", **extra}


# ── 판정 규칙 ───────────────────────────────────────────────────────
def test_policy_rules_match_the_tongdal_bridge():
    assert policy.is_loopback_host("127.0.0.1:47831", 47831) and policy.is_loopback_host(" LOCALHOST:47831 ", 47831)
    assert not policy.is_loopback_host("evil.test:47831", 47831) and not policy.is_loopback_host("127.0.0.1:1", 47831)
    assert not policy.is_loopback_host(None, 47831) and not policy.is_loopback_host("127.0.0.1", 47831)
    assert policy.classify_origin(None) == ("none", None) and policy.classify_origin("") == ("none", None)
    assert policy.classify_origin(ORIGIN) == ("extension", EXTENSION)
    for origin in ("https://evil.test", "http://127.0.0.1:47831", "null", "chrome-extension://short",
                   f"chrome-extension://{'z' * 32}", ORIGIN + "/"):
        assert policy.classify_origin(origin) == ("rejected", None)
    assert policy.parse_bearer(f"Bearer {TOKEN}") == TOKEN
    assert policy.parse_bearer("Bearer short") is None and policy.parse_bearer(f"Basic {TOKEN}") is None
    assert policy.parse_bearer(None) is None
    assert policy.is_valid_port(47831) and not policy.is_valid_port(80) and not policy.is_valid_port(True)
    assert policy.DEFAULT_PORT != 47821                              # TONGDAL.ai 브리지와 겹치지 않는다


def test_limiter_counts_within_a_window_and_starts_over_after_it():
    clock = [0.0]
    limiter = policy.FixedWindowLimiter(2, 60, now=lambda: clock[0])
    assert [limiter.hit("a"), limiter.hit("a"), limiter.hit("a")] == [True, True, False]
    assert limiter.hit("b")                                          # 열쇠마다 따로 센다
    clock[0] = 61
    assert limiter.hit("a")


# ── 누가 닿을 수 있는가 ─────────────────────────────────────────────
def test_only_this_pc_can_reach_it(server):
    assert server._server.server_address[0] == "127.0.0.1"
    for host in ("evil.test", f"evil.test:{server.port}", "127.0.0.1:1", ""):
        status, body, _ = call(server, "GET", "/bridge/v1/status", host=host)
        assert (status, body["code"]) == (403, "host_rejected")
    assert call(server, "GET", "/bridge/v1/status", host=f"localhost:{server.port}")[0] == 200


def test_web_pages_are_refused_and_never_get_cors_headers(server):
    for origin in ("https://evil.test", f"http://127.0.0.1:{server.port}", "null"):
        status, body, _ = call(server, "GET", "/bridge/v1/status", {"Origin": origin})
        assert (status, body["code"]) == (403, "origin_rejected")
    # Authorization 머리글을 붙이려는 웹 페이지의 사전 요청
    status, body, headers = call(server, "OPTIONS", "/bridge/v1/echo", {
        "Origin": "https://evil.test", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization"})
    assert (status, body["code"]) == (403, "origin_rejected")
    assert call(server, "OPTIONS", "/bridge/v1/echo", {"Origin": ORIGIN})[0] == 403
    for request in (("GET", "/bridge/v1/status", {"Origin": ORIGIN}), ("GET", "/bridge/v1/status", {"Origin": "https://evil.test"}),
                    ("GET", "/nowhere", None)):
        _, _, headers = call(server, *request)
        assert not [name for name in headers if name.lower().startswith("access-control-")]
        assert headers["Cache-Control"] == "no-store" and headers["X-Content-Type-Options"] == "nosniff"


def test_status_tells_strangers_only_that_the_bridge_is_up(server):
    status, body, _ = call(server, "GET", "/bridge/v1/status")
    assert status == 200 and body["paired"] is False and body["apiVersion"] == policy.API_VERSION
    assert set(body) == {"app", "apiVersion", "appVersion", "paired"}
    assert call(server, "GET", "/bridge/v1/status", auth("x" * 43))[1]["paired"] is False     # 틀린 토큰도 낯선 이다
    status, body, _ = call(server, "GET", "/bridge/v1/status", auth(Origin=ORIGIN))
    assert body["paired"] is True and body["project"] == "시험 프로젝트" and body["scopes"] == ["read", "write"]


def test_token_scope_and_extension_identity_are_all_required(server):
    assert call(server, "POST", "/bridge/v1/echo", body={"a": 1})[1]["code"] == "unauthorized"
    status, body, _ = call(server, "POST", "/bridge/v1/echo", auth(READ_ONLY, **{"Content-Type": "application/json"}), {"a": 1})
    assert (status, body["code"]) == (403, "scope_denied")
    status, body, _ = call(server, "POST", "/bridge/v1/echo",
                           auth(Origin=f"chrome-extension://{OTHER}", **{"Content-Type": "application/json"}), {"a": 1})
    assert (status, body["code"]) == (403, "origin_mismatch")                                  # 토큰을 훔친 다른 확장
    status, body, _ = call(server, "POST", "/bridge/v1/echo", auth(Origin=ORIGIN, **{"Content-Type": "application/json"}),
                           {"제목": "실적 보고"})
    assert (status, body) == (200, {"body": {"제목": "실적 보고"}, "origin": EXTENSION})
    status, body, _ = call(server, "GET", "/bridge/v1/items/%EB%AC%B8%EC%84%9C%201?limit=5", auth(READ_ONLY))
    assert (status, body) == (200, {"item": "문서 1", "q": {"limit": ["5"]}})


def test_repeated_wrong_tokens_are_shut_out_for_a_while(server):
    codes = [call(server, "POST", "/bridge/v1/echo", auth("x" * 43))[1]["code"] for _ in range(5)]
    assert codes == ["unauthorized"] * 3 + ["rate_limited"] * 2


def test_requests_per_client_are_limited():
    server = BridgeServer(0, ROUTES, authenticate, requests_per_minute=2)
    server.start()
    try:
        statuses = [call(server, "GET", "/bridge/v1/items/a", auth())[0] for _ in range(3)]
        assert statuses == [200, 200, 429]
        assert call(server, "GET", "/bridge/v1/items/a", auth(READ_ONLY))[0] == 200            # 다른 연결은 따로 센다
    finally:
        server.stop()


# ── 요청 형식과 오류 ────────────────────────────────────────────────
def test_unknown_paths_and_methods(server):
    assert call(server, "GET", "/")[1]["code"] == "not_found"
    assert call(server, "GET", "/bridge/v1/status/extra")[1]["code"] == "not_found"            # 경로 전체가 맞아야 한다
    assert call(server, "GET", "/bridge/v1/echo")[1]["code"] == "method_not_allowed"
    assert call(server, "PATCH", "/bridge/v1/nowhere")[1]["code"] == "method_not_allowed"


def test_bodies_must_be_small_json(server):
    headers = auth(**{"Content-Type": "application/json"})
    assert call(server, "POST", "/bridge/v1/echo", headers, b"{not json")[1]["code"] == "invalid_json"
    assert call(server, "POST", "/bridge/v1/echo", auth(**{"Content-Type": "text/plain"}), b"hello")[1]["code"] == "unsupported_media_type"
    status, body, _ = call(server, "POST", "/bridge/v1/echo", headers, b'"' + b"x" * (64 * 1024) + b'"')
    assert (status, body["code"]) == (413, "payload_too_large")
    assert call(server, "POST", "/bridge/v1/echo", auth(**{"Transfer-Encoding": "chunked"}))[1]["code"] == "length_required"
    assert call(server, "POST", "/bridge/v1/echo", headers)[1] == {"body": None, "origin": None}   # 본문 없는 요청


def test_errors_carry_fixed_wording_and_never_the_exception(server, monkeypatch):
    recorded = []
    monkeypatch.setattr("app.bridge.server.diagnostics.record", lambda name, *rest: recorded.append(name))
    status, body, _ = call(server, "GET", "/bridge/v1/boom")
    assert (status, body) == (500, {"code": "internal_error", "message": "눈치코치 연결 처리 중 오류가 발생했습니다."})
    assert recorded == ["bridge.error"]
    assert call(server, "GET", "/bridge/v1/busy")[:2] == (409, {"code": "busy", "message": "지금은 받을 수 없습니다."})
    assert call(server, "GET", "/bridge/v1/status")[0] == 200                                  # 오류 뒤에도 계속 받는다


# ── 열고 닫기 ───────────────────────────────────────────────────────
def test_stop_releases_the_port_and_a_taken_port_is_an_error():
    server = BridgeServer(0, ROUTES)
    assert not server.running
    server.start()
    server.start()                                                    # 두 번 열어도 하나다
    port = server.port
    with pytest.raises(OSError):
        BridgeServer(port, ROUTES).start()                           # 조용히 다른 포트로 옮기지 않는다
    server.stop()
    server.stop()
    assert not server.running
    with socket.socket() as probe:
        probe.settimeout(2)
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    again = BridgeServer(port, ROUTES)
    again.start()
    try:
        assert call(again, "GET", "/bridge/v1/status")[0] == 200
    finally:
        again.stop()
