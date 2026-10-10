"""꼴이 정해진 개인정보 탐지 (행동형 업무 비서 계획서 3절 12번).

찾는 것은 형식이 분명한 셋뿐이다 — 주민등록번호, 전화번호, 전자우편. 이름·주소·
계좌처럼 형식이 일정하지 않은 개인정보는 **찾지 못한다.** '없다'는 결과가 개인정보가
없다는 뜻이 아니다. 화면과 한계 문서에 그렇게 적는다.

찾은 값은 돌려주지 않는다. 종류와 건수만 알려 준다 — 점검 결과가 또 하나의 유출
경로가 되면 안 된다.
"""

from __future__ import annotations

import re

KINDS = {
    "resident": "주민등록번호",
    "phone": "전화번호",
    "email": "전자우편",
}

_PATTERNS = {
    "resident": re.compile(r"(?<!\d)\d{6}\s*-\s*[1-4]\d{6}(?!\d)"),
    "phone": re.compile(r"(?<!\d)0(?:2|[3-6]\d|1[016-9]|70)\s*-\s*\d{3,4}\s*-\s*\d{4}(?!\d)"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
}


def scan(text: str) -> dict[str, int]:
    """종류별 건수. 없으면 빈 사전이다."""
    counts = {kind: len(pattern.findall(text or "")) for kind, pattern in _PATTERNS.items()}
    return {kind: n for kind, n in counts.items() if n}


def merge(*counts: dict[str, int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for one in counts:
        for kind, n in one.items():
            out[kind] = out.get(kind, 0) + n
    return out


def describe(counts: dict[str, int]) -> str:
    return ", ".join(f"{KINDS[kind]} {n}건" for kind, n in counts.items()) or "찾지 못함"
