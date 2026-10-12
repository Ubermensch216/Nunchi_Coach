"""브리지를 켜고 끄고, 확장과 연결을 맺고 끊는다. 화면(Qt)은 모른다.

앱이 사는 동안 하나만 있다(`install`/`current`). 인수인계를 바꿔 열어도 서버와 연결은
그대로다. 서버의 처리 함수는 작업 스레드에서 돌기 때문에 여기서는 화면을 건드리지 않고,
화면이 필요할 때 상태를 읽어 간다.
"""
from __future__ import annotations

from collections.abc import Callable
import re
import threading

from . import pairing
from .policy import API_VERSION
from .routes import status_route
from .server import BridgeError, BridgeServer, Client, Context, Route
from .store import BridgeStore

# 연결할 때 주는 권한. 꾸러미 알림(쓰기)이 생겨도 이미 맺은 연결에 저절로 붙이지 않는다.
PAIRED_SCOPES = ("read",)


class BridgeService:
    def __init__(self, store: BridgeStore, routes: list[Route] | None = None):
        self.store = store
        self.describe: Callable[[], dict] = dict     # 연결된 확장에 알려 줄 앱 상태(열린 인수인계 이름 등)
        self.error = ""                              # 켜려다 실패한 사유. 화면이 그대로 보여 준다
        self._extra_routes = list(routes or [])
        self._server: BridgeServer | None = None
        self._pending: pairing.PairingCode | None = None
        self._lock = threading.Lock()

    # ── 켜고 끄기 ───────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int:
        return self._server.port if self._server else self.store.port

    def start_if_enabled(self) -> bool:
        return self.start() if self.store.enabled else False

    def start(self) -> bool:
        """서버를 연다. 열지 못하면 False와 함께 `error`에 사유를 남긴다."""
        if self._server is not None:
            return True
        server = BridgeServer(self.store.port, self._routes(), self.authenticate)
        try:
            server.start()
        except OSError:
            self.error = (f"포트 {self.store.port}을(를) 열 수 없습니다. 다른 프로그램이 쓰고 있거나 "
                          "보안 프로그램이 막고 있을 수 있습니다.")
            return False
        self._server, self.error = server, ""
        return True

    def stop(self) -> None:
        server, self._server = self._server, None
        with self._lock:
            self._pending = None
        if server is not None:
            server.stop()

    def set_enabled(self, enabled: bool) -> bool:
        """설정을 저장하고 그대로 켜거나 끈다. 지금 떠 있는지를 돌려준다."""
        self.store.set_enabled(enabled)
        self.store.audit("enable" if enabled else "disable")
        if not enabled:
            self.stop()
            self.error = ""
            return False
        return self.start()

    def set_port(self, port: int) -> bool:
        self.store.set_port(port)
        if not self.store.enabled:
            return False
        self.stop()
        return self.start()

    # ── 연결 ────────────────────────────────────────────────────────
    def new_code(self) -> pairing.PairingCode:
        """연결 코드를 새로 만든다. 앞서 만든 코드는 죽는다."""
        if not self.running:
            raise ValueError("연결을 먼저 켜세요.")
        with self._lock:
            self._pending = pairing.create_code()
            return self._pending

    def pending_code(self) -> pairing.PairingCode | None:
        with self._lock:
            return self._pending

    def clients(self) -> list[dict]:
        return self.store.clients()

    def revoke(self, client_id: str) -> bool:
        removed = self.store.remove_client(client_id)
        if removed:
            self.store.audit("revoke", client_id=client_id)
        return removed

    def authenticate(self, token: str) -> Client | None:
        found = self.store.find(pairing.hash_token(token), pairing.digests_equal)
        return Client(found["id"], found["extension_id"], tuple(found["scopes"])) if found else None

    def _routes(self) -> list[Route]:
        return [status_route(lambda: self.describe()),
                Route("POST", re.compile(r"/bridge/v1/pair"), self._pair), *self._extra_routes]

    def _pair(self, context: Context):
        """`POST /bridge/v1/pair {code, label}` — 코드가 맞으면 토큰을 한 번 내준다.

        브라우저가 붙인 확장 Origin이 있어야 한다. 토큰은 그 확장 ID에 묶이고, 다른 확장이
        같은 토큰을 들고 오면 서버가 거절한다.
        """
        extension_id = context.origin_extension_id
        if not extension_id:
            raise BridgeError(403, "origin_required", "onNara.sAIde 확장에서만 연결할 수 있습니다.")
        body = context.body if isinstance(context.body, dict) else {}
        with self._lock:
            verdict = pairing.check_code(self._pending, body.get("code"))
            if verdict in ("ok", "expired", "exhausted"):
                self._pending = None            # 한 번 쓰였거나 죽은 코드는 치운다
        if verdict != "ok":
            self.store.audit("pair", extension_id=extension_id, result=verdict)
            raise {
                "missing": BridgeError(409, "pairing_not_started", "눈치코치 설정에서 연결 코드를 먼저 만드세요."),
                "expired": BridgeError(410, "pairing_code_expired", "연결 코드의 유효 시간이 지났습니다. 새 코드를 만드세요."),
                "exhausted": BridgeError(429, "pairing_code_exhausted", "연결 코드를 여러 번 틀렸습니다. 새 코드를 만드세요."),
            }.get(verdict, BridgeError(403, "pairing_code_mismatch", "연결 코드가 맞지 않습니다."))
        token = pairing.generate_token()
        client = self.store.add_client(extension_id, pairing.hash_token(token), PAIRED_SCOPES,
                                       label=body.get("label") if isinstance(body.get("label"), str) else "")
        self.store.audit("pair", extension_id=extension_id, client_id=client["id"], result="ok")
        return 200, {"token": token, "clientId": client["id"], "scopes": client["scopes"], "apiVersion": API_VERSION}


_current: BridgeService | None = None


def install(service: BridgeService | None) -> None:
    global _current
    _current = service


def current() -> BridgeService | None:
    """앱이 띄운 브리지. 시험이나 도구처럼 앱 없이 도는 자리에서는 None이다."""
    return _current
