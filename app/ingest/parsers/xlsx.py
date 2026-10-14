"""XLSX / XLSM 파서 (openpyxl). 구형 XLS도 같은 길로 조각을 만든다(parsers/xls.py).

시트명과 셀 범위를 locator에 남긴다. 검색 근거로 "'검사결과' B4:F28"처럼
표시하려면 파싱 단계에서 좌표를 만들어야 한다(PAR-003).

data_only=True로 열어 수식이 아니라 마지막으로 계산된 표시값을 얻는다.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import openpyxl
from openpyxl.utils import get_column_letter

from .base import (
    EMPTY,
    ENCRYPTED,
    MAX_CELLS_PER_SHEET,
    MAX_CHARS,
    OK,
    PARTIAL,
    DocMeta,
    ParseResult,
    Section,
    clip,
    failed,
)

PARSER = "xlsx"
ROWS_PER_SECTION = 50
# 조각 하나의 글자 수 상한. core/chunking.py의 MAX_CHUNK_CHARS(1200)를 넘으면 조각이
# 글자 수로 강제로 잘려 행이 중간에서 끊긴다(실측: 한 행이 250자쯤인 24행짜리 시트가
# 'ight fluorescent light…'로 시작하는 조각이 됐다). 머리 행을 붙일 여유를 남긴다.
SECTION_CHARS = 900
HEADER_CHARS = 200


def parse(path: Path) -> ParseResult:
    if path.suffix.lower() == ".xls":
        return _parse_xls(path)
    try:
        # 머리글·바닥글 서식이 openpyxl이 못 읽는 꼴이면 경고만 내고 그 부분을 버린다.
        # 본문 추출과 무관하므로 파일마다 콘솔에 경고가 쌓이지 않게 한다.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Cannot parse header or footer")
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        message = str(exc)
        if "encrypted" in message.lower() or "password" in message.lower():
            return failed(PARSER, "암호로 보호된 통합문서", ENCRYPTED)
        return failed(PARSER, f"통합문서 열기 실패: {exc}")
    return _parse_book(wb, PARSER, _read_meta)


def _parse_xls(path: Path) -> ParseResult:
    """구형 XLS는 olefile로 레코드를 읽어 같은 꼴의 통합문서로 만든다(parsers/xls.py)."""
    from . import xls

    try:
        book = xls.open_book(path)
    except xls.Encrypted:
        return failed(xls.PARSER, "암호로 보호된 통합문서", ENCRYPTED)
    except Exception as exc:  # noqa: BLE001 — 손상된 구형 파일 하나가 조사를 멈추지 않게 한다
        return failed(xls.PARSER, f"구형 XLS 열기 실패: {exc}")
    return _parse_book(book, xls.PARSER, lambda b: DocMeta(
        title=b.title, author=b.author,
        created=b.created.isoformat() if b.created else None,
        modified=b.modified.isoformat() if b.modified else None))


def open_workbook(path: Path):
    """읽기 전용 통합문서 — xlsx는 openpyxl, 구형 xls는 parsers/xls. 부른 쪽이 close()한다."""
    if Path(path).suffix.lower() == ".xls":
        from . import xls

        return xls.open_book(path)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore")
        return openpyxl.load_workbook(path, read_only=True, data_only=True)


def _parse_book(wb, parser: str, read_meta) -> ParseResult:
    # openpyxl Workbook은 컨텍스트 매니저가 아니다. read_only 모드에서는
    # 파일 핸들을 잡고 있으므로 close()를 반드시 불러야 원본이 잠기지 않는다.
    try:
        meta = read_meta(wb)
        budget = [MAX_CHARS]
        out: list[Section] = []
        ordinal = 0
        truncated: list[str] = []
        sheets = []
        omissions = []
        char_limited = False

        for ws in wb.worksheets:
            if budget[0] <= 0:
                omissions.append({"reason": "character_limit", "sheet": ws.title,
                                  "unit": "unvisited_sheet", "count": 1})
                continue
            info = {"sheet": ws.title, "declared_rows": ws.max_row, "declared_columns": ws.max_column,
                    "rows_visited": 0, "cells_visited": 0, "nonempty_cells": 0}
            sheets.append(info)
            cells_seen = 0
            buffer: list[str] = []
            first_row = None
            max_col = 0
            row_idx = 0
            last_row = 0
            header = None          # 이어지는 조각에 다시 붙일 머리 행
            continued = False

            def close_section() -> None:
                nonlocal ordinal, buffer, first_row, max_col, continued, char_limited
                if not buffer:
                    return
                ordinal += 1
                lines = [header, *buffer] if continued and header else buffer
                requested = len(f"[{ws.title}]\n" + "\n".join(lines))
                section = _section(ws.title, ordinal, first_row, last_row, max_col, lines, budget, header)
                if len(section.text) < requested:
                    char_limited = True
                    omissions.append({"reason": "character_limit", "sheet": ws.title,
                                      "unit": "section", "rows": [first_row, last_row],
                                      "characters_omitted": requested - len(section.text)})
                out.append(section)
                buffer, first_row, max_col, continued = [], None, 0, True

            # A forged huge sheet width must not allocate an unbounded row tuple.
            width = min(ws.max_column or 1, MAX_CELLS_PER_SHEET)
            for row_idx, row in enumerate(ws.iter_rows(values_only=True, max_col=width), start=1):
                if budget[0] <= 0:
                    char_limited = True
                    omissions.append({"reason": "character_limit", "sheet": ws.title,
                                      "unit": "remaining_rows", "first_row": row_idx,
                                      "count": None})
                    break
                if cells_seen >= MAX_CELLS_PER_SHEET:
                    truncated.append(ws.title)
                    omissions.append({"reason": "cell_limit", "sheet": ws.title,
                                      "unit": "remaining_rows", "first_row": row_idx, "count": None})
                    break
                room = MAX_CELLS_PER_SHEET - cells_seen
                bounded = row[:room]
                values = [_fmt(v) for v in bounded]
                cells_seen += len(values)
                info["rows_visited"] = row_idx
                info["cells_visited"] = cells_seen
                info["nonempty_cells"] += sum(bool(v) for v in values)
                if len(bounded) < len(row) or (ws.max_column or 1) > width:
                    if ws.title not in truncated:
                        truncated.append(ws.title)
                    omissions.append({"reason": "cell_limit", "sheet": ws.title, "unit": "row_tail",
                                      "row": row_idx, "first_column": len(bounded) + 1,
                                      "last_column_declared": ws.max_column})
                if not any(values):
                    continue
                line = "\t".join(values).rstrip()
                # 행 중간에서 끊기지 않게, 넘치기 전에 조각을 닫는다.
                if buffer and (len(buffer) >= ROWS_PER_SECTION
                               or sum(map(len, buffer)) + len(buffer) + len(line) > SECTION_CHARS):
                    close_section()
                    if budget[0] <= 0:
                        char_limited = True
                        omissions.append({"reason": "character_limit", "sheet": ws.title,
                                          "unit": "remaining_rows", "first_row": row_idx, "count": None})
                        break
                if header is None and not continued and _is_header(row, line):
                    header = line
                max_col = max(max_col, len(values))
                if first_row is None:
                    first_row = row_idx
                last_row = row_idx
                buffer.append(line)

            close_section()
    finally:
        wb.close()

    out = [s for s in out if s.text]
    coverage = {"version": 1, "scope": f"{parser}_cached_values", "completeness": "unverified",
                "sheets_total": len(wb.worksheets), "sheets_visited": len(sheets), "sheets": sheets,
                "cells_visited": sum(s["cells_visited"] for s in sheets),
                "characters_emitted": sum(len(s.text) for s in out),
                "character_limit": MAX_CHARS, "cell_limit_per_sheet": MAX_CELLS_PER_SHEET,
                "omissions": omissions,
                "limitations": ["dimensions_are_declared_not_verified", "formula_cache_not_verified",
                                "charts_images_and_formatting_not_extracted"]}
    notes = []
    if truncated:
        notes.append(f"{', '.join(truncated)} 시트는 셀 상한으로 일부만 읽음")
    if char_limited or any(o["unit"] == "unvisited_sheet" for o in omissions):
        notes.append("글자 상한으로 일부만 읽음")
    return ParseResult(status=(PARTIAL if omissions else OK) if out else EMPTY,
                       parser=parser, sections=out, meta=meta, coverage=coverage,
                       note="; ".join(notes) or None)


def _section(sheet, ordinal, first_row, last_row, max_col, buffer, budget, header=None) -> Section:
    start = first_row or 1
    end_col = get_column_letter(max(max_col, 1))
    locator = f"'{sheet}' A{start}:{end_col}{last_row}"
    body = clip(f"[{sheet}]\n" + "\n".join(buffer), budget)
    meta = {"sheet": sheet, "rows": [start, last_row]}
    if header:
        meta["header"] = header
    return Section("sheet", ordinal, locator, body, meta)


def _is_header(row, line: str) -> bool:
    """열 이름으로 볼 만한 행 — 칸이 둘 이상이고 전부 글자다."""
    cells = [v for v in row if v is not None and str(v).strip()]
    return len(cells) >= 2 and len(line) <= HEADER_CHARS and all(isinstance(v, str) for v in cells)


def _fmt(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _read_meta(wb) -> DocMeta:
    props = wb.properties
    return DocMeta(
        title=(props.title or "").strip() or None,
        author=(props.creator or "").strip() or None,
        created=props.created.isoformat() if props.created else None,
        modified=props.modified.isoformat() if props.modified else None,
    )
