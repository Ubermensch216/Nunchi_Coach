"""HWPX 파서.

HWPX는 ZIP + XML이므로 외부 의존성 없이 안정적으로 읽을 수 있다.
본문은 Contents/section*.xml 안의 <hp:p>(문단) / <hp:t>(텍스트 런) 구조다.
표는 문단 안에 <hp:tbl> → <hp:tr> → <hp:tc>로 들어 있고, 셀마다
<hp:cellAddr>(행·열 위치)와 <hp:cellSpan>(병합 크기)을 가진다.

네임스페이스 URI가 생성 버전마다 달라질 수 있어 local-name으로만 판별한다.

표는 행 단위로 내보낸다(ODT 파서와 같은 형식). 셀을 문단처럼 흩어 놓으면
'820'이 어느 항목의 무슨 값인지 잃는다 — 실제 인계 자료의 금액·기한은
대부분 표 안에 있다. 각 행은 첫 행(또는 제목 셀로 지정된 행)과 단위를 함께
담아 그 조각만으로 읽을 수 있게 한다.

  · 병합 셀의 값은 덮는 모든 칸에 채운다. 세로 병합된 '구분' 아래 행들이
    자기 구분을 잃지 않게 하기 위해서다.
  · 첫 행이 머리글이라고 단정하지 않는다. 제목 셀 표시(header="1")가 있으면
    '머리글', 없으면 '첫 행'이라고 사실대로 적는다.
  · 한 칸짜리 표(글상자 용도)와 한 열짜리 표는 본문처럼 그대로 적는다.
"""

from __future__ import annotations

from collections import Counter
import zipfile
import re
from pathlib import Path

from lxml import etree

from .base import (
    EMPTY,
    ENCRYPTED,
    MAX_CELLS_PER_SHEET,
    MAX_CHARS,
    OK,
    PARTIAL,
    UNSUPPORTED,
    DocMeta,
    ParseResult,
    Section,
    clip,
    failed,
)

PARSER = "hwpx"

# "(단위: 천원)" · "[단위 : 백만원]". 표 바로 앞 문단이나 캡션에 적힌다.
_UNIT = re.compile(r"단위\s*[:：]\s*([^)\]\n]{1,20})")
# 단위 안내는 짧은 한 줄이다. 긴 본문 속의 '단위:'를 표의 단위로 넘겨짚지 않는다.
_UNIT_LINE_MAX = 40
_INLINE = {"tab": "\t", "lineBreak": "\n", "fwSpace": " ", "nbSpace": " "}


def parse(path: Path) -> ParseResult:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        return failed(PARSER, f"ZIP 구조 손상: {exc}")

    with zf:
        names = zf.namelist()
        if any(n.startswith("META-INF/") and "encryption" in n.lower() for n in names):
            return failed(PARSER, "암호화된 HWPX", ENCRYPTED)

        sections_xml = sorted(
            (n for n in names if re.fullmatch(r"contents/section\d+\.xml", n.lower())),
            key=lambda n: int(re.search(r"section(\d+)", n.lower()).group(1)),
        )
        if not sections_xml:
            return failed(PARSER, "Contents/section*.xml 없음", UNSUPPORTED)

        meta = _read_meta(zf, names)
        writer = _Writer()
        writer.parts_total = len(sections_xml)

        for name in sections_xml:
            writer.parts_visited += 1
            try:
                root = etree.fromstring(zf.read(name), etree.XMLParser(resolve_entities=False, no_network=True))
            except etree.XMLSyntaxError as exc:
                writer.omit("xml_parse_error", name)
                result = failed(PARSER, f"{name} XML 오류: {exc}")
                result.coverage = writer.coverage()
                return result
            writer.parts_parsed += 1
            for node in root.iter():
                kind = _local(node)
                if kind in {"p", "tbl", "tr", "tc", "t"}:
                    writer.inventory[kind] += 1
                if kind == "t" and "".join(node.itertext()).strip():
                    writer.inventory["nonempty_text_runs"] += 1
                if kind == "cellSpan" and (_int(node, "colSpan", 1) > 1 or _int(node, "rowSpan", 1) > 1):
                    writer.inventory["merged_cells"] += 1
            writer.name = name
            writer.table_number = 0
            writer.preceding = ""
            _body(root, writer)
            if writer.budget[0] <= 0:
                if writer.parts_parsed < writer.parts_total:
                    writer.omit("unvisited_xml_parts", name)
                break

    if not writer.out:
        return ParseResult(status=EMPTY, parser=PARSER, meta=meta, coverage=writer.coverage())
    return ParseResult(
        status=PARTIAL if writer.limited else OK, parser=PARSER, sections=writer.out, meta=meta,
        note="일부 구조를 추출하지 못했습니다. 추출 범위 진단을 확인하세요." if writer.limited else None,
        coverage=writer.coverage(),
    )


class _Writer:
    """문서 순서대로 조각을 쌓는다. 추출 한도에 걸렸는지도 여기서 센다."""

    def __init__(self) -> None:
        self.out: list[Section] = []
        self.budget = [MAX_CHARS]
        self.limited = False
        self.name = ""
        self.table_number = 0
        self.preceding = ""   # 표 바로 앞 문단 — 단위 안내를 찾는 데만 쓴다
        self.parts_total = self.parts_parsed = self.parts_visited = 0
        self.inventory = Counter()
        self.tables_visited = self.cells_visited = self.merges_visited = 0
        self.omission_counts = Counter()
        self.omission_samples = {}

    def omit(self, reason, locator):
        self.limited = True
        self.omission_counts[reason] += 1
        samples = self.omission_samples.setdefault(reason, [])
        if len(samples) < 100:
            samples.append(locator)

    def coverage(self):
        return {"version": 1, "scope": "hwpx_xml_text_and_table_rows", "completeness": "unverified",
                "xml_parts_total": self.parts_total, "xml_parts_parsed": self.parts_parsed,
                "xml_parts_visited": self.parts_visited,
                "xml_parts_failed": self.parts_visited - self.parts_parsed,
                "xml_parts_unvisited": self.parts_total - self.parts_visited,
                "inventory_scope": "successfully_parsed_xml_parts",
                "parsed_xml_inventory": dict(self.inventory),
                "tables_visited": self.tables_visited, "table_cells_visited": self.cells_visited,
                "merged_cells_visited": self.merges_visited,
                "table_rows_emitted": sum(s.kind == "table" for s in self.out),
                "characters_emitted": sum(len(s.text) for s in self.out),
                "character_limit": MAX_CHARS, "grid_slot_limit_per_table": MAX_CELLS_PER_SHEET,
                "omissions": [{"reason": r, "observations": n, "locator_samples": self.omission_samples[r],
                               "samples_complete": n <= 100} for r, n in self.omission_counts.items()],
                "limitations": ["layout_and_page_count_not_verified", "images_charts_not_extracted",
                                "inventory_counts_are_not_proof_of_full_text_extraction"]}

    def emit(self, kind: str, locator, text: str, meta: dict | None = None) -> None:
        if not text.strip():
            return
        body = clip(text, self.budget)
        if len(body) < len(text):
            self.omit("character_limit", locator(len(self.out) + 1))
        if body:
            ordinal = len(self.out) + 1
            self.out.append(Section(kind, ordinal, locator(ordinal), body, meta or {}))


def _local(el) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _scan(el):
    """el 아래의 문단과 표를 문서 순서로. 표 안으로는 들어가지 않는다.

    표는 문단 안에 중첩되고(p > run > tbl) 표의 셀은 다시 문단을 가진다.
    표를 만나면 멈추고 호출자에게 넘겨야 셀 문단이 본문 문단으로 새지 않는다.
    """
    stack = list(reversed(el))
    while stack:
        node = stack.pop()
        name = _local(node)
        if name == "tbl":
            yield name, node
            continue
        if name == "p":
            yield name, node
        stack.extend(reversed(node))


def _own_text(paragraph) -> str:
    """이 문단이 직접 가진 글자만. 중첩된 문단·표의 글자는 그쪽에서 따로 나온다."""
    parts: list[str] = []
    stack = list(reversed(paragraph))
    while stack:
        node = stack.pop()
        name = _local(node)
        if name in ("p", "tbl"):
            continue
        if name == "t":
            # <hp:t>앞<hp:tab/>뒤</hp:t> — 탭·줄바꿈 뒤의 글자는 tail에 있다.
            parts.append(node.text or "")
            for child in node:
                parts.append(_INLINE.get(_local(child), ""))
                parts.append(child.tail or "")
            continue
        stack.extend(reversed(node))
    return "".join(parts)


def _body(root, writer: _Writer) -> None:
    for kind, node in _scan(root):
        if kind == "p":
            text = _own_text(node)
            # 표를 담은 문단은 글자가 없는 경우가 많다 — 그 앞의 안내 문단을 지우지 않는다.
            if text.strip():
                writer.preceding = text
            writer.emit("paragraph", lambda n: f"{writer.name} · {n}문단", text)
        else:
            _table(node, writer)
            writer.preceding = ""


def _int(node, attribute: str, default: int) -> int:
    try:
        return max(0, int(node.get(attribute, default))) if node is not None else default
    except (TypeError, ValueError):
        return default


def _child(node, name: str):
    return next((child for child in node if _local(child) == name), None)


def _table(table, writer: _Writer) -> None:
    if writer.budget[0] <= 0:
        writer.omit("character_limit", f"{writer.name} · 미방문 표")
        return
    writer.tables_visited += 1
    writer.table_number += 1
    number = writer.table_number
    unit = _unit(writer.preceding)
    writer.preceding = ""   # 셀 안의 표가 바깥 표의 단위를 물려받지 않게 한다

    grid: dict[tuple[int, int], str] = {}
    spans: dict[int, list[str]] = {}       # 행 → 그 행에서 시작한 병합 설명
    header_rows: set[int] = set()
    nested: dict[int, list] = {}           # 행 → 그 행의 셀에 든 표
    cells_seen = 0

    row_index = -1
    for child in table:
        name = _local(child)
        if name == "caption":
            caption = " ".join(_own_text(p) for kind, p in _scan(child) if kind == "p")
            unit = unit or _unit(caption)
            writer.emit("paragraph", lambda n: f"{writer.name} · 표 {number} · 캡션", caption)
            continue
        if name != "tr":
            continue
        row_index += 1
        column = 0
        for cell in child:
            if _local(cell) != "tc":
                continue
            if cells_seen >= MAX_CELLS_PER_SHEET:
                writer.omit("table_cell_limit", f"{writer.name} · 표 {number} · {row_index + 1}행")
                break
            writer.cells_visited += 1
            address = _child(cell, "cellAddr")
            span = _child(cell, "cellSpan")
            row = _int(address, "rowAddr", row_index)
            # 위치 정보가 없는 파일도 있다 — 그때는 이 행의 다음 빈 칸에 놓는다.
            while address is None and (row, column) in grid:
                column += 1
            col = _int(address, "colAddr", column)
            width = max(1, _int(span, "colSpan", 1))
            height = max(1, _int(span, "rowSpan", 1))

            if width > 1 or height > 1:
                writer.merges_visited += 1
            lines: list[str] = []
            for kind, node in _scan(cell):
                if kind == "p":
                    lines.append(_own_text(node))
                else:
                    nested.setdefault(row, []).append(node)
            text = "\n".join(line for line in lines if line.strip())

            room = MAX_CELLS_PER_SHEET - cells_seen
            if width * height > room:
                # 손상되거나 악의적인 병합 크기로 격자가 폭주하지 않게 한다.
                # 값은 제자리 한 칸에만 두고, 일부만 읽었다고 알린다.
                writer.omit("merge_expansion_limit", f"{writer.name} · 표 {number} · {row + 1}행 {col + 1}열")
                width = height = 1
            for down in range(height):
                for across in range(width):
                    grid.setdefault((row + down, col + across), text)
            cells_seen += width * height
            if width > 1 or height > 1:
                spans.setdefault(row, []).append(f"{col + 1}열 병합 {width}x{height}")
            if cell.get("header") == "1":
                header_rows.add(row)
            column = col + width


    if grid:
        _rows(grid, spans, header_rows, unit, number, nested, writer)
    else:
        for tables in nested.values():
            for inner in tables:
                _table(inner, writer)


def _rows(grid, spans, header_rows, unit, number, nested, writer: _Writer) -> None:
    rows = max(row for row, _col in grid) + 1
    column_ids = sorted({col for _row, col in grid})
    columns = len(column_ids)

    # 제목 셀로 지정된 행은 맨 위에서 이어진 것만 머리글로 본다. 지정이 없으면
    # 첫 행을 문맥으로 붙이되 '머리글'이라고 부르지 않는다.
    declared = 0
    while declared in header_rows:
        declared += 1
    head_count = declared or 1
    label = "머리글" if declared else "첫 행"
    heads = []
    for col in column_ids:
        seen: list[str] = []
        for row in range(min(head_count, rows)):
            value = " ".join(grid.get((row, col), "").split())
            if value and value not in seen:
                seen.append(value)
        heads.append(" / ".join(seen))

    row_ids = sorted({row for row, _col in grid} | set(nested))
    for row in row_ids:
        values = [grid.get((row, col), "") for col in column_ids]
        if any(value.strip() for value in values):
            if columns == 1:
                text = values[0]
            else:
                text = " | ".join(f"{col + 1}열: {value}" for col, value in zip(column_ids, values))
                if row >= head_count and any(heads):
                    text = f"{label}: " + " | ".join(heads) + "\n" + text
                    if unit:
                        text += f"\n단위: {unit}"
                if row in spans:
                    text += "\n" + ", ".join(spans[row])
            meta = {"table": number, "rows": [row + 1, row + 1]}
            if columns > 1 and any(heads):
                meta["header"] = " | ".join(heads)
            if unit:
                meta["unit"] = unit
            if row in spans:
                meta["spans"] = spans[row]
            writer.emit("table", lambda _n, r=row: f"{writer.name} · 표 {number} · {r + 1}행", text, meta)
        # 셀 안의 표는 그 행 바로 뒤에 별도의 표로 낸다.
        for inner in nested.get(row, []):
            _table(inner, writer)
        if writer.budget[0] <= 0:
            if row != row_ids[-1]:
                writer.omit("unvisited_table_rows", f"{writer.name} · 표 {number} · {row + 2}행 이후")
            return


def _unit(text: str) -> str:
    text = " ".join((text or "").split())
    if len(text) > _UNIT_LINE_MAX:
        return ""
    match = _UNIT.search(text)
    return match.group(1).strip() if match else ""


def _read_meta(zf: zipfile.ZipFile, names: list[str]) -> DocMeta:
    """version.xml / Contents/content.hpf 등에서 얻을 수 있는 것만 취한다."""
    meta = DocMeta()
    for cand in ("Contents/content.hpf", "meta.xml", "settings.xml"):
        if cand not in names:
            continue
        try:
            root = etree.fromstring(zf.read(cand), etree.XMLParser(resolve_entities=False, no_network=True))
        except etree.XMLSyntaxError:
            continue
        for el in root.iter():
            name = (el.get("name", "") if _local(el).lower() == "meta" else _local(el)).lower()
            value = (el.text or "").strip()
            if not value:
                continue
            if name == "title" and not meta.title:
                meta.title = value
            elif name in ("creator", "author") and not meta.author:
                meta.author = value
            elif name in ("createddate", "created"):
                meta.created = value
            elif name == "date" and not meta.created:
                meta.created = value
            elif name in ("modifieddate", "modified") and not meta.modified:
                meta.modified = value
    return meta
