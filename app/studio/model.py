"""산출물 본문의 모양 — 사람이 고치는 글과, 파일로 그리는 덩이 사이.

본문은 간단한 표기(마크다운의 일부)로 저장한다. 사람이 메모장처럼 고칠 수
있어야 하고, 같은 글에서 MD·DOCX·HWPX·PDF가 모두 나와야 하기 때문이다.

    # 문서 제목
    ## 1. 항목 제목
    - 첫 단계 문장 [1]
      - 둘째 단계 문장
    일반 문단
    | 구분 | 내용 |          ← 표. 둘째 줄의 |---|---|는 있어도 없어도 된다
    > 확인 필요: …           ← 주의·빈칸 표시
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TITLE, HEADING, BULLET, PARAGRAPH, TABLE, NOTE = "title", "heading", "bullet", "paragraph", "table", "note"
MISSING = "확인 필요: 선택한 자료에서 이 항목의 근거를 찾지 못했습니다."
SOURCES_HEADING = "출처"

_BULLET = re.compile(r"^(?P<indent>\s*)(?:[-*·○□▪•]|\d+[.)])\s+(?P<text>.+)$")
_DIVIDER = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_MARK = re.compile(r"\[(\d+)\]")


@dataclass(slots=True)
class Block:
    kind: str
    text: str = ""
    level: int = 0                      # 목록의 단계(0부터)
    rows: list[list[str]] = field(default_factory=list)


def _cells(line: str) -> list[str] | None:
    text = line.strip()
    if not text.startswith("|") or text.count("|") < 2:
        return None
    return [cell.strip() for cell in text.strip("|").split("|")]


def parse(markdown: str) -> list[Block]:
    """본문을 덩이로 나눈다. 모르는 표기는 문단으로 둔다 — 글을 잃지 않는다."""
    blocks: list[Block] = []
    lines = str(markdown or "").replace("\r\n", "\n").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        index += 1
        if not stripped:
            continue
        if stripped.startswith("# "):
            blocks.append(Block(TITLE, stripped[2:].strip()))
        elif stripped.startswith("#"):
            blocks.append(Block(HEADING, stripped.lstrip("#").strip()))
        elif stripped.startswith(">"):
            blocks.append(Block(NOTE, stripped.lstrip("> ").strip()))
        elif _cells(line) is not None:
            rows = [_cells(line)]
            while index < len(lines) and (_cells(lines[index]) is not None or _DIVIDER.match(lines[index])):
                if not _DIVIDER.match(lines[index]):
                    rows.append(_cells(lines[index]))
                index += 1
            width = max(len(row) for row in rows)
            blocks.append(Block(TABLE, rows=[row + [""] * (width - len(row)) for row in rows]))
        elif (match := _BULLET.match(line)) is not None:
            indent = len(match["indent"].replace("\t", "  "))
            blocks.append(Block(BULLET, match["text"].strip(), level=min(indent // 2, 2)))
        else:
            blocks.append(Block(PARAGRAPH, stripped))
    return blocks


def cited(markdown: str) -> set[int]:
    """본문이 실제로 쓰는 근거 번호. 출처 목록의 번호 자체는 세지 않는다."""
    body = re.split(rf"(?m)^##\s*{SOURCES_HEADING}\s*$", str(markdown or ""))[0]
    return {int(n) for n in _MARK.findall(body)}


def title_of(markdown: str, fallback: str = "") -> str:
    for block in parse(markdown):
        if block.kind == TITLE:
            return block.text
    return fallback


def plain(markdown: str) -> str:
    """미리보기·글자 수 세기용 평문."""
    out = []
    for block in parse(markdown):
        if block.kind == TABLE:
            out += ["  ".join(row) for row in block.rows]
        else:
            out.append(("  " * block.level + block.text) if block.kind == BULLET else block.text)
    return "\n".join(out)
