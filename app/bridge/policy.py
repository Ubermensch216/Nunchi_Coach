"""onNara.sAIde 브리지의 보안 판정. 소켓도 화면도 모른다 — 그대로 시험할 수 있게.

규칙은 TONGDAL.ai 브리지(`electron/bridge/policy.ts`)와 같다. 두 브리지를 같은 확장이
부르므로 판정이 달라지면 한쪽에서만 통하는 요청이 생긴다.
"""
from __future__ import annotations

import re
import time

API_VERSION = 1
# TONGDAL.ai 브리지(47821)와 겹치지 않게 둔다. 두 앱이 같은 PC에서 함께 돈다.
DEFAULT_PORT = 47831

SCOPES = ("read", "write")

# 크로미움 확장 ID는 a–p 32글자다.
_EXTENSION_ORIGIN = re.compile(r"^chrome-extension://([a-p]{32})$")
_BEARER = re.compile(r"^Bearer\s+([A-Za-z0-9_-]{32,128})$")


def is_valid_port(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1024 <= value <= 65535


def is_loopback_host(host_header: str | None, port: int) -> bool:
    """Host 머리글이 이 PC의 이름인가.

    웹 페이지는 자기 도메인을 127.0.0.1로 풀어(DNS 리바인딩) 이 서버와 같은 출처인
    척할 수 있다. 그런 요청에도 Host에는 공격자의 이름이 남으므로 loopback 이름만 받는다.
    """
    if not host_header:
        return False
    host = host_header.strip().lower()
    return host in (f"127.0.0.1:{port}", f"localhost:{port}")


def classify_origin(origin: str | None) -> tuple[str, str | None]:
    """('none', None) · ('extension', 확장 ID) · ('rejected', None).

    웹 페이지는 다른 출처로 보내는 요청에 Origin을 반드시 붙인다. 그래서 http(s) Origin은
    곧바로 거절한다. Origin이 없는 요청(curl, 브라우저가 붙이지 않은 GET)은 받되,
    토큰은 여전히 맞아야 한다.
    """
    if origin is None or origin == "":
        return "none", None
    match = _EXTENSION_ORIGIN.match(origin.strip())
    return ("extension", match.group(1)) if match else ("rejected", None)


def parse_bearer(header: str | None) -> str | None:
    match = _BEARER.match(header.strip()) if header else None
    return match.group(1) if match else None


class FixedWindowLimiter:
    """열쇠별로 정해진 시간 창 안의 횟수를 센다. 작고 메모리에만 둔다."""

    def __init__(self, limit: int, window_seconds: float, now=time.monotonic):
        self.limit = limit
        self.window = window_seconds
        self._now = now
        self._windows: dict[str, list[float]] = {}

    def hit(self, key: str) -> bool:
        """한 번 세고, 아직 한도 안인지 돌려준다."""
        now = self._now()
        current = self._windows.get(key)
        if current is None or now - current[0] >= self.window:
            self._windows[key] = [now, 1]
            return True
        current[1] += 1
        return current[1] <= self.limit
