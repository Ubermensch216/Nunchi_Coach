"""서식 파일(.hwpx·.odt)에서 본문 서식을 읽어 스튜디오 서식으로 옮긴다.

    형식 확인 → 형식별 해석(문단마다 글자·서식) → 문서 틀 제외
    → 기호로 단계 판별 → 단계마다 가장 많이 쓰인 값 → 스튜디오 서식(format)

onNara.sAIde 공문기안기의 서식 분석기(src/lib/template-format)를 옮긴 것이다.
거기서 실물 문서로 확인한 규칙을 그대로 따른다.

  · AI를 쓰지 않는다. 파일에 적힌 값을 읽는 규칙 처리라 결과가 늘 같다.
  · 결재란·등록번호 같은 문서 틀(온나라 CellField, 보호 칸)은 분석하지 않는다.
    온나라 '본문' 누름틀이 있으면 그 안만 본다.
  · 한 단계 안에서도 줄간격·칸 수가 문단마다 조금씩 다르다. 평균이 아니라 가장
    많이 쓰인 값을 고르고, 흩어진 값은 알림(notes)으로 남겨 사람이 고치게 한다.
  · 원본 파일과 본문 글은 남기지 않는다. 서식 값·파일 이름·지문(SHA-256)만 남긴다.
    예시 한 줄(sample)은 확인 화면에서만 쓰고 저장하는 서식에는 넣지 않는다.

값의 단위는 한글 기준이다 — 글자 크기·여백은 pt, 자간·장평·줄간격은 %.
"""

from __future__ import annotations

import copy
import hashlib
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from lxml import etree

from . import markers
from .templates import DEFAULT_FORMAT

ACCEPT = (".hwpx", ".odt", ".ott")
FILE_FILTER = "서식 파일 (*.hwpx *.odt)"
MAX_BYTES = 30 * 1024 * 1024        # 그림이 많이 든 보고서도 받는다. 본문 XML만 풀어 읽는다
MAX_XML_BYTES = 40 * 1024 * 1024    # 풀어 놓은 XML 하나의 한도(압축 폭탄 방지)
SAMPLE_CHARS = 30
MAX_HEADINGS = 10
MAX_LEVELS = 3                      # 본문 목록은 세 단계까지 쓴다(studio/model.py)
# 한컴 변환기가 내보낸 ODT는 한글 160%를 123%, 170%를 131%, 180%를 138%로 적는다
# (실물에서 관찰). 기준 높이가 달라 약 1.3배 차이가 난다 — 확정값이 아닌 추정이다.
ODT_LINE_FACTOR = 1.3

APPROVAL, TITLE_BOX, SUMMARY_BOX, CHAPTER_BOX, DATA = "approval", "title", "summary", "chapter", "data"
_APPROVAL_WORDS = re.compile(
    r"^(?:등록번호|등록일자|결재일자|공개구분|기안자|검토자|결재권자|협조자|시행|접수|수신자?|경유|참조|"
    r"주무관|팀장|과장|국장)$")
_ROMAN = re.compile(r"^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ][.．]?$|^[IVX]{1,4}[.．]?$")
_TITLE_LABEL = re.compile(r"^\s*제\s*목\s*[:：]?\s*$")
_OFFICIAL_KEYS = {"num", "ga", "num-paren", "ga-paren", "paren-num", "paren-ga"}
_LENGTH = re.compile(r"^(-?\d+(?:\.\d+)?)\s*(cm|mm|in|pt|pc|px)?$")
_PERCENT = re.compile(r"^(-?\d+(?:\.\d+)?)%$")


class FormatError(ValueError):
    """사용자에게 그대로 보여 줄 수 있는 문장을 담는다."""


@dataclass(slots=True, frozen=True)
class Char:
    font: str = ""
    size: float = 10.0
    bold: bool = False
    spacing: int = 0        # 자간(%). 0이 기본, 음수는 좁게
    ratio: int = 100        # 장평(%)


@dataclass(slots=True, frozen=True)
class Para:
    line: int = 160         # 줄간격(%, 한글 기준)
    before: float = 0.0     # 문단 위·아래 간격(pt)
    after: float = 0.0
    left: float = 0.0       # 왼쪽 여백(pt)
    indent: float = 0.0     # 첫 줄 들여쓰기(+)·내어쓰기(-)(pt)
    align: str = "left"


@dataclass(slots=True)
class RawPara:
    text: str               # 앞 공백을 포함한 문단 글
    char: Char
    para: Para
    table: tuple[int, int] | None = None    # (표 번호, 칸 번호)
    in_field: bool = False


@dataclass(slots=True)
class RawDoc:
    paras: list[RawPara] = field(default_factory=list)
    tables: dict[int, str] = field(default_factory=dict)    # 표 번호 → 쓰임
    page: dict | None = None
    body_field_used: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Level:
    key: str
    label: str
    glyph: str              # 원본에 가장 많이 쓰인 기호 그대로
    glyphs: list[str]
    sample: str
    lead: int               # 줄 앞 공백 칸 수
    char: Char
    para: Para
    count: int              # 근거가 된 문단 수


@dataclass(slots=True)
class Box:
    role: str               # title | summary | chapter
    sample: str
    char: Char
    para: Para
    lead_char: Char | None = None


@dataclass(slots=True)
class Analysis:
    file_name: str
    kind: str               # hwpx | odt
    sha256: str
    doc_kind: str           # official(1. → 가.) | report(□ → ○ → -)
    page: dict | None
    levels: list[Level]
    boxes: list[Box]
    headings: list[str]     # 대제목·소제목 글 — 서식의 항목 초안
    notes: list[str]


# ── 공통 도구 ───────────────────────────────────────────────────────
def _local(element) -> str:
    return etree.QName(element).localname if isinstance(element.tag, str) else ""


def _children(element, name: str | None = None) -> list:
    return [child for child in element if isinstance(child.tag, str) and (name is None or _local(child) == name)]


def _first(element, name: str):
    return next(iter(_children(element, name)), None) if element is not None else None


def _attr(element, name: str) -> str | None:
    """접두사와 무관하게 속성 값을 읽는다."""
    if element is None:
        return None
    for key, value in element.attrib.items():
        if etree.QName(key).localname == name:
            return value
    return None


def _number(element, name: str, fallback: float = 0.0) -> float:
    try:
        return float(_attr(element, name))
    except (TypeError, ValueError):
        return fallback


def _descendants(element, name: str) -> list:
    return [node for node in element.iter() if isinstance(node.tag, str) and _local(node) == name]


def _read_xml(package: zipfile.ZipFile, name: str):
    info = package.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise FormatError("서식 파일의 본문이 너무 커서 읽을 수 없습니다.")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    try:
        return etree.fromstring(package.read(name), parser)
    except etree.XMLSyntaxError as exc:
        raise FormatError(f"서식 파일의 구조가 깨졌습니다({name}).") from exc


def _round1(value: float) -> float:
    return round(value * 10) / 10


def _mm(pt: float) -> float:
    return _round1(pt * 25.4 / 72)


def dominant_char(segments: list[tuple[str, Char]]) -> Char:
    """문단의 대표 글자 서식 — 글자 수가 가장 많은 서식.

    이름이 아니라 값으로 센다. 한 문단이 같은 서식의 묶음 여럿으로 쪼개져 있으면
    이름으로 셀 때 '행 사 명' 같은 짧은 강조 묶음이 대표로 뽑힌다(실물 확인).
    """
    weight: dict[Char, int] = {}
    for text, char in segments:
        weight[char] = weight.get(char, 0) + len(re.sub(r"\s", "", text))
    return max(weight, key=weight.get) if weight else Char()


def is_frame_field(name: str | None) -> bool:
    """온나라 문서 틀 필드인가. 칸 필드는 `CellField번호¡이름¡…` 꼴이다.

    업무보고의 제목 표에도 `CellField¡제목` 필드가 있다(실물 확인) — 제목 필드만
    있는 표는 문서 틀이 아니라 제목 상자다.
    """
    if not name or not name.startswith("CellField"):
        return False
    parts = name.split("¡")
    return re.sub(r"\s+", "", parts[1] if len(parts) > 1 else "") != "제목"


def classify_table(cells: list[str], rows: int, framed: bool, nested: bool = False) -> str:
    """표 모양과 글로 표의 쓰임을 정한다."""
    if framed:
        return APPROVAL
    if nested:              # 표 안의 표는 자료 표의 일부다
        return DATA
    filled = [cell.strip() for cell in cells if cell.strip()]
    if sum(1 for cell in filled if _APPROVAL_WORDS.match(re.sub(r"\s+", "", cell))) >= 2:
        return APPROVAL
    if filled and re.match(r"^제\s*목\s*[:：]?$", filled[0]):
        return TITLE_BOX
    if rows == 1 and len(filled) == 2 and _ROMAN.match(filled[0]):
        return CHAPTER_BOX
    if rows == 1 and len(filled) <= 1:
        return SUMMARY_BOX
    return DATA


# ── HWPX ────────────────────────────────────────────────────────────
_HWPX_ALIGN = {"CENTER": "center", "RIGHT": "right", "JUSTIFY": "justify",
               "DISTRIBUTE": "justify", "DISTRIBUTE_SPACE": "justify"}


class _HwpxHeader:
    """Contents/header.xml의 서식 정의 목록. 글자 모양의 height 1000 = 10pt, 여백은 HWPUNIT(100 = 1pt)."""

    def __init__(self, root):
        self.fonts: dict[str, str] = {}
        self.chars: dict[str, Char] = {}
        self.paras: dict[str, tuple] = {}
        self.styles: dict[str, tuple[str, str]] = {}
        for face in _descendants(root, "fontface"):
            if _attr(face, "lang") == "HANGUL":
                for font in _children(face, "font"):
                    self.fonts[_attr(font, "id") or ""] = _attr(font, "face") or ""
        for item in _descendants(root, "charPr"):
            ref = _first(item, "fontRef")
            self.chars[_attr(item, "id") or ""] = Char(
                font=self.fonts.get(_attr(ref if ref is not None else item, "hangul") or "", ""),
                size=_round1(_number(item, "height", 1000) / 100),
                bold=_first(item, "bold") is not None,
                spacing=round(_number(_first(item, "spacing"), "hangul")),
                ratio=round(_number(_first(item, "ratio"), "hangul", 100)))
        for item in _descendants(root, "paraPr"):
            # 여백·줄간격이 hp:switch 안에 있는 경우가 있다(실물 확인). case → default → 바로 아래 순으로 찾는다.
            switch = _first(item, "switch")
            scopes = [scope for scope in (_first(switch, "case"), _first(switch, "default"), item)
                      if scope is not None]

            def pick(name: str, scopes=scopes):
                return next((found for scope in scopes if (found := _first(scope, name)) is not None), None)

            align = _first(item, "align")
            self.paras[_attr(item, "id") or ""] = (
                pick("margin"), pick("lineSpacing"),
                _HWPX_ALIGN.get(_attr(align if align is not None else item, "horizontal") or "", "left"))
        for item in _descendants(root, "style"):
            self.styles[_attr(item, "id") or ""] = (_attr(item, "paraPrIDRef") or "0", _attr(item, "charPrIDRef") or "0")

    def char(self, key: str | None, style: str | None) -> Char:
        return (self.chars.get(key or "") or self.chars.get(self.styles.get(style or "0", ("0", "0"))[1])
                or Char())

    def para(self, key: str | None, style: str | None, size: float) -> Para:
        found = self.paras.get(key or "") or self.paras.get(self.styles.get(style or "0", ("0", "0"))[0])
        margin, spacing, align = found or (None, None, "left")

        def side(name: str) -> float:
            item = _first(margin, name)
            if item is None:
                return 0.0
            value = _number(item, "value")
            # 글자 단위(CHAR)로 적힌 값은 글자 크기를 곱해 pt로 바꾼다.
            return value * size if _attr(item, "unit") == "CHAR" else value / 100

        kind = _attr(spacing, "type") if spacing is not None else "PERCENT"
        value = _number(spacing, "value", 160)
        percent = value if kind == "PERCENT" else (value / 100 / size * 100 if size else 160)
        # 같은 설정인데도 199·200·201%처럼 1%씩 흔들려 적힌다(실물 확인). 5% 단위로 맞춘다.
        return Para(line=round(percent / 5) * 5, before=_round1(side("prev")), after=_round1(side("next")),
                    left=_round1(side("left")), indent=_round1(side("intent")), align=align)


def _hwpx_page(properties) -> dict | None:
    page = _first(properties, "pagePr")
    if page is None:
        return None
    margin = _first(page, "margin")
    width, height = _mm(_number(page, "width") / 100), _mm(_number(page, "height") / 100)
    # landscape="NARROWLY"는 가로 방향이다(WIDELY가 세로). 폭·높이를 바꿔 적는다.
    if _attr(page, "landscape") == "NARROWLY" and width < height:
        width, height = height, width
    if not width or not height:
        return None
    return {"width_mm": width, "height_mm": height,
            "margin_mm": {side: _mm(_number(margin, side) / 100) for side in ("top", "bottom", "left", "right")}}


def read_hwpx(package: zipfile.ZipFile) -> RawDoc:
    names = package.namelist()
    if "Contents/header.xml" not in names:
        raise FormatError("HWPX 서식 정의(Contents/header.xml)를 찾지 못했습니다. 한글에서 HWPX로 다시 저장해 주세요.")
    sections = sorted((name for name in names if re.fullmatch(r"Contents/section\d+\.xml", name, re.I)),
                      key=lambda name: int(re.search(r"\d+", name).group()))
    if not sections:
        raise FormatError("HWPX 본문(Contents/section*.xml)을 찾지 못했습니다.")
    header = _HwpxHeader(_read_xml(package, "Contents/header.xml"))
    doc = RawDoc()
    state = {"field": "before", "field_id": None}

    def paragraph(node, table: tuple[int, int] | None = None) -> None:
        segments: list[tuple[str, str | None]] = []
        nested = []
        starts = ends = False
        style = _attr(node, "styleIDRef")
        for run in _children(node, "run"):
            char_id = _attr(run, "charPrIDRef")
            for child in _children(run):
                name = _local(child)
                if name == "t":
                    segments.append((child.text or "", char_id))
                    for inner in _children(child):
                        mark = {"tab": "\t", "fwSpace": "　"}.get(_local(inner), " ")
                        segments.append((mark + (inner.tail or ""), char_id))
                elif name == "tbl":
                    nested.append(child)
                elif name == "ctrl":
                    for control in _children(child):
                        if _local(control) == "fieldBegin" and "본문" in (_attr(control, "name") or ""):
                            starts = True
                            state["field_id"] = _attr(control, "id") or _attr(control, "fieldid")
                        elif _local(control) == "fieldEnd" and state["field_id"] is not None:
                            ref = _attr(control, "beginIDRef") or _attr(control, "fieldid")
                            if not ref or ref == state["field_id"]:
                                ends = True
                elif name == "secPr" and doc.page is None:
                    doc.page = _hwpx_page(child)
        if starts:
            state["field"] = "in"
        text = "".join(part for part, _key in segments)
        if text.strip():
            char = dominant_char([(part, header.char(key, style)) for part, key in segments])
            doc.paras.append(RawPara(text, char, header.para(_attr(node, "paraPrIDRef"), style, char.size),
                                     table, state["field"] == "in"))
        if ends:
            state["field"] = "after"
        for item in nested:
            table_of(item, table is not None)

    def table_of(node, nested: bool) -> None:
        key = len(doc.tables) + 1
        doc.tables[key] = DATA
        rows = _children(node, "tr")
        cells = [cell for row in rows for cell in _children(row, "tc")]
        cell_paras = [_children(_first(cell, "subList"), "p") if _first(cell, "subList") is not None else []
                      for cell in cells]
        texts = [" ".join("".join(t.text or "" for t in _descendants(p, "t")) for p in paras)
                 for paras in cell_paras]
        framed = any(is_frame_field(_attr(mark, "name")) for mark in _descendants(node, "fieldBegin"))
        doc.tables[key] = classify_table(texts, len(rows), framed, nested)
        for index, paras in enumerate(cell_paras, start=1):
            for item in paras:
                paragraph(item, (key, index))

    for name in sections:
        for node in _children(_read_xml(package, name), "p"):
            paragraph(node)
    doc.body_field_used = state["field"] != "before"
    if doc.body_field_used:
        doc.paras = [p for p in doc.paras if p.in_field]
    return doc


# ── ODT ─────────────────────────────────────────────────────────────
def _length_pt(value: str | None) -> float | None:
    """ODT 길이('3.257cm', '12pt', '0.5in')를 pt로."""
    match = _LENGTH.match(value.strip()) if value else None
    if not match:
        return None
    number = float(match.group(1))
    return number * {"cm": 72 / 2.54, "mm": 72 / 25.4, "in": 72, "pc": 12, "px": 0.75}.get(match.group(2), 1)


def _percent(value: str | None) -> float | None:
    match = _PERCENT.match(value.strip()) if value else None
    return float(match.group(1)) if match else None


def _prefixed(key: str) -> str:
    """fo:font-weight와 style:font-weight-asian처럼 접두사로 갈리는 속성이 있어 접두사를 붙여 둔다."""
    name = etree.QName(key)
    space = name.namespace or ""
    prefix = "fo" if "xsl-fo" in space else "style" if ":style:" in space else ""
    return f"{prefix}:{name.localname}"


class _OdtStyles:
    """서식은 스타일 이름으로 가리키고, 부모 스타일을 따라 물려받는다."""

    def __init__(self):
        self.styles: dict[str, tuple[str | None, dict, dict]] = {}
        self.defaults: dict[str, tuple[dict, dict]] = {}
        self.fonts: dict[str, str] = {}

    @staticmethod
    def _props(element) -> tuple[dict, dict]:
        text, para = {}, {}
        for child in _children(element):
            target = {"text-properties": text, "paragraph-properties": para}.get(_local(child))
            if target is not None:
                target.update({_prefixed(key): value for key, value in child.attrib.items()})
        return text, para

    def add(self, root) -> None:
        for node in root.iter():
            if not isinstance(node.tag, str):
                continue
            name = _local(node)
            if name == "font-face":
                key = _attr(node, "name")
                if key:
                    self.fonts[key] = (_attr(node, "font-family") or "").strip("'\"") or key
            elif name == "style" and ":style:" in (etree.QName(node).namespace or ""):
                self.styles[f"{_attr(node, 'family')}:{_attr(node, 'name')}"] = (
                    _attr(node, "parent-style-name"), *self._props(node))
            elif name == "default-style":
                self.defaults[_attr(node, "family") or ""] = self._props(node)

    def resolve(self, family: str, name: str | None) -> tuple[dict, dict]:
        chain, seen = [], set()
        while name and name not in seen:
            seen.add(name)
            found = self.styles.get(f"{family}:{name}")
            if found is None:
                break
            chain.insert(0, found[1:])
            name = found[0]
        if family in self.defaults:
            chain.insert(0, self.defaults[family])
        text, para = {}, {}
        for text_props, para_props in chain:
            text.update(text_props)
            para.update(para_props)
        return text, para


def _odt_char(props: dict, fonts: dict[str, str]) -> Char:
    name = props.get("style:font-name-asian") or props.get("style:font-name")
    font = ((name and fonts.get(name)) or name or (props.get("style:font-family-asian") or "").strip("'\"")
            or (props.get("fo:font-family") or "").strip("'\""))
    size = _length_pt(props.get("style:font-size-asian") or props.get("fo:font-size")) or 10
    weight = props.get("style:font-weight-asian") or props.get("fo:font-weight") or "normal"
    spacing = _length_pt(props.get("fo:letter-spacing")) or 0
    return Char(font=font or "", size=_round1(size),
                bold=weight == "bold" or (weight.isdigit() and int(weight) >= 600),
                # 자간은 ODT의 pt 값을 글자 크기로 나눠 한글 %로 바꾼다(-1.20pt ÷ 15pt = -8%).
                spacing=round(spacing / size * 100) if size else 0,
                ratio=round(_percent(props.get("style:text-scale")) or 100))


def _odt_para(props: dict, size: float) -> Para:
    height = props.get("fo:line-height")
    percent = _percent(height)
    absolute = _length_pt(height) if percent is None else None
    if percent is not None:
        line = round(percent * ODT_LINE_FACTOR / 5) * 5
    else:
        line = round(absolute / size * 100) if absolute is not None and size else 160
    left = _length_pt(props.get("fo:margin-left")) or 0
    indent = _length_pt(props.get("fo:text-indent")) or 0
    align = props.get("fo:text-align")
    return Para(line=line, before=_round1(_length_pt(props.get("fo:margin-top")) or 0),
                after=_round1(_length_pt(props.get("fo:margin-bottom")) or 0),
                # ODT는 "왼쪽 여백 + 음수 첫 줄"로 내어쓰기를 적는다. 한글은 "왼쪽 여백 + 내어쓰기"다.
                left=_round1(left + min(indent, 0)), indent=_round1(indent),
                align={"center": "center", "justify": "justify", "end": "right", "right": "right"}.get(align, "left"))


def _odt_page(styles) -> dict | None:
    nodes = [node for node in styles.iter() if isinstance(node.tag, str)]
    masters = [node for node in nodes if _local(node) == "master-page"]
    master = next((node for node in masters if _attr(node, "name") == "Standard"), masters[0] if masters else None)
    layout_name = _attr(master, "page-layout-name")
    layout = next((node for node in nodes if _local(node) == "page-layout"
                   and (not layout_name or _attr(node, "name") == layout_name)), None)
    props = _first(layout, "page-layout-properties")
    if props is None:
        return None

    def mm(name: str) -> float:
        return _mm(_length_pt(_attr(props, name)) or 0)

    if not mm("page-width") or not mm("page-height"):
        return None
    return {"width_mm": mm("page-width"), "height_mm": mm("page-height"),
            "margin_mm": {side: mm(f"margin-{side}") for side in ("top", "bottom", "left", "right")}}


def read_odt(package: zipfile.ZipFile) -> RawDoc:
    names = package.namelist()
    if "content.xml" not in names:
        raise FormatError("ODT 본문(content.xml)을 찾지 못했습니다. 한글이나 온나라에서 ODT로 다시 저장해 주세요.")
    content = _read_xml(package, "content.xml")
    styles = _read_xml(package, "styles.xml") if "styles.xml" in names else None
    book = _OdtStyles()
    if styles is not None:
        book.add(styles)
    book.add(content)
    body = next((node for node in content.iter() if isinstance(node.tag, str) and _local(node) == "text"
                 and node.getparent() is not None and _local(node.getparent()) == "body"), None)
    if body is None:
        raise FormatError("ODT 본문(office:text)을 찾지 못했습니다.")
    doc = RawDoc(notes=["ODT 줄간격은 한글 기준으로 환산한 추정값입니다(ODT 123% ≈ 한글 160%)."])
    state = {"field": "before"}

    def paragraph(node, table: tuple[int, int] | None = None) -> None:
        segments: list[tuple[str, str | None]] = []
        nested = []
        marks = {"starts": False, "ends": False}

        def visit(element, style: str | None) -> None:
            segments.append((element.text or "", style))
            for child in element:
                name = _local(child)
                if name == "s":
                    segments.append((" " * (int(_number(child, "c", 1)) or 1), style))
                elif name == "tab":
                    segments.append(("\t", style))
                elif name == "line-break":
                    segments.append((" ", style))
                elif name in ("span", "a"):
                    visit(child, _attr(child, "style-name") or style)
                elif name in ("bookmark-start", "bookmark"):
                    marks["starts"] = marks["starts"] or "¡본문¡" in (_attr(child, "name") or "")
                elif name == "bookmark-end":
                    marks["ends"] = marks["ends"] or "¡본문¡" in (_attr(child, "name") or "")
                elif name == "frame":       # 표는 draw:frame 안에 들어 문단에 매달려 있다
                    nested.append(child)
                elif name and name not in ("note", "annotation"):
                    visit(child, style)
                segments.append((child.tail or "", style))

        visit(node, None)
        if marks["starts"]:
            state["field"] = "in"
            doc.body_field_used = True
        text = "".join(part for part, _style in segments)
        if text.strip():
            base_text, base_para = book.resolve("paragraph", _attr(node, "style-name"))
            char = dominant_char([
                (part, _odt_char({**base_text, **(book.resolve("text", style)[0] if style else {})}, book.fonts))
                for part, style in segments])
            doc.paras.append(RawPara(text, char, _odt_para(base_para, char.size), table, state["field"] == "in"))
        if marks["ends"]:
            state["field"] = "after"
        for frame in nested:
            walk(frame, table)

    def table_of(node, nested: bool) -> None:
        key = len(doc.tables) + 1
        doc.tables[key] = DATA
        cells: list = []
        found = {"rows": 0, "framed": False}

        def scan(element) -> None:
            for child in _children(element):
                name = _local(child)
                if name == "table-row":
                    found["rows"] += 1
                    scan(child)
                elif name == "table-cell":
                    cells.append(child)
                    found["framed"] = found["framed"] or _attr(child, "protected") == "true"
                elif name in ("table-rows", "table-header-rows", "table-row-group"):
                    scan(child)

        scan(node)
        if not found["framed"]:
            found["framed"] = any(_local(item).startswith("bookmark") and is_frame_field(_attr(item, "name"))
                                  for item in node.iter() if isinstance(item.tag, str))
        texts = [" ".join("".join(item.itertext()) for item in cell.iter()
                          if isinstance(item.tag, str) and _local(item) in ("p", "h")) for cell in cells]
        doc.tables[key] = classify_table(texts, found["rows"], found["framed"], nested)
        for index, cell in enumerate(cells, start=1):
            walk(cell, (key, index))

    def walk(element, table: tuple[int, int] | None = None) -> None:
        for child in _children(element):
            name = _local(child)
            if name in ("p", "h"):
                paragraph(child, table)
            elif name == "table":
                table_of(child, table is not None)
            elif name not in ("sequence-decls", "variable-decls", "tracked-changes"):
                walk(child, table)

    walk(body)
    if doc.body_field_used:
        doc.paras = [p for p in doc.paras if p.in_field]
    doc.page = _odt_page(styles) if styles is not None else None
    return doc


# ── 단계 판별 ───────────────────────────────────────────────────────
def _mode(values: list):
    """가장 많이 나온 값. 같으면 먼저 나온 값."""
    return Counter(values).most_common(1)[0][0]


def _mode_char(items: list[Char]) -> Char:
    return Char(*(_mode([getattr(item, name) for item in items])
                  for name in ("font", "size", "bold", "spacing", "ratio")))


def _mode_para(items: list[Para]) -> Para:
    return Para(*(_mode([getattr(item, name) for item in items])
                  for name in ("line", "before", "after", "left", "indent", "align")))


def _clip(text: str, limit: int = SAMPLE_CHARS) -> str:
    flat = " ".join(text.split())
    return flat[:limit] + "…" if len(flat) > limit else flat


def build(raw: RawDoc, file_name: str, kind: str, sha256: str) -> Analysis:
    """해석 결과 → 단계별 서식. 형식과 무관한 공통 단계."""
    notes = list(raw.notes)
    if raw.body_field_used:
        notes.append("온나라 ‘본문’ 누름틀 안의 내용만 분석했습니다.")
    position = {id(para): index for index, para in enumerate(raw.paras)}

    # 1. 본문 문단(표 밖)을 단계별로 묶는다.
    body = [para for para in raw.paras if para.table is None]
    classified = []     # [문단, key, label, glyph, rest]
    for para in body:
        marker = markers.classify(para.text)
        classified.append([para, marker.key, marker.label, marker.glyph, marker.rest] if marker
                          else [para, "body", "일반 문단", "", para.text.strip()])
    # 기호 없는 첫 문단이 가운데 정렬이거나 가장 큰 글자면 제목으로 본다(보고서 첫 줄 제목).
    first_marked = next((i for i, item in enumerate(classified) if item[1] != "body"), len(classified))
    largest = max((para.char.size for para in body), default=0)
    if not any(item[1] == "title" for item in classified):
        for item in classified[:first_marked]:
            if item[0].para.align == "center" or item[0].char.size >= largest:
                item[1:4] = ["title", "제목", ""]
                break
    # 요약 상자는 본문 첫 항목보다 앞에 있는 표만 인정한다(문서 끝 점검표를 요약으로 오인하지 않게).
    first_item = next((item[0] for item in classified if item[1] not in ("body", "title")), None)
    first_item_at = position[id(first_item)] if first_item is not None else float("inf")

    groups: dict[str, list] = {}
    for item in classified:
        groups.setdefault(item[1], []).append(item)
    # 기호 없는 일반 문단은 이어지는 줄이 대부분이라 두 번 이상 나올 때만 단계로 남긴다.
    if len(groups.get("body", [])) < 2:
        groups.pop("body", None)
    order = {key: classified.index(group[0]) for key, group in groups.items()}
    levels = []
    for key in sorted(groups, key=lambda name: (markers.rank_of(name), order[name])):
        group = groups[key]
        glyphs = list(dict.fromkeys(item[3] for item in group if item[3]))[:10]
        levels.append(Level(key, group[0][2], _mode([item[3] for item in group]), glyphs, _clip(group[0][0].text),
                            _mode([markers.lead_spaces(item[0].text) for item in group]),
                            _mode_char([item[0].char for item in group]),
                            _mode_para([item[0].para for item in group]), len(group)))
        spread = sorted({item[0].para.line for item in group})
        if len(group) >= 3 and len(spread) > 1:
            notes.append(f"‘{group[0][2]}’의 줄간격이 문단마다 달라({spread[0]}~{spread[-1]}%) "
                         "가장 많이 쓰인 값을 골랐습니다.")

    # 2. 상자 요소(표): 제목 상자, 요약 상자, Ⅰ 대제목 막대.
    def paras_of(table: int) -> list[RawPara]:
        return [para for para in raw.paras if para.table and para.table[0] == table]

    boxes: list[Box] = []
    summary: list[RawPara] = []
    used = None
    title_table = next((key for key, role in raw.tables.items() if role == TITLE_BOX), None)
    if title_table is not None:
        # 업무보고 제목 표: '제목:' 칸 → 제목 칸 → (아래 행) 요약 문단.
        inside = paras_of(title_table)
        label = next((para for para in inside if _TITLE_LABEL.match(para.text)), None)
        label_cell = label.table[1] if label else 0
        titles = [para for para in inside if para.table[1] == label_cell + 1]
        if titles:
            main = max(titles, key=lambda para: para.char.size)
            boxes.append(Box("title", _clip(" ".join(para.text for para in titles)), main.char, main.para,
                             label.char if label else None))
        summary += [para for para in inside if para.table[1] > label_cell + 1]
        used = title_table
    else:
        # 첫 한 칸 표가 글 한 줄짜리이고 문서에서 가장 큰 글자를 쓰면 제목 상자다.
        biggest = max((para.char.size for para in raw.paras), default=0)
        first = next((key for key, role in raw.tables.items() if role == SUMMARY_BOX), None)
        inside = paras_of(first) if first is not None else []
        if len(inside) == 1 and inside[0].char.size >= max(18, biggest):
            boxes.append(Box("title", _clip(inside[0].text), inside[0].char, inside[0].para))
            used = first
    for key, role in raw.tables.items():
        inside = paras_of(key)
        if role == SUMMARY_BOX and key != used and inside and position[id(inside[0])] < first_item_at:
            summary += inside
    summary = [para for para in summary
               if (marker := markers.classify(para.text)) is None or marker.key != "note"]
    if summary:
        boxes.append(Box("summary", _clip(summary[0].text), _mode_char([p.char for p in summary]),
                         _mode_para([p.para for p in summary])))
    chapters = []
    for key, role in raw.tables.items():
        if role != CHAPTER_BOX:
            continue
        inside = paras_of(key)
        number = next((para for para in inside if para.table[1] == 1), None)
        name = next((para for para in inside if para.table[1] > 1 and para.text.strip()), None)
        if number and name:
            chapters.append((number, name))
    if chapters:
        boxes.append(Box("chapter", _clip(f"{chapters[0][0].text.strip()} {chapters[0][1].text.strip()}"),
                         _mode_char([name.char for _number, name in chapters]),
                         _mode_para([name.para for _number, name in chapters]),
                         _mode_char([number.char for number, _name in chapters])))

    # 3. 시행문(1. → 가.)인지 보고서(□ → ○)인지: 가장 윗단계 기호로 정한다.
    top = next((level for level in levels if level.key not in ("title", "body")), None)
    doc_kind = "official" if top and top.key in _OFFICIAL_KEYS and not chapters else "report"

    # 4. 대제목 글 → 항목 초안. 시행문의 1. 2. 3.은 문장이라 항목 이름으로 쓰지 않는다.
    if doc_kind == "official":
        found = []
    elif chapters:
        found = [name.text for _number, name in chapters]
    else:
        found = [item[4] for item in groups.get("section") or groups.get("sym-pua") or []]
    headings = list(dict.fromkeys(filter(None, (_clip(text, 40) for text in found))))[:MAX_HEADINGS]

    approval = sum(1 for role in raw.tables.values() if role == APPROVAL)
    data = sum(1 for role in raw.tables.values() if role == DATA)
    if approval or data:
        notes.append(f"결재란 등 문서 틀 표 {approval}개와 자료 표 {data}개는 분석에서 뺐습니다.")
    return Analysis(file_name, kind, sha256, doc_kind, raw.page, levels, boxes, headings, notes)


def analyze(path: str | Path) -> Analysis:
    """서식 파일 하나를 분석한다. 읽을 수 없으면 사용자에게 보여 줄 문장과 함께 FormatError."""
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            magic = handle.read(4)
            handle.seek(0)
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as exc:
        raise FormatError(f"파일을 열 수 없습니다: {exc.strerror or exc}") from exc
    if not size:
        raise FormatError("빈 파일입니다.")
    if size > MAX_BYTES:
        raise FormatError(f"파일이 {MAX_BYTES // 1024 // 1024}MB를 넘어 올릴 수 없습니다.")
    if suffix == ".hwp" or magic == b"\xd0\xcf\x11\xe0":
        raise FormatError("구형 한글(.hwp) 파일은 서식을 읽을 수 없습니다. 한글에서 [다른 이름으로 저장]으로 "
                          "‘HWPX’ 형식을 골라 저장한 뒤 올려 주세요.")
    if suffix not in ACCEPT or not zipfile.is_zipfile(path):
        raise FormatError("서식 파일은 HWPX · ODT 형식만 올릴 수 있습니다.")
    kind = "hwpx" if suffix == ".hwpx" else "odt"
    try:
        with zipfile.ZipFile(path) as package:
            raw = read_hwpx(package) if kind == "hwpx" else read_odt(package)
    except (zipfile.BadZipFile, KeyError, RuntimeError) as exc:
        raise FormatError("서식 파일을 풀 수 없습니다. 암호가 걸렸거나 손상된 파일입니다.") from exc
    analysis = build(raw, path.name, kind, digest)
    if not analysis.levels and not analysis.boxes:
        raise FormatError("본문에서 서식을 찾지 못했습니다. 본문 글이 들어 있는 문서인지 확인해 주세요.")
    return analysis


# ── 스튜디오 서식으로 ───────────────────────────────────────────────
def _style(char: Char, para: Para, **extra) -> dict:
    return {"font": char.font or DEFAULT_FORMAT["body"]["font"], "size": char.size, "bold": char.bold,
            "align": para.align, "line": para.line, "spacing": char.spacing, "ratio": char.ratio,
            "before": para.before, "hanging": max(0.0, -para.indent), **extra}


def _stepped(level: Level, **extra) -> dict:
    return _style(level.char, replace(level.para, align="left"), key=level.key, label=level.label,
                  glyph=level.glyph, glyphs=level.glyphs, lead=level.lead, **extra)


def studio_format(analysis: Analysis) -> dict:
    """분석한 단계를 스튜디오 서식의 자리(제목 · 항목 제목 · 목록 1~3 · 주석 · 본문)에 앉힌다.

    산출물은 '항목 제목 아래 개조식 문장' 꼴이다(studio/model.py). 그래서 서식의
    가장 윗단계가 항목 제목이 되고, 그 아래 단계들이 차례로 목록 단계가 된다.
    Ⅰ 대제목 막대(표)가 있으면 그것이 항목 제목이다.
    """
    fmt = copy.deepcopy(DEFAULT_FORMAT)
    if analysis.page:
        fmt["page"] = copy.deepcopy(analysis.page)
    by_key = {level.key: level for level in analysis.levels}
    boxes = {box.role: box for box in analysis.boxes}
    hierarchy = [level for level in analysis.levels if level.key not in markers.NON_HIERARCHY]

    if "title" in boxes:
        fmt["title"] = _style(boxes["title"].char, replace(boxes["title"].para, align="center"), box=True)
    elif "title" in by_key:
        fmt["title"] = _style(by_key["title"].char, by_key["title"].para)

    if "chapter" in boxes and "chapter" not in by_key:
        box = boxes["chapter"]
        fmt["heading"] = _style(box.char, replace(box.para, align="left"), key="chapter", label="Ⅰ. 대제목",
                                glyph="Ⅰ", glyphs=["Ⅰ"], lead=0, box=True)
    elif hierarchy:
        fmt["heading"] = _stepped(hierarchy.pop(0))
    if hierarchy:
        fmt["levels"] = [_stepped(level) for level in hierarchy[:MAX_LEVELS]]
        if len(hierarchy) > MAX_LEVELS:
            fmt.setdefault("notes", []).append(
                f"본문 목록은 {MAX_LEVELS}단계까지 씁니다. 그 아래 {len(hierarchy) - MAX_LEVELS}단계는 뺐습니다.")
    first = fmt["levels"][0]
    if "body" in by_key:
        fmt["body"] = _style(by_key["body"].char, by_key["body"].para)
    elif hierarchy:
        # 일반 문단이 따로 없으면 첫 목록 단계의 글자를 본문 글자로 쓴다.
        fmt["body"] = {**{k: first[k] for k in ("font", "size", "bold", "line", "spacing", "ratio")},
                       "align": "justify"}
    if "note" in by_key:
        fmt["note"] = _stepped(by_key["note"])
    else:
        fmt["note"] = {**fmt["note"], "font": fmt["body"]["font"]}
    fmt["doc_kind"] = analysis.doc_kind
    fmt["source"] = {"file": analysis.file_name, "kind": analysis.kind, "sha256": analysis.sha256,
                     "imported_at": datetime.now().isoformat(timespec="seconds")}
    fmt["notes"] = analysis.notes + fmt.get("notes", [])
    return fmt


def summary(fmt: dict) -> str:
    """서식 목록에 붙이는 한 줄 — 어떤 글꼴·기호 체계인가."""
    glyphs = [str(part.get("glyph")) for part in (fmt.get("heading", {}), *fmt.get("levels", []))
              if part.get("glyph")]
    main = (fmt.get("levels") or [fmt.get("body", {})])[0]
    shown = ["□" if markers.is_private(g) else g for g in glyphs]
    text = f"{main.get('font', '')} {main.get('size', 12):g}pt · 줄간격 {main.get('line', 160)}%"
    return text + (f" · 단계 기호 {' → '.join(shown)}" if shown else "")
