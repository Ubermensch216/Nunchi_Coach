"""DOCX 파서 (python-docx).

문단과 표를 문서 순서대로 훑는다. 순서를 지켜야 나중에 "3쪽 2문단" 같은
근거 위치가 실제 읽기 순서와 맞는다.
"""

from __future__ import annotations

import re
from pathlib import Path

import docx as _docx
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from .base import (
    EMPTY,
    ENCRYPTED,
    MAX_CHARS,
    OK,
    UNSUPPORTED,
    DocMeta,
    ParseResult,
    Section,
    clip,
    failed,
)

PARSER = "docx"
# 표 조각의 글자 수 상한과, 이어지는 조각에 다시 붙일 첫 행의 길이 상한(xlsx.py와 같은 값).
SECTION_CHARS = 900
HEADER_CHARS = 200
HEADING_CHARS = 80
_HEADING_STYLE = re.compile(r"(?:heading|제목)\s*(\d)", re.IGNORECASE)


def parse(path: Path) -> ParseResult:
    if path.suffix.lower() == ".doc":
        return failed(PARSER, "구형 DOC 형식 — 지원하지 않음", UNSUPPORTED)
    try:
        document = _docx.Document(str(path))
    except Exception as exc:
        message = str(exc).lower()
        if "encrypted" in message or "password" in message:
            return failed(PARSER, "암호로 보호된 문서", ENCRYPTED)
        return failed(PARSER, f"문서 열기 실패: {exc}")

    meta = _read_meta(document)
    budget = [MAX_CHARS]
    out: list[Section] = []
    ordinal = 0
    table_no = 0
    headings: list[tuple[int, str]] = []   # (수준, 제목) — 지금 위치를 감싼 제목들

    for block in _blocks(document):
        path = [title for _level, title in headings]
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue
            body = clip(text, budget)
            if not body:
                break
            level = _heading_level(block)
            if level:
                # 같은 수준이나 더 깊은 제목은 이 제목에서 끝난다. 제목 문단은 제 제목 아래에
                # 든다 — 그래야 제목과 그 본문이 한 조각으로 묶인다(core/chunking.py).
                headings = [h for h in headings if h[0] < level]
                headings.append((level, body[:HEADING_CHARS]))
                path = [title for _level, title in headings]
            ordinal += 1
            out.append(Section("paragraph", ordinal, f"{ordinal}문단", body, _meta(heading=path)))
        else:
            table_no += 1
            rows = _table_rows(block)
            if not rows:
                continue
            pieces = _table_pieces(rows)
            header = rows[0] if len(rows[0]) <= HEADER_CHARS else ""
            stopped = False
            for first, last, lines in pieces:
                body = clip("\n".join(lines), budget)
                if not body:
                    stopped = True
                    break
                ordinal += 1
                locator = f"표{table_no}" if len(pieces) == 1 else f"표{table_no} · {first}~{last}행"
                out.append(Section("table", ordinal, locator, body, _meta(
                    heading=path, table=table_no, rows=[first, last], header=header)))
            if stopped:
                break

    if not out:
        return ParseResult(status=EMPTY, parser=PARSER, meta=meta)
    return ParseResult(status=OK, parser=PARSER, sections=out, meta=meta)


def _blocks(document):
    """본문 자식 요소를 문서 순서대로 문단/표 객체로 돌려준다."""
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def _table_rows(table: Table) -> list[str]:
    rows = []
    for row in table.rows:
        cells = [c.text.strip().replace("\n", " ") for c in row.cells]
        if any(cells):
            rows.append("\t".join(cells))
    return rows


def _table_pieces(rows: list[str]) -> list[tuple[int, int, list[str]]]:
    """표를 (첫 행, 끝 행, 줄들)로 나눈다. 짧은 표는 한 조각이다.

    조각 하나가 core/chunking.py의 MAX_CHUNK_CHARS를 넘으면 글자 수로 잘려 둘째
    조각부터 열 이름을 잃는다 — 값만 남은 행은 무엇의 값인지 알 수 없다. 넘치기 전에
    행 경계에서 닫고, 이어지는 조각의 맨 위에 첫 행을 다시 적는다(xlsx.py와 같은 방식).
    행 번호는 빈 행을 뺀 순서다.
    """
    header = rows[0] if len(rows[0]) <= HEADER_CHARS else ""
    pieces: list[tuple[int, int, list[str]]] = []
    buffer: list[str] = []
    first = 1
    for number, line in enumerate(rows, start=1):
        if buffer and sum(map(len, buffer)) + len(buffer) + len(line) > SECTION_CHARS:
            pieces.append((first, number - 1, buffer))
            buffer, first = ([header] if header else []), number
        buffer.append(line)
    pieces.append((first, len(rows), buffer))
    return pieces


def _heading_level(paragraph: Paragraph) -> int:
    """제목 문단이면 수준(1부터), 아니면 0. 스타일 이름으로만 판정한다 — 굵은 글씨나
    글자 크기로 짐작하지 않는다."""
    try:
        name = (paragraph.style.name or "").strip()
    except Exception:
        return 0
    if name.lower() == "title" or name == "제목":
        return 1
    match = _HEADING_STYLE.fullmatch(name)
    return int(match.group(1)) if match else 0


def _meta(**values) -> dict:
    return {key: value for key, value in values.items() if value}


def _read_meta(document) -> DocMeta:
    props = document.core_properties
    return DocMeta(
        title=(props.title or "").strip() or None,
        author=(props.author or "").strip() or None,
        created=props.created.isoformat() if props.created else None,
        modified=props.modified.isoformat() if props.modified else None,
    )
