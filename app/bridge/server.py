"""onNara.sAIde 브리지의 로컬 HTTP 서버. 표준 라이브러리만 쓴다.

이 모듈이 맡는 것은 요청이 지나는 길뿐이다 — Host·Origin 검사, 토큰 인증, 권한,
횟수 제한, 본문 크기, 오류 형식. 무엇을 해 주는지(경로)와 누가 연결됐는지(인증)는
밖에서 넣는다. 그래서 화면 없이 실제 소켓으로 시험할 수 있다.

지키는 것
  · 127.0.0.1에만 연다. 다른 PC에서는 닿지 않는다.
  · CORS 응답 머리글을 보내지 않는다. 확장은 host 권한으로 CORS를 거치지 않고 부르고,
    일반 웹 페이지는 응답을 읽지 못하며 Authorization 머리글의 사전 요청(OPTIONS)도 실패한다.
  · 오류 응답에는 정해 둔 한글 문구만 싣는다. 예외 내용·경로·버전을 내보내지 않는다.
  · 요청 내용을 기록하지 않는다(문서 제목·경로가 지나간다).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import socket
import threading
from urllib.parse import parse_qs, unquote, urlsplit

from ..core import diagnostics
from .policy import FixedWindowLimiter, classify_origin, is_loopback_host, parse_bearer

MAX_JSON_BYTES = 64 * 1024
METHODS = ("GET", "POST", "PUT", "DELETE")


class BridgeError(Exception):
    """사용자에게 그대로 보여도 되는 한글 문구와 함께 돌려주는 오류."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class Client:
    id: str
    extension_id: str
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Context:
    method: str
    params: dict[str, str]
    query: dict[str, list[str]]
    body: object
    client: Client | None            # 익명 경로에서는 None
    origin_extension_id: str | None  # 브라우저가 Origin을 붙였을 때의 확장 ID


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern               # 경로 전체와 맞춘다. 이름 붙은 묶음이 params가 된다
    handler: Callable[[Context], tuple[int, object]]
    # 'none' 익명 · 'optional' 토큰이 맞으면 client가 채워진다 · 그 밖에는 필요한 권한
    auth: str = "none"


@dataclass
class _Settings:
    routes: list[Route]
    authenticate: Callable[[str], Client | None]
    max_json_bytes: int
    requests: FixedWindowLimiter
    auth_failures: FixedWindowLimiter
    port: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "NunchiBridge"
    sys_version = ""
    timeout = 15                      # 느리게 보내며 연결을 붙드는 요청을 끊는다

    def log_message(self, format, *args):   # noqa: A002 — 표준 라이브러리의 이름
        pass

    def _respond(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _serve(self) -> None:
        self.close_connection = True
        try:
            status, payload = self._handle()
        except BridgeError as error:
            status, payload = error.status, {"code": error.code, "message": error.message}
        except Exception as exc:        # 경로의 버그가 내용을 흘리면 안 된다
            diagnostics.record("bridge.error", exc)
            status, payload = 500, {"code": "internal_error", "message": "눈치코치 연결 처리 중 오류가 발생했습니다."}
        try:
            self._respond(status, payload)
        except OSError:                 # 상대가 먼저 끊었다
            pass

    do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_HEAD = do_PATCH = _serve

    def _handle(self) -> tuple[int, object]:
        settings: _Settings = self.server.settings
        if not is_loopback_host(self.headers.get("Host"), settings.port):
            raise BridgeError(403, "host_rejected", "허용되지 않은 호스트 이름입니다.")
        kind, extension_id = classify_origin(self.headers.get("Origin"))
        if kind == "rejected" or self.command == "OPTIONS":
            raise BridgeError(403, "origin_rejected", "웹 페이지에서는 이 연결을 사용할 수 없습니다.")

        url = urlsplit(self.path)
        route, params, path_matched = None, {}, False
        for candidate in settings.routes:
            match = candidate.pattern.fullmatch(url.path)
            if not match:
                continue
            path_matched = True
            if candidate.method == self.command:
                route = candidate
                params = {key: unquote(value) for key, value in match.groupdict().items() if value is not None}
                break
        if route is None:
            if path_matched or self.command not in METHODS:
                raise BridgeError(405, "method_not_allowed", "지원하지 않는 요청 방식입니다.")
            raise BridgeError(404, "not_found", "요청한 기능이 없습니다.")

        client = None
        if route.auth != "none":
            token = parse_bearer(self.headers.get("Authorization"))
            client = settings.authenticate(token) if token else None
            if client is None and route.auth != "optional":
                with settings.lock:
                    allowed = settings.auth_failures.hit("loopback")
                if not allowed:
                    raise BridgeError(429, "rate_limited", "인증 실패가 너무 많습니다. 잠시 후 다시 시도하세요.")
                raise BridgeError(401, "unauthorized", "연결 정보가 없거나 만료되었습니다. 눈치코치와 다시 연결하세요.")
            if client is not None:
                if kind == "extension" and extension_id != client.extension_id:
                    raise BridgeError(403, "origin_mismatch", "연결을 허용한 확장과 요청한 확장이 다릅니다.")
                if route.auth != "optional" and route.auth not in client.scopes:
                    raise BridgeError(403, "scope_denied", "눈치코치 연결 설정에서 이 기능이 허용되지 않았습니다.")
                with settings.lock:
                    allowed = settings.requests.hit(client.id)
                if not allowed:
                    raise BridgeError(429, "rate_limited", "요청이 너무 많습니다. 잠시 후 다시 시도하세요.")

        body = None if self.command in ("GET", "DELETE") else self._read_json(settings.max_json_bytes)
        return route.handler(Context(self.command, params, parse_qs(url.query), body, client, extension_id))

    def _read_json(self, limit: int):
        if self.headers.get("Transfer-Encoding"):
            raise BridgeError(411, "length_required", "본문 길이를 밝힌 요청만 받습니다.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BridgeError(400, "invalid_request", "요청 형식이 올바르지 않습니다.") from None
        if length < 0:
            raise BridgeError(400, "invalid_request", "요청 형식이 올바르지 않습니다.")
        if length > limit:
            # 조금 넘친 본문은 읽어서 버린다. 읽지 않고 닫으면 윈도우가 연결을 끊어(RST)
            # 상대가 이 거절 응답을 받지 못한다. 터무니없이 큰 것은 읽지 않고 닫는다.
            remaining = length if length <= limit * 16 else 0
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 64 * 1024))
                if not chunk:
                    break
                remaining -= len(chunk)
            raise BridgeError(413, "payload_too_large", "요청 본문이 너무 큽니다.")
        if length == 0:
            return None
        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            raise BridgeError(415, "unsupported_media_type", "JSON 본문만 받습니다.")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            raise BridgeError(400, "invalid_json", "요청 본문이 올바른 JSON이 아닙니다.") from None


class _Server(ThreadingHTTPServer):
    """포트를 혼자 쓴다.

    표준 서버는 SO_REUSEADDR을 켠다. 윈도우에서 그 설정은 **다른 프로세스가 같은 포트를
    또 열 수 있다**는 뜻이라, 확장의 요청과 토큰이 엉뚱한 프로그램으로 갈 수 있다.
    """
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:       # 윈도우에만 있다
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


class BridgeServer:
    """127.0.0.1에 여는 서버. `start()`로 열고 `stop()`으로 닫는다. 기본은 닫혀 있다."""

    def __init__(self, port: int, routes: list[Route],
                 authenticate: Callable[[str], Client | None] = lambda token: None,
                 max_json_bytes: int = MAX_JSON_BYTES, requests_per_minute: int = 600,
                 auth_failures_per_minute: int = 30):
        self._requested_port = port
        self._settings = _Settings(list(routes), authenticate, max_json_bytes,
                                   FixedWindowLimiter(requests_per_minute, 60),
                                   FixedWindowLimiter(auth_failures_per_minute, 60))
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int:
        """실제로 연 포트. 0번으로 열면(시험) 운영체제가 고른 번호다."""
        return self._settings.port or self._requested_port

    def start(self) -> None:
        """포트를 연다. 이미 쓰는 포트면 OSError — 조용히 다른 포트로 옮기지 않는다."""
        if self._server is not None:
            return
        server = _Server(("127.0.0.1", self._requested_port), _Handler)
        self._settings.port = server.server_address[1]
        server.settings = self._settings
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="nunchi-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)
