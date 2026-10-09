"""줄 머리 기호로 문단의 단계를 가린다 — 서식을 읽을 때도, 입힐 때도 쓴다.

같은 단계도 문서마다 기호 글자가 다르다. ○ 단계는 `○`·`❍`·`ㅇ`, · 단계는
`·`·`‧`·`∙`, □ 단계는 `□`이거나 한컴 사용자 영역 글자(U+F03DA)다. 그래서 모양이
아니라 **단계(key)** 로 묶고, 원래 글자(glyph)는 따로 남긴다.

모르는 기호(한컴 원문자 제목 같은 사용자 영역 글자)는 버리지 않고 `sym-…`
단계로 둔다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# (단계, 사람이 읽는 이름, 위아래 순서, 기호를 잡는 식). 순서가 작을수록 윗단계다.
_RULES: list[tuple[str, str, int, re.Pattern[str]]] = [
    ("title", "제목", 0, re.compile(r"^(제\s*목\s*[:：])\s*")),
    ("chapter", "Ⅰ. 대제목", 10, re.compile(r"^([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ][.．]?)\s*")),
    ("section", "□ 소제목", 20, re.compile("^([□■▣◆◇\U000F03DA])\\s*")),
    # 공문(시행문) 개조식: 1. → 가. → 1) → 가) → (1) → (가)
    ("num", "1. 항목", 30, re.compile(r"^(\d{1,2}\.)(?!\d)\s*")),
    ("ga", "가. 항목", 31, re.compile(r"^([가-하]\.)\s+")),
    ("num-paren", "1) 항목", 32, re.compile(r"^(\d{1,2}\))\s*")),
    ("ga-paren", "가) 항목", 33, re.compile(r"^([가-하]\))\s*")),
    ("paren-num", "(1) 항목", 34, re.compile(r"^(\(\d{1,2}\))\s*")),
    ("paren-ga", "(가) 항목", 35, re.compile(r"^(\([가-하]\))\s*")),
    # 보고서형 개조식: ○ → ① → - → · → ▷
    ("item", "○ 항목", 40, re.compile(r"^([○❍◯●]|ㅇ(?=\s))\s*")),
    ("circled", "① 항목", 45, re.compile(r"^([①-⑳])\s*")),
    ("sub", "- 세부항목", 50, re.compile(r"^([-−–―])\s*")),
    ("detail", "· 세부내용", 60, re.compile(r"^([·‧∙•・])\s*")),
    ("arrow", "▷ 참고", 70, re.compile(r"^([▷▶►➢])\s*")),
    ("note", "※ 주석", 80, re.compile(r"^([※*＊])\s*")),
    ("attach", "붙임", 90, re.compile(r"^(붙\s*임)(?=[\s:：\d]|$)\s*[:：]?\s*")),
]
_SYMBOL = re.compile("[←-➿■-◿☀-⛿㈀-㋿]")
_UNKNOWN_RANK = 25      # 모르는 기호는 제목류로 쓰이는 일이 많아 □ 바로 아래에 둔다

# 위아래 순서를 따지지 않는 단계(제목·붙임·주석·일반 문단).
NON_HIERARCHY = {"title", "attach", "note", "body"}
_GA = "가나다라마바사아자차카타파하"
_ROMAN = "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ"
_ORDINAL = {"num", "ga", "num-paren", "ga-paren", "paren-num", "paren-ga", "circled", "chapter"}


@dataclass(slots=True)
class Marker:
    key: str
    label: str
    glyph: str
    rank: int
    rest: str           # 기호를 뺀 글


def is_private(glyph: str) -> bool:
    """한컴 전용 글자(사용자 영역)인가. 한컴 글꼴이 없는 화면에서는 깨져 보인다."""
    code = ord(glyph[0]) if glyph else 0
    return code >= 0xF0000 or 0xE000 <= code <= 0xF8FF


def classify(line: str) -> Marker | None:
    """앞 공백을 뺀 줄의 기호. 기호가 없으면 None."""
    text = line.lstrip(" \t 　")
    if not text:
        return None
    for key, label, rank, pattern in _RULES:
        match = pattern.match(text)
        if match:
            return Marker(key, label, match.group(1), rank, text[match.end():].strip())
    first = text[0]
    if is_private(first) or _SYMBOL.match(first):
        # 원문자 제목처럼 글자마다 코드가 다른 사용자 영역 기호는 한 단계로 묶는다.
        key = "sym-pua" if is_private(first) else f"sym-{ord(first):X}"
        label = f"{first} 중제목" if is_private(first) else f"{first} 기호"
        return Marker(key, label, first, _UNKNOWN_RANK, text[1:].strip())
    return None


def lead_spaces(line: str) -> int:
    """줄 앞 공백 칸 수. 전각 공백은 2칸, 탭은 4칸으로 센다."""
    count = 0
    for char in line:
        if char in "  ":
            count += 1
        elif char == "　":
            count += 2
        elif char == "\t":
            count += 4
        else:
            break
    return count


def rank_of(key: str) -> int:
    if key.startswith("sym-"):
        return _UNKNOWN_RANK
    return next((rank for name, _label, rank, _pattern in _RULES if name == key), 100)


def _consecutive(glyphs: list[str]) -> list[int]:
    codes = sorted(ord(g[0]) for g in glyphs if g)
    return codes if len(codes) > 1 and all(b == a + 1 for a, b in zip(codes, codes[1:])) else []


def is_ordinal(key: str, glyphs: list[str] | None = None) -> bool:
    """번호를 새로 매기는 단계인가(1. 가. ① Ⅰ, 코드가 이어지는 원문자 제목)."""
    return key in _ORDINAL or bool(_consecutive(glyphs or []))


def ordinal_glyph(key: str, number: int, glyph: str = "", glyphs: list[str] | None = None) -> str:
    """number(1부터)째 항목의 기호."""
    ga = _GA[(number - 1) % len(_GA)]
    if key == "num":
        return f"{number}."
    if key == "ga":
        return f"{ga}."
    if key == "num-paren":
        return f"{number})"
    if key == "ga-paren":
        return f"{ga})"
    if key == "paren-num":
        return f"({number})"
    if key == "paren-ga":
        return f"({ga})"
    if key == "circled":
        return chr(0x2460 + min(number, 20) - 1)
    if key == "chapter":
        return _ROMAN[(number - 1) % len(_ROMAN)] + "."
    codes = _consecutive(glyphs or [])
    return chr(codes[0] + number - 1) if codes else glyph
