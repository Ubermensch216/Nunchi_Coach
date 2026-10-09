"""산출물을 서식이 입혀진 문서 모양으로 보여 준다.

본문은 간단한 표기(#, -, >)로 저장돼 있다(studio/model.py). 그 표기를 그대로
보여 주면 사람은 기호를 걸러 가며 읽어야 한다. 여기서는 파일로 내보낼 때와
같은 줄 목록(studio/layout.py)을 받아, 서식의 글꼴·크기·기호·앞 칸대로 그린다 —
화면에서 본 모양이 내보낸 파일의 모양이다.

글은 반드시 먼저 이스케이프한다(answer_html.py와 같은 이유).
"""

from __future__ import annotations

import html
import re

from ....studio import markers
from ....studio.layout import Line, lay_out
from ....studio.model import BULLET, HEADING, NOTE, TABLE, TITLE
from ... import theme

_CITATION = re.compile(r"((?:\[\d+\])+)")
# 화면의 판은 종이보다 좁다. 크기의 비례(제목 > 항목 제목 > 본문)는 지키고 통째로 줄인다.
PANEL_SCALE = 0.82
PAGE_SCALE = 1.0
_MIN_PT = 9.0


def _text(text: str) -> str:
    """근거 번호는 본문보다 작고 옅게 — 읽는 흐름을 끊지 않으면서 어디에 근거가 있는지는 보인다."""
    return _CITATION.sub(
        lambda m: (f'<span style="color:{theme.PRIMARY}; font-size:{_MIN_PT:g}pt;">'
                   f'{" " if m.start() else ""}{m.group(1)}</span>'),
        html.escape(text))


def _shown(glyph: str, slot: str) -> str:
    """한컴 전용 글자(사용자 영역)는 화면 글꼴에 없다. 비슷한 모양으로 바꿔 보여 준다 — 파일에는 원래 글자가 들어간다."""
    if not markers.is_private(glyph):
        return glyph
    return "□" if slot == "heading" else "①"


def _font(style: dict, scale: float) -> str:
    family = str(style.get("font") or "").replace("'", "")
    size = max(_MIN_PT, float(style.get("size", 12)) * scale)
    return (f"font-family:'{family}'; " if family else "") + (
        f"font-size:{size:.1f}pt; font-weight:{700 if style.get('bold') else 400}; "
        f"line-height:{int(style.get('line', 160))}%;")


def _line(line: Line, scale: float) -> str:
    style = line.style
    size = max(_MIN_PT, float(style.get("size", 12)) * scale)
    above = min(float(style.get("before") or 0), 24) * scale
    if line.kind == TITLE:
        frame = (f"border-top:1px solid {theme.BORDER_STRONG}; border-bottom:1px solid {theme.BORDER_STRONG}; "
                 "padding:8px 4px;")
        return (f'<table width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 14px 0;"><tr>'
                f'<td align="center" style="{frame} {_font(style, scale)}">{_text(line.body)}</td></tr></table>')
    if line.kind == HEADING and style.get("box"):
        # Ⅰ 대제목 막대 — 번호 칸은 칠하고 제목 칸은 테두리만 둔다.
        return (f'<table width="100%" cellspacing="0" cellpadding="0" style="margin:{above + 12:.0f}px 0 6px 0;"><tr>'
                f'<td width="1" align="center" style="background:{theme.PRIMARY}; color:{theme.TEXT_ON_PRIMARY}; '
                f'padding:3px 9px; {_font(style, scale)}">{html.escape(line.glyph.rstrip("."))}</td>'
                f'<td style="border:1px solid {theme.PRIMARY}; padding:3px 9px; {_font(style, scale)}">'
                f"{_text(line.body)}</td></tr></table>")
    glyph = _shown(line.glyph, line.slot)
    # 앞 칸은 반각 공백 폭(글자 크기의 절반쯤)으로 옮긴다. 둘째 줄부터는 기호 뒤에 맞춘다(내어쓰기).
    step = size * 1.33 * 0.5
    hang = (len(glyph) + 1) * step * 1.25 if glyph else 0
    left = line.lead * step + hang
    if line.kind == HEADING:
        above += 14
    elif line.kind == BULLET:
        above += 2
    color = theme.ATTENTION if line.kind == NOTE else theme.TEXT
    align = "left" if style.get("align") == "justify" else style.get("align", "left")
    # 줄바꿈 없는 공백(nbsp)은 쓰지 않는다 — 서식의 글꼴이 이 PC에 없어 다른 글꼴이 대신 그릴 때 빈 네모로 나온다.
    mark = f"{html.escape(glyph)} " if glyph else ""
    return (f'<p align="{align}" style="margin:{above:.0f}px 0 4px {left:.0f}px; text-indent:{-hang:.0f}px; '
            f'color:{color}; {_font(style, scale)}">{mark}{_text(line.body)}</p>')


def _table(line: Line, scale: float) -> str:
    font = _font({**line.style, "bold": False, "size": float(line.style.get("size", 12)) - 1, "line": 140}, scale)
    pad = "padding:5px 9px;"
    rows = []
    for index, row in enumerate(line.rows):
        cells = "".join(
            f'<td style="{pad} border:1px solid {theme.BORDER_STRONG}; {font} '
            + (f'background:{theme.SURFACE}; font-weight:700;' if index == 0 else "")
            + f'">{_text(cell)}</td>' for cell in row)
        rows.append(f"<tr>{cells}</tr>")
    return ('<table width="100%" cellspacing="0" cellpadding="0" '
            f'style="border-collapse:collapse; margin:6px 0 12px 0;">{"".join(rows)}</table>')


def output_html(markdown: str, fmt: dict, scale: float = PANEL_SCALE) -> str:
    parts = [_table(line, scale) if line.kind == TABLE else _line(line, scale) for line in lay_out(markdown, fmt)]
    return f'<div style="color:{theme.TEXT};">{"".join(parts)}</div>'
