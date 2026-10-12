"""브리지를 켜고 끄고, 확장과 연결을 맺고 끊는다. 화면(Qt)은 모른다.

앱이 사는 동안 하나만 있다(`install`/`current`). 인수인계를 바꿔 열어도 서버와 연결은
그대로다. 서버의 처리 함수는 작업 스레드에서 돌기 때문에 여기서는 화면을 건드리지 않고,
화면이 필요할 때 상태를 읽어 간다.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from . import pairing
from .packages import downloads_folder, resolve_package
from .policy import API_VERSION
from .routes import status_route
from .server import BridgeError, BridgeServer, Client, Context, Route
from .store import BridgeStore

# 연결할 때 주는 권한. read: 상태·보유 문서 조회, write: 꾸러미 준비 알림.
# 권한을 늘려도 이미 맺은 연결에 저절로 붙이지 않는다 — 다시 연결해야 받는다.
PAIRED_SCOPES = ("read", "write")
MAX_ARRIVALS = 20
MAX_KNOWN_IDS = 500


class BridgeService:
    def __init__(self, store: BridgeStore, routes: list[Route] | None = None):
        self.store = store
        self.describe: Callable[[], dict] = dict     # 연결된 확장에 알려 줄 앱 상태(열린 인수인계 이름 등)
        self.error = ""                              # 켜려다 실패한 사유. 화면이 그대로 보여 준다
        self._extra_routes = list(routes or [])
        self._server: BridgeServer | None = None
        self._pending: pairing.PairingCode | None = None
        self._lock = threading.Lock()
        # 지금 열어 둔 인수인계의 DB 파일. 보유 문서 조회가 읽기 전용으로 따로 연다(서버 스레드).
        self.project_db: Path | None = None
        # 확장이 알려 온 꾸러미. 메모리에만 둔다 — 앱을 다시 켜면 폴더를 직접 고르면 된다.
        self._arrivals: dict[str, dict] = {}

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
                Route("POST", re.compile(r"/bridge/v1/pair"), self._pair),
                Route("POST", re.compile(r"/bridge/v1/packages"), self._announce, auth="write"),
                Route("GET", re.compile(r"/bridge/v1/packages/(?P<run>[A-Za-z0-9_-]{6,80})"), self._arrival, auth="read"),
                Route("POST", re.compile(r"/bridge/v1/known"), self._known, auth="read"),
                *self._extra_routes]

    # ── 꾸러미 알림 ─────────────────────────────────────────────────
    def receive_roots(self) -> list[Path]:
        """꾸러미 알림을 받는 폴더 — 다운로드 폴더와 사용자가 더한 폴더."""
        default = downloads_folder()
        return ([default] if default else []) + [Path(root) for root in self.store.receive_roots]

    def _announce(self, context: Context):
        """`POST /bridge/v1/packages {runId, path}` — 꾸러미가 준비됐다는 알림.

        받아 두기만 한다. 자료로 등록할지는 사용자가 눈치코치 화면에서 정한다.
        """
        body = context.body if isinstance(context.body, dict) else {}
        run_id = body.get("runId")
        try:
            folder, connector = resolve_package(body.get("path"), run_id, self.receive_roots())
        except BridgeError as error:
            self.store.audit("package", client_id=context.client.id, result=error.code)
            raise
        files = sum(len(items) for items in connector.assets.values())
        with self._lock:
            known = self._arrivals.get(run_id)
            # 이미 묻고 있거나 등록한 꾸러미는 다시 묻지 않는다. 닫았거나 실패한 것은 다시 알리면 다시 묻는다.
            if known is None or known["state"] in ("dismissed", "failed"):
                self._arrivals[run_id] = {"run_id": run_id, "path": str(folder), "state": "queued",
                                          "documents": len(connector.records), "files": files,
                                          "received_at": time.time()}
                for stale in list(self._arrivals)[:-MAX_ARRIVALS]:
                    del self._arrivals[stale]
            state = self._arrivals[run_id]["state"]
        self.store.audit("package", client_id=context.client.id, run_id=run_id, result="ok")
        return 202, {"runId": run_id, "state": state, "documents": len(connector.records), "files": files,
                     "project": self.describe().get("project")}

    def _arrival(self, context: Context):
        """`GET /bridge/v1/packages/{runId}` — 알린 꾸러미가 어떻게 됐는지."""
        with self._lock:
            arrival = self._arrivals.get(context.params["run"])
            if arrival is None:
                raise BridgeError(404, "not_found", "알린 적이 없는 꾸러미입니다.")
            return 200, {"runId": arrival["run_id"], "state": arrival["state"],
                         "documents": arrival["documents"], "files": arrival["files"]}

    def take_arrivals(self) -> list[dict]:
        """화면이 아직 묻지 않은 꾸러미를 가져간다(주 스레드). 가져간 것은 'asked'가 된다."""
        with self._lock:
            fresh = [dict(item) for item in self._arrivals.values() if item["state"] == "queued"]
            for item in fresh:
                self._arrivals[item["run_id"]]["state"] = "asked"
            return fresh

    def settle_arrival(self, run_id: str, state: str) -> None:
        """사용자의 결정을 적는다 — 'imported' · 'dismissed' · 'failed'."""
        with self._lock:
            if run_id in self._arrivals and state in ("imported", "dismissed", "failed"):
                self._arrivals[run_id]["state"] = state

    # ── 보유 문서 조회 ──────────────────────────────────────────────
    def _known(self, context: Context):
        """`POST /bridge/v1/known {origin, ids}` — 열어 둔 인수인계가 이미 온전히 가진 문서.

        '온전히'는 본문을 받았고 받지 못한 파일이 없다는 뜻이다. 붙임 하나라도 빠진 문서는
        가진 것으로 치지 않는다 — 다시 내보내면 채울 수 있어야 한다.
        """
        body = context.body if isinstance(context.body, dict) else {}
        ids = body.get("ids")
        host = urlsplit(str(body.get("origin") or "")).netloc
        if (not host or not isinstance(ids, list) or len(ids) > MAX_KNOWN_IDS
                or not all(isinstance(item, str) and 0 < len(item) <= 200 for item in ids)):
            raise BridgeError(400, "invalid_request", "보유 문서 조회의 형식이 올바르지 않습니다.")
        database = self.project_db
        if database is None or not Path(database).is_file():
            raise BridgeError(409, "no_project", "눈치코치에서 인수인계를 먼저 여세요.")
        if not ids:
            return 200, {"known": [], "project": self.describe().get("project")}
        connection = sqlite3.connect(f"{Path(database).as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            marks = ",".join("?" * len(ids))
            rows = connection.execute(
                f"SELECT r.document_id FROM source_records r WHERE r.system_id=? AND r.document_id IN ({marks})"
                " AND EXISTS (SELECT 1 FROM source_assets a WHERE a.record_id=r.id AND a.role='body' AND a.status='downloaded')"
                " AND NOT EXISTS (SELECT 1 FROM source_assets a WHERE a.record_id=r.id AND a.status!='downloaded')",
                (f"onnara@{host}", *ids)).fetchall()
        except sqlite3.Error:
            raise BridgeError(503, "project_busy", "인수인계 자료를 지금 읽을 수 없습니다. 잠시 후 다시 시도하세요.") from None
        finally:
            connection.close()
        return 200, {"known": [row[0] for row in rows], "project": self.describe().get("project")}

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
