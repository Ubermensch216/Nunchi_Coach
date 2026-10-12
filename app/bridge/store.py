"""브리지 설정과 연결 목록 — 이 PC의 앱 데이터 폴더에 둔다.

프로젝트 DB에 두지 않는다. 연결은 "이 PC의 눈치코치 ↔ 이 PC의 확장"이라
인수인계를 바꿔 열어도 그대로여야 하고, 인계 묶음·프로젝트 폴더를 따라 다른 PC로
옮겨 가서도 안 된다.

파일에는 토큰의 SHA-256만 남는다. 원문이 없으므로 이 파일을 복사해 가도 연결을
다시 쓸 수 없다.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .policy import DEFAULT_PORT, SCOPES, is_valid_port

FILE_NAME = "bridge.json"
AUDIT_NAME = "bridge-audit.jsonl"
MAX_CLIENTS = 8
MAX_ROOTS = 8
_EXTENSION_ID = re.compile(r"^[a-p]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _client(raw) -> dict | None:
    """파일에서 읽은 연결 하나. 형식이 어긋나면 버린다 — 손상된 항목이 인증을 통과하면 안 된다."""
    if not isinstance(raw, dict):
        return None
    if not (isinstance(raw.get("id"), str) and _EXTENSION_ID.match(str(raw.get("extension_id") or ""))
            and _DIGEST.match(str(raw.get("token_hash") or ""))):
        return None
    scopes = [scope for scope in raw.get("scopes") or [] if scope in SCOPES]
    return {"id": raw["id"][:64], "extension_id": raw["extension_id"], "token_hash": raw["token_hash"],
            "scopes": scopes, "label": str(raw.get("label") or "")[:60],
            "paired_at": str(raw.get("paired_at") or "")[:40]}


class BridgeStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.enabled = False
        self.port = DEFAULT_PORT
        self._clients: list[dict] = []
        # 다운로드 폴더 말고도 꾸러미 알림을 받을 폴더(브라우저의 저장 위치를 따로 정한 경우).
        self.receive_roots: list[str] = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return                      # 없거나 손상됐다 — 꺼진 상태·연결 없음으로 시작한다
        if not isinstance(data, dict):
            return
        self.enabled = data.get("enabled") is True
        if is_valid_port(data.get("port")):
            self.port = data["port"]
        clients = data.get("clients") if isinstance(data.get("clients"), list) else []
        self._clients = [item for item in map(_client, clients) if item][:MAX_CLIENTS]
        roots = data.get("receive_roots") if isinstance(data.get("receive_roots"), list) else []
        self.receive_roots = [root for root in roots if isinstance(root, str) and Path(root).is_absolute()
                              and not root.startswith(("\\\\", "//"))][:MAX_ROOTS]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps({"enabled": self.enabled, "port": self.port, "clients": self._clients,
                                         "receive_roots": self.receive_roots},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        # 백신·색인기가 방금 쓴 파일을 잠깐 붙들면 윈도우가 덮어쓰기를 거절한다. 짧게 다시 시도한다.
        for attempt in range(20):
            try:
                os.replace(temporary, self.path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.1)

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.enabled = bool(enabled)
            self._save()

    def set_port(self, port: int) -> None:
        if not is_valid_port(port):
            raise ValueError("포트는 1024~65535 사이의 숫자여야 합니다.")
        with self._lock:
            self.port = port
            self._save()

    def set_receive_roots(self, roots: list[str]) -> None:
        """수신 폴더 목록을 바꾼다. 네트워크 경로는 받지 않는다 — 꾸러미는 이 PC의 확장이 만든다."""
        cleaned = []
        for root in roots:
            text = str(root)
            if text.startswith(("\\\\", "//")) or not Path(text).is_absolute():
                raise ValueError("이 PC의 폴더만 수신 폴더로 쓸 수 있습니다.")
            if text not in cleaned:
                cleaned.append(text)
        with self._lock:
            self.receive_roots = cleaned[:MAX_ROOTS]
            self._save()

    def clients(self) -> list[dict]:
        """화면에 보여 줄 연결 목록. 토큰 해시는 싣지 않는다."""
        with self._lock:
            return [{key: value for key, value in item.items() if key != "token_hash"} for item in self._clients]

    def find(self, token_hash: str, equal) -> dict | None:
        """해시가 같은 연결. `equal`은 상수 시간 비교 함수다. 모든 항목을 끝까지 견준다."""
        with self._lock:
            found = None
            for item in self._clients:
                if equal(item["token_hash"], token_hash):
                    found = item
            return dict(found) if found else None

    def add_client(self, extension_id: str, token_hash: str, scopes: tuple[str, ...], label: str = "") -> dict:
        """연결을 더한다. 같은 확장의 옛 연결은 지운다 — 다시 연결하면 옛 토큰은 죽는다."""
        client = _client({"id": uuid.uuid4().hex, "extension_id": extension_id, "token_hash": token_hash,
                          "scopes": list(scopes), "label": " ".join(str(label).split()), "paired_at": _now()})
        if client is None:
            raise ValueError("연결 정보가 올바르지 않습니다.")
        with self._lock:
            kept = [item for item in self._clients if item["extension_id"] != extension_id]
            self._clients = (kept + [client])[-MAX_CLIENTS:]
            self._save()
        return {key: value for key, value in client.items() if key != "token_hash"}

    def remove_client(self, client_id: str) -> bool:
        with self._lock:
            kept = [item for item in self._clients if item["id"] != client_id]
            removed = len(kept) != len(self._clients)
            if removed:
                self._clients = kept
                self._save()
            return removed

    def audit(self, action: str, **fields) -> None:
        """연결을 맺고 끊은 일의 기록. 토큰·코드·요청 내용은 적지 않는다."""
        line = json.dumps({"at": _now(), "action": action, **fields}, ensure_ascii=False)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.with_name(AUDIT_NAME).open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            pass                        # 기록을 못 남겼다고 연결을 막지는 않는다
