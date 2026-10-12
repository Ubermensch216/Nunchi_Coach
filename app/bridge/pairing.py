"""연결 코드와 토큰. 소켓도 화면도 모른다.

연결은 사용자가 눈치코치 설정에서 만든 6자리 코드를 확장 설정에 옮겨 적어 맺는다.
코드는 5분 동안 한 번만 쓸 수 있고, 다섯 번 틀리면 죽는다 — 여섯 자리를 차례로
찍어 맞힐 수 없게 한다. 맺어지면 256비트 토큰을 한 번 내주고 눈치코치에는 그 해시만 남긴다.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import secrets
import time

CODE_TTL_SECONDS = 5 * 60
CODE_MAX_ATTEMPTS = 5


def generate_token() -> str:
    """256비트 난수(base64url). 원문은 응답으로 한 번 나갈 뿐 어디에도 저장하지 않는다."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def digests_equal(left: str, right: str) -> bool:
    """어디서부터 다른지 드러내지 않고 견준다."""
    return bool(left) and hmac.compare_digest(left.encode("ascii", "ignore"), right.encode("ascii", "ignore"))


@dataclass
class PairingCode:
    code: str
    expires_at: float
    attempts: int = 0


def create_code(now: float | None = None) -> PairingCode:
    now = time.time() if now is None else now
    return PairingCode(f"{secrets.randbelow(1_000_000):06d}", now + CODE_TTL_SECONDS)


def check_code(pending: PairingCode | None, submitted, now: float | None = None) -> str:
    """'ok' · 'missing' · 'expired' · 'mismatch' · 'exhausted'. 시도 횟수를 pending에 적는다."""
    now = time.time() if now is None else now
    if pending is None:
        return "missing"
    if now > pending.expires_at:
        return "expired"
    if pending.attempts >= CODE_MAX_ATTEMPTS:
        return "exhausted"
    pending.attempts += 1
    candidate = "".join(submitted.split()) if isinstance(submitted, str) else ""
    if hmac.compare_digest(candidate.encode("utf-8"), pending.code.encode("utf-8")):
        return "ok"
    return "exhausted" if pending.attempts >= CODE_MAX_ATTEMPTS else "mismatch"
