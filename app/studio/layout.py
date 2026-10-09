"""본문에 서식을 입힌다 — 덩이마다 기호·앞 칸·번호와 글자·문단 모양을 정한다.

    본문(studio/model.py의 덩이) + 서식(format) → 줄(Line) 목록

파일로 그리는 쪽(studio/render.py)과 화면의 미리보기가 같은 줄 목록을 쓴다.
그래야 화면에서 본 모양과 내보낸 파일의 모양이 같다.

기호는 모델에게 맡기지 않고 여기서 규칙으로 맞춘다(onNara.sAIde 공문기안기의
template-format/apply.ts와 같은 방식). 경량 모델은 □ 대신 1., ❍ 대신 ○처럼 기호
체계를 자주 틀린다.
  · 항목 제목: 서식에 기호가 있으면 본문에 적힌 번호를 떼고 서식의 기호를 붙인다.
  · 번호가 있는 단계(1. 가. ① Ⅰ)는 번호를 새로 매긴다. 윗단계가 나오면 아랫단계
    번호는 1부터 다시 센다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import markers, model
from .model import BULLET, HEADING, NOTE, TABLE, TITLE

SLOTS = ("body", "title", "heading", "level0", "level1", "level2", "note")
NOTE_GLYPH = "※"


@dataclass(slots=True)
class Line:
    kind: str                           # model의 덩이 종류
    slot: str                           # 어느 모양을 쓰는가(SLOTS)
    style: dict
    glyph: str = ""
    body: str = ""                      # 기호 뒤의 글
    lead: int = 0                       # 줄 앞 공백 칸 수
    rows: list[list[str]] = field(default_factory=list)

    @property
    def text(self) -> str:
        """앞 칸과 기호까지 붙인 한 줄 — 글자만 넣을 수 있는 곳에 쓴다."""
        return " " * self.lead + (f"{self.glyph} " if self.glyph else "") + self.body


def style_of(fmt: dict, key: str) -> dict:
    return {**fmt.get("body", {}), **fmt.get(key, {})}


def level_of(fmt: dict, level: int) -> dict:
    levels = fmt.get("levels") or [{"glyph": "-", "lead": 1}]
    return {**fmt.get("body", {}), **levels[min(level, len(levels) - 1)], "align": "left"}


def styles(fmt: dict) -> dict[str, dict]:
    """자리(slot)마다의 모양."""
    return {"body": style_of(fmt, "body"), "title": style_of(fmt, "title"), "heading": style_of(fmt, "heading"),
            "note": style_of(fmt, "note"), **{f"level{i}": level_of(fmt, i) for i in range(3)}}


def _glyph(style: dict, counters: dict, slot: str) -> str:
    key, glyphs = style.get("key", ""), style.get("glyphs") or []
    if not markers.is_ordinal(key, glyphs):
        return str(style.get("glyph", ""))
    counters[slot] = counters.get(slot, 0) + 1
    return markers.ordinal_glyph(key, counters[slot], str(style.get("glyph", "")), glyphs)


def lay_out(markdown: str, fmt: dict) -> list[Line]:
    look = styles(fmt)
    counters: dict[str, int] = {}
    lines: list[Line] = []
    for block in model.parse(markdown):
        if block.kind == TABLE:
            lines.append(Line(TABLE, "body", look["body"], rows=block.rows))
        elif block.kind == TITLE:
            lines.append(Line(TITLE, "title", look["title"], body=block.text))
        elif block.kind == HEADING:
            style = look["heading"]
            for slot in ("level0", "level1", "level2"):
                counters.pop(slot, None)
            if not style.get("glyph"):
                lines.append(Line(HEADING, "heading", style, body=block.text))
                continue
            # 본문에는 '1. 보고 배경'처럼 번호가 붙어 있을 수 있다. 서식의 기호로 바꾼다.
            marker = markers.classify(block.text)
            rest = marker.rest if marker and marker.key not in markers.NON_HIERARCHY and marker.rest else block.text
            lines.append(Line(HEADING, "heading", style, _glyph(style, counters, "heading"), rest,
                              int(style.get("lead", 0))))
        elif block.kind == BULLET:
            depth = min(block.level, 2)
            for deeper in range(depth + 1, 3):
                counters.pop(f"level{deeper}", None)
            style = look[f"level{depth}"]
            lines.append(Line(BULLET, f"level{depth}", style, _glyph(style, counters, f"level{depth}") or "-",
                              block.text, int(style.get("lead", 0))))
        elif block.kind == NOTE:
            style = look["note"]
            lines.append(Line(NOTE, "note", style, str(style.get("glyph") or NOTE_GLYPH), block.text,
                              int(style.get("lead", 0))))
        else:
            lines.append(Line(block.kind, "body", look["body"], body=block.text))
    return lines


def sample_markdown(sections: list[str]) -> str:
    """서식 확인용 예시 본문. 원본 문서의 글 대신 쓴다."""
    names = [name for name in sections if name.strip()][:2] or ["추진 배경"]
    out = ["# ○○ 추진 계획 보고", ""]
    for name in names:
        out += [f"## {name}", "- 주요 내용을 개조식으로 적습니다", "  - 세부 사항을 적습니다",
                "    - 세부 내용", ""]
    out.append("> 주석 및 근거")
    return "\n".join(out)
