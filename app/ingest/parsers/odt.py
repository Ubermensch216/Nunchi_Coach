"""Read ODF text documents without executing embedded objects or macros."""
from __future__ import annotations

import zipfile
from pathlib import Path
from lxml import etree

from .base import (DocMeta, ParseResult, Section, OK, PARTIAL, EMPTY, ENCRYPTED,
                   MAX_BYTES, MAX_CHARS, MAX_CELLS_PER_SHEET, clip, failed)

PARSER = "odt"
TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"


def _local(node) -> str:
    return node.tag.rsplit("}", 1)[-1] if isinstance(node.tag, str) else ""


def _text(node) -> str:
    parts = [node.text or ""]
    for child in node:
        if child.tag == f"{{{TEXT}}}s":
            parts.append(" " * min(_count(child, f"{{{TEXT}}}c"), 100))
        elif child.tag == f"{{{TEXT}}}tab":
            parts.append("\t")
        elif child.tag == f"{{{TEXT}}}line-break":
            parts.append("\n")
        elif child.tag != f"{{{TABLE}}}table":
            parts.append(_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _count(node, attribute: str) -> int:
    try:
        return max(1, int(node.get(attribute, "1")))
    except ValueError:
        return 1


def parse(path: Path) -> ParseResult:
    with zipfile.ZipFile(path) as archive:
        if sum(info.file_size for info in archive.infolist()) > MAX_BYTES:
            return failed(PARSER, "압축 해제 크기 상한 초과")
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        if "META-INF/manifest.xml" in archive.namelist():
            manifest = etree.fromstring(archive.read("META-INF/manifest.xml"), parser)
            if any(_local(node) == "encryption-data" for node in manifest.iter()):
                return failed(PARSER, "암호화된 ODT", ENCRYPTED)
        if "content.xml" not in archive.namelist():
            return failed(PARSER, "content.xml 없음")
        root = etree.fromstring(archive.read("content.xml"), parser)
        body = root.find(f".//{{{OFFICE}}}body/{{{OFFICE}}}text")
        if body is None:
            return failed(PARSER, "ODF 텍스트 본문 없음")
        meta = DocMeta()
        if "meta.xml" in archive.namelist():
            metadata = etree.fromstring(archive.read("meta.xml"), parser)
            fields = {"title": "title", "initial-creator": "author", "creator": "author",
                      "creation-date": "created", "date": "modified"}
            for node in metadata.iter():
                field = fields.get(_local(node))
                if field and node.text and not getattr(meta, field):
                    setattr(meta, field, node.text.strip())

        sections: list[Section] = []
        budget = [MAX_CHARS]
        table_number = 0
        limited = False

        def emit(kind, locator, text):
            nonlocal limited
            if not text.strip():
                return
            value = clip(text, budget)
            if len(value) < len(text):
                limited = True
            if value:
                sections.append(Section(kind, len(sections) + 1, locator, value))

        def walk(node):
            nonlocal table_number, limited
            if budget[0] <= 0:
                limited = True
                return
            if node.tag in (f"{{{TEXT}}}p", f"{{{TEXT}}}h"):
                emit("paragraph", f"content.xml · {len(sections) + 1}문단", _text(node))
                return
            if node.tag == f"{{{TABLE}}}table":
                table_number += 1
                number = table_number
                row_index = 0
                count = 0
                headers: list[str] = []
                # Exclude rows belonging to nested tables; visit them separately.
                rows = [row for row in node.iter(f"{{{TABLE}}}table-row")
                        if next(row.iterancestors(f"{{{TABLE}}}table"), None) is node]
                for row in rows:
                    cells = []
                    spans = []
                    nested = []
                    for cell in row:
                        if _local(cell) not in ("table-cell", "covered-table-cell"):
                            continue
                        repeats = _count(cell, f"{{{TABLE}}}number-columns-repeated")
                        room = MAX_CELLS_PER_SHEET - count - len(cells)
                        if repeats > max(room, 0):
                            limited = True
                        repeats = min(repeats, max(room, 0))
                        value = "\n".join(_text(p) for p in cell
                                          if p.tag in (f"{{{TEXT}}}p", f"{{{TEXT}}}h"))
                        cells.extend([value] * repeats)
                        width = _count(cell, f"{{{TABLE}}}number-columns-spanned")
                        height = _count(cell, f"{{{TABLE}}}number-rows-spanned")
                        if width > 1 or height > 1:
                            spans.append(f"{len(cells)}열 병합 {width}x{height}")
                        nested.extend(cell.iter(f"{{{TABLE}}}table"))
                    repeat_rows = _count(row, f"{{{TABLE}}}number-rows-repeated")
                    allowed = (MAX_CELLS_PER_SHEET - count) // max(len(cells), 1)
                    if repeat_rows > allowed:
                        limited = True
                    for _ in range(min(repeat_rows, allowed)):
                        row_index += 1
                        count += max(len(cells), 1)
                        if not headers:
                            headers = cells[:]
                        context = ""
                        if row_index > 1 and headers and any(headers):
                            context = "첫 행: " + " | ".join(headers) + "\n"
                        text = context + " | ".join(f"{i}열: {value}" for i, value in enumerate(cells, 1))
                        if spans:
                            text += "\n" + ", ".join(spans)
                        emit("table", f"content.xml · 표 {number} · {row_index}행", text)
                        if budget[0] <= 0:
                            break
                    for inner in nested:
                        walk(inner)
                    if count >= MAX_CELLS_PER_SHEET or budget[0] <= 0:
                        break
                return
            for child in node:
                walk(child)

        walk(body)
        status = PARTIAL if limited else (OK if sections else EMPTY)
        return ParseResult(status=status, parser=PARSER, sections=sections, meta=meta,
                           note="추출 한도에 도달했습니다." if limited else None)
