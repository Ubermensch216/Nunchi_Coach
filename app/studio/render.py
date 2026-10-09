"""산출물 본문을 파일로 그린다 — MD · DOCX · HWPX · PDF · XLSX.

같은 본문(studio/model.py의 덩이)과 같은 서식 모양(studio/templates.py의
format)에서 모든 형식이 나온다. 그리는 함수는 (본문, 모양, 제목)을 받아
바이트를 돌려준다 — 어디에 쓸지는 모른다(쓰기는 studio/outputs.py가 한다).

HWPX는 한글 문서의 최소 꾸러미를 직접 짓는다. 글자·문단 모양과 표까지만
담고, 결재란·누름틀·쪽 번호 같은 문서 틀은 만들지 않는다 — 내용을 한글에서
이어 다듬기 위한 출발점이다.
"""

from __future__ import annotations

import io
import os
import zipfile
from html import escape
from pathlib import Path

from . import model
from .model import BULLET, HEADING, NOTE, PARAGRAPH, TABLE, TITLE

RENDERER_VERSION = "1"
_FONT_DIR = Path(__file__).resolve().parents[1] / "ui" / "assets" / "fonts"
# 한글의 줄간격 160%는 워드의 '배수 1.15'쯤과 비슷한 높이다(글꼴 높이 기준이 다르다).
_WORD_LINE_PER_HWP = 1.15 / 160


def _style(fmt: dict, key: str) -> dict:
    return {**fmt.get("body", {}), **fmt.get(key, {})}


def _level(fmt: dict, level: int) -> dict:
    levels = fmt.get("levels") or [{"glyph": "-", "lead": 1}]
    return {**fmt.get("body", {}), **levels[min(level, len(levels) - 1)]}


def _bullet_text(fmt: dict, block) -> str:
    style = _level(fmt, block.level)
    return " " * int(style.get("lead", 0)) + f"{style.get('glyph', '-')} {block.text}"


# ── Markdown ────────────────────────────────────────────────────────
def to_markdown(markdown: str, fmt: dict, title: str) -> bytes:
    return ("﻿" + markdown.strip() + "\n").encode("utf-8")


# ── DOCX ────────────────────────────────────────────────────────────
def to_docx(markdown: str, fmt: dict, title: str) -> bytes:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt

    align = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
             "right": WD_ALIGN_PARAGRAPH.RIGHT, "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}
    document = docx.Document()
    document.core_properties.title = title
    margin = fmt.get("page", {}).get("margin_mm", {})
    for section in document.sections:
        section.top_margin, section.bottom_margin = Mm(margin.get("top", 20)), Mm(margin.get("bottom", 15))
        section.left_margin, section.right_margin = Mm(margin.get("left", 20)), Mm(margin.get("right", 20))

    def write(paragraph, text: str, style: dict) -> None:
        run = paragraph.add_run(text)
        run.font.name = style.get("font", "맑은 고딕")
        # 한글 글꼴은 동아시아 글꼴 자리에 따로 적어야 워드가 그 글꼴로 그린다.
        run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), style.get("font", "맑은 고딕"))
        run.font.size = Pt(style.get("size", 12))
        run.font.bold = bool(style.get("bold"))
        paragraph.alignment = align.get(style.get("align", "left"), WD_ALIGN_PARAGRAPH.LEFT)
        paragraph.paragraph_format.line_spacing = round(style.get("line", 160) * _WORD_LINE_PER_HWP, 2)
        paragraph.paragraph_format.space_after = Pt(2)

    for block in model.parse(markdown):
        if block.kind == TABLE:
            table = document.add_table(rows=len(block.rows), cols=len(block.rows[0]))
            table.style = "Table Grid"
            for r, row in enumerate(block.rows):
                for c, cell in enumerate(row):
                    paragraph = table.cell(r, c).paragraphs[0]
                    write(paragraph, cell, {**_style(fmt, "body"), "align": "left", "bold": r == 0})
            document.add_paragraph()
            continue
        paragraph = document.add_paragraph()
        if block.kind == TITLE:
            write(paragraph, block.text, _style(fmt, "title"))
        elif block.kind == HEADING:
            paragraph.paragraph_format.space_before = Pt(10)
            write(paragraph, block.text, _style(fmt, "heading"))
        elif block.kind == BULLET:
            write(paragraph, _bullet_text(fmt, block), {**_level(fmt, block.level), "align": "left"})
        elif block.kind == NOTE:
            write(paragraph, "※ " + block.text, _style(fmt, "note"))
        else:
            write(paragraph, block.text, _style(fmt, "body"))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# ── XLSX ────────────────────────────────────────────────────────────
def to_xlsx(markdown: str, fmt: dict, title: str) -> bytes:
    """표는 시트 하나씩, 나머지 글은 '본문' 시트에 한 줄씩."""
    import openpyxl
    from openpyxl.styles import Font

    workbook = openpyxl.Workbook()
    body = workbook.active
    body.title = "본문"
    body.column_dimensions["A"].width = 100
    tables = 0
    heading = ""
    for block in model.parse(markdown):
        if block.kind == TABLE:
            tables += 1
            name = (heading or f"표{tables}")[:25].translate(str.maketrans("", "", "[]:*?/\\")) or f"표{tables}"
            sheet = workbook.create_sheet(f"{tables}. {name}"[:31])
            for row in block.rows:
                sheet.append(row)
            for cell in sheet[1]:
                cell.font = Font(bold=True)
            body.append([f"(표 {tables} — ‘{sheet.title}’ 시트)"])
            continue
        if block.kind == HEADING:
            heading = block.text
        text = _bullet_text(fmt, block) if block.kind == BULLET else block.text
        body.append([text])
        if block.kind in (TITLE, HEADING):
            body.cell(row=body.max_row, column=1).font = Font(bold=True, size=14 if block.kind == TITLE else 12)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ── PDF ─────────────────────────────────────────────────────────────
def _pdf_fonts() -> tuple[Path, str, str]:
    """PDF에 넣을 글꼴 파일. 이 PC의 맑은 고딕을 먼저 쓰고, 없으면 프로그램에 든 Pretendard.

    Pretendard로 만든 PDF는 화면에는 바르게 보이지만 글자를 긁으면 쌍점(:)이
    다른 글자로, 빈칸이 줄바꿈 없는 공백으로 나온다(실측) — 복사와 검색이
    어긋난다. 맑은 고딕은 그렇지 않다. 글꼴은 쓴 글자만 추려 넣는다.
    """
    system = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if (system / "malgun.ttf").is_file() and (system / "malgunbd.ttf").is_file():
        return system, "malgun.ttf", "malgunbd.ttf"
    return _FONT_DIR, "Pretendard-Regular.ttf", "Pretendard-Bold.ttf"


def to_pdf(markdown: str, fmt: dict, title: str) -> bytes:
    """크기·굵기·줄간격·여백은 서식을 따른다. 글꼴은 _pdf_fonts가 고른 한 벌로 통일한다."""
    import pymupdf

    font_dir, regular, bold = _pdf_fonts()

    def css(name: str, style: dict) -> str:
        return (f".{name} {{ font-family: doc; font-size: {style.get('size', 12)}pt; "
                f"font-weight: {'bold' if style.get('bold') else 'normal'}; "
                f"text-align: {style.get('align', 'left')}; line-height: {style.get('line', 160) / 100 * 0.85:.2f}; "
                f"margin: 0 0 4pt 0; }}")

    rules = [
        f"@font-face {{ font-family: doc; src: url({regular}); }}",
        f"@font-face {{ font-family: doc; font-weight: bold; src: url({bold}); }}",
        css("title", _style(fmt, "title")), css("heading", _style(fmt, "heading")),
        css("body", _style(fmt, "body")), css("note", _style(fmt, "note")),
        ".heading { margin-top: 10pt; } table { border-collapse: collapse; width: 100%; margin: 4pt 0 8pt 0; }",
        f"td {{ font-family: doc; font-size: {_style(fmt, 'body').get('size', 12) - 1}pt; "
        "border: 0.5pt solid #666; padding: 3pt 5pt; }",
    ] + [css(f"level{i}", _level(fmt, i)) for i in range(3)]
    parts = []
    for block in model.parse(markdown):
        if block.kind == TABLE:
            rows = "".join("<tr>" + "".join(
                f"<td>{'<b>' if r == 0 else ''}{escape(cell)}{'</b>' if r == 0 else ''}</td>" for cell in row)
                + "</tr>" for r, row in enumerate(block.rows))
            parts.append(f"<table>{rows}</table>")
        elif block.kind == BULLET:
            # 앞 칸은 공백 대신 여백으로 준다. HTML은 줄 앞 공백을 지운다.
            style = _level(fmt, block.level)
            indent = int(style.get("lead", 0)) * style.get("size", 12) * 0.5
            parts.append(f'<p class="level{min(block.level, 2)}" style="margin-left: {indent:.0f}pt">'
                         f"{escape(str(style.get('glyph', '-')))} {escape(block.text)}</p>")
        else:
            name = {TITLE: "title", HEADING: "heading", NOTE: "note", PARAGRAPH: "body"}[block.kind]
            text = ("※ " if block.kind == NOTE else "") + block.text
            parts.append(f'<p class="{name}">{escape(text)}</p>')
    page = fmt.get("page", {})
    margin = page.get("margin_mm", {})
    mm = 72 / 25.4
    media = pymupdf.Rect(0, 0, page.get("width_mm", 210) * mm, page.get("height_mm", 297) * mm)
    where = media + (margin.get("left", 20) * mm, margin.get("top", 20) * mm,
                     -margin.get("right", 20) * mm, -margin.get("bottom", 15) * mm)
    story = pymupdf.Story(html="".join(parts), user_css="\n".join(rules), archive=pymupdf.Archive(str(font_dir)))
    buffer = io.BytesIO()
    writer = pymupdf.DocumentWriter(buffer)
    more = True
    while more:
        device = writer.begin_page(media)
        more, _filled = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()
    with pymupdf.open("pdf", buffer.getvalue()) as document:
        document.set_metadata({"title": title})
        document.subset_fonts()         # 글꼴을 통째로 넣으면 한 쪽짜리 문서가 수십 MB가 된다
        return document.tobytes(garbage=4, deflate=True)


# ── HWPX ────────────────────────────────────────────────────────────
_HWPX_TEXT_WIDTH = 42520        # A4에서 좌우 여백 20mm를 뺀 폭(HWPUNIT)
_HWPX_LANGS = ("HANGUL", "LATIN", "HANJA", "JAPANESE", "OTHER", "SYMBOL", "USER")
_HWPX_ALIGN = {"left": "LEFT", "center": "CENTER", "right": "RIGHT", "justify": "JUSTIFY"}
# 문단·글자 모양 번호. 본문 0 · 제목 1 · 항목 제목 2 · 목록 3~5 · 주의 6 · 표 머리 7
_ID = {"body": 0, "title": 1, "heading": 2, "level0": 3, "level1": 4, "level2": 5, "note": 6, "cell_head": 7}


def _hwpunit(mm: float) -> int:
    return round(mm * 7200 / 25.4)


def _hwpx_header(fmt: dict) -> str:
    styles = {"body": _style(fmt, "body"), "title": _style(fmt, "title"), "heading": _style(fmt, "heading"),
              "level0": {**_level(fmt, 0), "align": "left"}, "level1": {**_level(fmt, 1), "align": "left"},
              "level2": {**_level(fmt, 2), "align": "left"}, "note": _style(fmt, "note"),
              "cell_head": {**_style(fmt, "body"), "bold": True, "align": "center"}}
    fonts: list[str] = []
    for style in styles.values():
        if style.get("font", "맑은 고딕") not in fonts:
            fonts.append(style.get("font", "맑은 고딕"))
    faces = "".join(
        f'<hh:fontface lang="{lang}" fontCnt="{len(fonts)}">'
        + "".join(f'<hh:font id="{i}" face="{escape(font, quote=True)}" type="TTF" isEmbedded="0"/>'
                  for i, font in enumerate(fonts)) + "</hh:fontface>" for lang in _HWPX_LANGS)

    def spread(value) -> str:
        return " ".join(f'{lang.lower()}="{value}"' for lang in _HWPX_LANGS)

    char_props, para_props, named = [], [], []
    for name, key in _ID.items():
        style = styles[name]
        font = fonts.index(style.get("font", "맑은 고딕"))
        char_props.append(
            f'<hh:charPr id="{key}" height="{round(style.get("size", 12) * 100)}" textColor="#000000" '
            f'shadeColor="none" useFontSpace="0" useKerning="0" symMark="NONE" borderFillIDRef="1">'
            f"<hh:fontRef {spread(font)}/><hh:ratio {spread(100)}/><hh:spacing {spread(0)}/>"
            f"<hh:relSz {spread(100)}/><hh:offset {spread(0)}/>"
            + ("<hh:bold/>" if style.get("bold") else "") + "</hh:charPr>")
        para_props.append(
            f'<hh:paraPr id="{key}" tabPrIDRef="0" condense="0" fontLineHeight="0" snapToGrid="1" '
            f'suppressLineNumbers="0" checked="0">'
            f'<hh:align horizontal="{_HWPX_ALIGN.get(style.get("align", "left"), "LEFT")}" vertical="BASELINE"/>'
            '<hh:heading type="NONE" idRef="0" level="0"/>'
            '<hh:breakSetting breakLatinWord="KEEP_WORD" breakNonLatinWord="KEEP_WORD" widowOrphan="0" '
            'keepWithNext="0" keepLines="0" pageBreakBefore="0" lineWrap="BREAK"/>'
            '<hh:autoSpacing eAsianEng="0" eAsianNum="0"/>'
            '<hh:margin><hc:intent value="0" unit="HWPUNIT"/><hc:left value="0" unit="HWPUNIT"/>'
            '<hc:right value="0" unit="HWPUNIT"/><hc:prev value="0" unit="HWPUNIT"/>'
            '<hc:next value="0" unit="HWPUNIT"/></hh:margin>'
            f'<hh:lineSpacing type="PERCENT" value="{int(style.get("line", 160))}" unit="HWPUNIT"/>'
            '<hh:border borderFillIDRef="1" offsetLeft="0" offsetRight="0" offsetTop="0" offsetBottom="0" '
            'connect="0" ignoreMargin="0"/></hh:paraPr>')
        label = {"body": "바탕글", "title": "문서제목", "heading": "항목제목", "level0": "목록1",
                 "level1": "목록2", "level2": "목록3", "note": "주의", "cell_head": "표머리"}[name]
        named.append(f'<hh:style id="{key}" type="PARA" name="{label}" engName="{name}" paraPrIDRef="{key}" '
                     f'charPrIDRef="{key}" nextStyleIDRef="0" langID="1042" lockForm="0"/>')

    def border(key: int, kind: str) -> str:
        sides = "".join(f'<hh:{side} type="{kind}" width="0.12 mm" color="#000000"/>'
                        for side in ("leftBorder", "rightBorder", "topBorder", "bottomBorder"))
        return (f'<hh:borderFill id="{key}" threeD="0" shadow="0" centerLine="NONE" breakCellSeparateLine="0">'
                '<hh:slash type="NONE" Crooked="0" isCounter="0"/><hh:backSlash type="NONE" Crooked="0" isCounter="0"/>'
                f'{sides}<hh:diagonal type="NONE" width="0.1 mm" color="#000000"/></hh:borderFill>')

    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<hh:head xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head" '
            'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core" version="1.4" secCnt="1">'
            '<hh:beginNum page="1" footnote="1" endnote="1" pic="1" tbl="1" equation="1"/><hh:refList>'
            f'<hh:fontfaces itemCnt="{len(_HWPX_LANGS)}">{faces}</hh:fontfaces>'
            f'<hh:borderFills itemCnt="2">{border(1, "NONE")}{border(2, "SOLID")}</hh:borderFills>'
            f'<hh:charProperties itemCnt="{len(char_props)}">{"".join(char_props)}</hh:charProperties>'
            '<hh:tabProperties itemCnt="1"><hh:tabPr id="0" autoTabLeft="0" autoTabRight="0"/></hh:tabProperties>'
            f'<hh:paraProperties itemCnt="{len(para_props)}">{"".join(para_props)}</hh:paraProperties>'
            f'<hh:styles itemCnt="{len(named)}">{"".join(named)}</hh:styles>'
            "</hh:refList></hh:head>")


def _hwpx_section_properties(fmt: dict) -> str:
    page = fmt.get("page", {})
    margin = page.get("margin_mm", {})
    return (
        '<hp:secPr id="" textDirection="HORIZONTAL" spaceColumns="1134" tabStop="8000" tabStopVal="4000" '
        'tabStopUnit="HWPUNIT" outlineShapeIDRef="0" memoShapeIDRef="0" textVerticalWidthHead="0" masterPageCnt="0">'
        '<hp:grid lineGrid="0" charGrid="0" wonggojiFormat="0"/>'
        '<hp:startNum pageStartsOn="BOTH" page="0" pic="0" tbl="0" equation="0"/>'
        '<hp:visibility hideFirstHeader="0" hideFirstFooter="0" hideFirstMasterPage="0" border="SHOW_ALL" '
        'fill="SHOW_ALL" hideFirstPageNum="0" hideFirstEmptyLine="0" showLineNumber="0"/>'
        f'<hp:pagePr landscape="WIDELY" width="{_hwpunit(page.get("width_mm", 210))}" '
        f'height="{_hwpunit(page.get("height_mm", 297))}" gutterType="LEFT_ONLY">'
        f'<hp:margin header="{_hwpunit(10)}" footer="{_hwpunit(10)}" gutter="0" '
        f'left="{_hwpunit(margin.get("left", 20))}" right="{_hwpunit(margin.get("right", 20))}" '
        f'top="{_hwpunit(margin.get("top", 20))}" bottom="{_hwpunit(margin.get("bottom", 15))}"/></hp:pagePr>'
        '<hp:footNotePr><hp:autoNumFormat type="DIGIT" userChar="" prefixChar="" suffixChar=")" supscript="0"/>'
        '<hp:noteLine length="-1" type="SOLID" width="0.12 mm" color="#000000"/>'
        '<hp:noteSpacing betweenNotes="283" belowLine="567" aboveLine="850"/>'
        '<hp:numbering type="CONTINUOUS" newNum="1"/><hp:placement place="EACH_COLUMN" beneathText="0"/></hp:footNotePr>'
        '<hp:endNotePr><hp:autoNumFormat type="DIGIT" userChar="" prefixChar="" suffixChar=")" supscript="0"/>'
        '<hp:noteLine length="14692344" type="SOLID" width="0.12 mm" color="#000000"/>'
        '<hp:noteSpacing betweenNotes="0" belowLine="567" aboveLine="850"/>'
        '<hp:numbering type="CONTINUOUS" newNum="1"/><hp:placement place="END_OF_DOCUMENT" beneathText="0"/></hp:endNotePr>'
        "</hp:secPr>")


def _hwpx_paragraph(index: int, key: int, text: str, extra: str = "") -> str:
    return (f'<hp:p id="{index}" paraPrIDRef="{key}" styleIDRef="{key}" pageBreak="0" columnBreak="0" merged="0">'
            f'<hp:run charPrIDRef="{key}">{extra}<hp:t>{escape(text)}</hp:t></hp:run></hp:p>')


def _hwpx_table(index: int, rows: list[list[str]], width: int, extra: str = "") -> str:
    columns = len(rows[0])
    cell_width, cell_height = width // columns, 1700
    body = []
    for r, row in enumerate(rows):
        cells = []
        for c, text in enumerate(row):
            key = _ID["cell_head"] if r == 0 else _ID["body"]
            cells.append(
                '<hp:tc name="" header="0" hasMargin="0" protect="0" editable="0" dirty="0" borderFillIDRef="2">'
                '<hp:subList id="" textDirection="HORIZONTAL" lineWrap="BREAK" vertAlign="CENTER" linkListIDRef="0" '
                'linkListNextIDRef="0" textWidth="0" textHeight="0" hasTextRef="0" hasNumRef="0">'
                + _hwpx_paragraph(0, key, text) + "</hp:subList>"
                f'<hp:cellAddr colAddr="{c}" rowAddr="{r}"/><hp:cellSpan colSpan="1" rowSpan="1"/>'
                f'<hp:cellSz width="{cell_width}" height="{cell_height}"/>'
                '<hp:cellMargin left="510" right="510" top="141" bottom="141"/></hp:tc>')
        body.append("<hp:tr>" + "".join(cells) + "</hp:tr>")
    return (
        f'<hp:p id="{index}" paraPrIDRef="0" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">'
        f'<hp:run charPrIDRef="0">{extra}'
        f'<hp:tbl id="{1000 + index}" zOrder="0" numberingType="TABLE" textWrap="TOP_AND_BOTTOM" textFlow="BOTH_SIDES" '
        f'lock="0" dropcapstyle="None" pageBreak="CELL" repeatHeader="1" rowCnt="{len(rows)}" colCnt="{columns}" '
        'cellSpacing="0" borderFillIDRef="2" noAdjust="0">'
        f'<hp:sz width="{cell_width * columns}" widthRelTo="ABSOLUTE" height="{cell_height * len(rows)}" '
        'heightRelTo="ABSOLUTE" protect="0"/>'
        '<hp:pos treatAsChar="1" affectLSpacing="0" flowWithText="1" allowOverlap="0" holdAnchorAndSO="0" '
        'vertRelTo="PARA" horzRelTo="COLUMN" vertAlign="TOP" horzAlign="LEFT" vertOffset="0" horzOffset="0"/>'
        '<hp:outMargin left="0" right="0" top="0" bottom="0"/><hp:inMargin left="510" right="510" top="141" bottom="141"/>'
        + "".join(body) + "</hp:tbl><hp:t/></hp:run></hp:p>")


def to_hwpx(markdown: str, fmt: dict, title: str) -> bytes:
    margin = fmt.get("page", {}).get("margin_mm", {})
    width = _hwpunit(fmt.get("page", {}).get("width_mm", 210) - margin.get("left", 20) - margin.get("right", 20))
    blocks = model.parse(markdown) or [model.Block(PARAGRAPH, "")]
    paragraphs = []
    for index, block in enumerate(blocks):
        # 구역 설정(용지·여백)은 첫 문단의 첫 글자 묶음 안에 둔다.
        extra = _hwpx_section_properties(fmt) if index == 0 else ""
        if block.kind == TABLE:
            paragraphs.append(_hwpx_table(index, block.rows, width, extra))
        elif block.kind == BULLET:
            paragraphs.append(_hwpx_paragraph(index, _ID[f"level{min(block.level, 2)}"],
                                              _bullet_text(fmt, block), extra))
        else:
            key = {TITLE: "title", HEADING: "heading", NOTE: "note", PARAGRAPH: "body"}[block.kind]
            text = ("※ " if block.kind == NOTE else "") + block.text
            paragraphs.append(_hwpx_paragraph(index, _ID[key], text, extra))
    section = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
               'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
               'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core">' + "".join(paragraphs) + "</hs:sec>")
    files = {
        "version.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       '<hv:HCFVersion xmlns:hv="http://www.hancom.co.kr/hwpml/2011/version" tagetApplication="WORDPROCESSOR" '
                       'major="5" minor="1" micro="0" buildNumber="1" os="1" xmlVersion="1.4" '
                       'application="NunchiCoach" appVersion="1"/>',
        "META-INF/container.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                                  '<ocf:container xmlns:ocf="urn:oasis:names:tc:opendocument:xmlns:container">'
                                  '<ocf:rootfiles><ocf:rootfile full-path="Contents/content.hpf" '
                                  'media-type="application/hwpml-package+xml"/></ocf:rootfiles></ocf:container>',
        "Contents/content.hpf": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                                '<opf:package xmlns:opf="http://www.idpf.org/2007/opf/" '
                                'xmlns:dc="http://purl.org/dc/elements/1.1/" version="" unique-identifier="" id="">'
                                f"<opf:metadata><opf:title>{escape(title)}</opf:title><opf:language>ko</opf:language>"
                                "</opf:metadata><opf:manifest>"
                                '<opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>'
                                '<opf:item id="section0" href="Contents/section0.xml" media-type="application/xml"/>'
                                '<opf:item id="settings" href="settings.xml" media-type="application/xml"/>'
                                '</opf:manifest><opf:spine><opf:itemref idref="header" linear="yes"/>'
                                '<opf:itemref idref="section0" linear="yes"/></opf:spine></opf:package>',
        "Contents/header.xml": _hwpx_header(fmt),
        "Contents/section0.xml": section,
        "settings.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                        '<ha:HWPApplicationSetting xmlns:ha="http://www.hancom.co.kr/hwpml/2011/app" '
                        'xmlns:config="urn:oasis:names:tc:opendocument:xmlns:config:1.0">'
                        '<ha:CaretPosition listIDRef="0" paraIDRef="0" pos="0"/></ha:HWPApplicationSetting>',
        "Preview/PrvText.txt": model.plain(markdown),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        # mimetype은 맨 앞에, 압축하지 않고 넣는다 — 꾸러미 형식의 약속이다.
        package.writestr(zipfile.ZipInfo("mimetype"), "application/hwp+zip", compress_type=zipfile.ZIP_STORED)
        for name, content in files.items():
            package.writestr(name, content.encode("utf-8"))
    return buffer.getvalue()


RENDERERS = {"md": to_markdown, "docx": to_docx, "hwpx": to_hwpx, "pdf": to_pdf, "xlsx": to_xlsx}
FILTERS = {
    "hwpx": "한글 문서 (*.hwpx)", "docx": "워드 문서 (*.docx)", "pdf": "PDF (*.pdf)",
    "xlsx": "엑셀 통합문서 (*.xlsx)", "md": "Markdown (*.md)",
}
