"""답 본문을 읽기 좋은 리치 텍스트로 옮긴다.

모델이 쓰는 글은 가벼운 마크다운이다 — 굵은 글씨, 목록, 제목, 그리고 표.
그대로 QLabel에 넣으면 `| 구분 | 금액 |` 같은 줄이 기호째 보이고, 목록은
한 덩어리 문단이 된다. 여기서 그 넷만 알아보고 Qt의 리치 텍스트로 바꾼다.

본문은 반드시 먼저 이스케이프한다(views/ask.py _linkify_citations와 같은 이유) —
문서에서 온 문장에 <, &가 섞이면 태그로 해석돼 답이 깨진다.
"""

from __future__ import annotations

import html
import re

from ... import theme

_CITATION = re.compile(r"\[(\d+)\]")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_BULLET = re.compile(r"^[-*•·▪◦–]\s+(.*)$")
# '9. 30.까지'처럼 날짜로 시작하는 줄을 번호 목록으로 읽지 않는다.
_NUMBERED = re.compile(r"^\d{1,2}[.)]\s+(?!\d{1,2}\.)(.*)$")
_RULE_LINE = re.compile(r"^[-*_]{3,}$")
_TABLE_RULE = re.compile(r"^\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?$")
_CELL_BREAK = re.compile(r"(?<!\\)\|")
# 수량 대조 답(search/rag.py)이 쓰는 줄 — `- 파일 · 기간 · 항목: 값 [1]`. 줄마다
# 같은 넷이 되풀이되므로 표로 세워야 값끼리 견주어진다.
_FACT_ROW = re.compile(r"^- (.+?) · (.+?) · (.+?): (.+) (\[\d+\])$")
_NUMERIC = re.compile(r"^[+-]?[\d,]+(?:\.\d+)?\s*(?:%|원|천원|백만원|억원|건|명|개|회|일)?$")
FACT_HEADER = ["자료", "기간", "항목", "값", "근거"]


def answer_html(text: str) -> str:
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            out.append(f'<p style="margin:0 0 10px 0; line-height:165%;">{"<br>".join(paragraph)}</p>')
            paragraph.clear()

    index = 0
    while index < len(lines):
        line = lines[index].strip()
        following = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if not line or _RULE_LINE.match(line):
            flush()
            index += 1
        elif "|" in line and "|" in following and _TABLE_RULE.match(following):
            flush()
            header, aligns = _cells(line), _aligns(following)
            index += 2
            rows = []
            while index < len(lines) and "|" in lines[index]:
                rows.append(_cells(lines[index]))
                index += 1
            out.append(_table(header, rows, aligns))
        elif _FACT_ROW.match(line):
            flush()
            rows = []
            while index < len(lines) and (match := _FACT_ROW.match(lines[index].strip())):
                rows.append(list(match.groups()))
                index += 1
            out.append(_table(FACT_HEADER, rows, []))
        elif match := _HEADING.match(line):
            flush()
            out.append(f'<p style="margin:6px 0 6px 0; font-weight:700;">{_inline(match.group(1))}</p>')
            index += 1
        elif _BULLET.match(line) or _NUMBERED.match(line):
            flush()
            pattern, tag = (_BULLET, "ul") if _BULLET.match(line) else (_NUMBERED, "ol")
            items = []
            while index < len(lines) and (match := pattern.match(lines[index].strip())):
                items.append(f'<li style="margin-bottom:5px; line-height:160%;">{_inline(match.group(1))}</li>')
                index += 1
            # Qt는 목록을 40px씩 들여쓴다. 좁은 대화 판에서는 한 단계면 충분하다.
            out.append(f'<{tag} style="margin:0 0 10px 0; -qt-list-indent:1;">{"".join(items)}</{tag}>')
        else:
            paragraph.append(_inline(line))
            index += 1
    flush()
    return "".join(out)


def _inline(text: str) -> str:
    escaped = html.escape(text or "")
    escaped = _BOLD.sub(r"<b>\1</b>", escaped)
    # 고정폭 글꼴에는 한글이 없어 다른 글꼴이 끼어든다. 역따옴표만 걷어 낸다.
    escaped = _CODE.sub(r"\1", escaped)
    # 색을 직접 준다. 맡겨 두면 QLabel은 팔레트의 링크 색을 써서 거의 안 보인다.
    return _CITATION.sub(
        lambda m: (f'<a href="{m.group(1)}" style="color:{theme.PRIMARY}; font-weight:700; '
                   f'text-decoration:none;">[{m.group(1)}]</a>'),
        escaped)


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [cell.strip().replace("\\|", "|") for cell in _CELL_BREAK.split(line)]


def _aligns(rule: str) -> list[str]:
    marks = []
    for cell in _cells(rule):
        if cell.endswith(":"):
            marks.append("center" if cell.startswith(":") else "right")
        else:
            marks.append("")
    return marks


def _table(header: list[str], rows: list[list[str]], aligns: list[str]) -> str:
    """가로줄만 있는 표. 세로줄과 바탕을 빼야 좁은 판에서도 숫자가 먼저 읽힌다."""
    width = max([len(header)] + [len(row) for row in rows])
    header = header + [""] * (width - len(header))
    rows = [row + [""] * (width - len(row)) for row in rows]
    aligns = aligns + [""] * (width - len(aligns))
    for column in range(width):
        if not aligns[column]:
            values = [row[column] for row in rows if row[column]]
            # 숫자만 든 칸은 오른쪽으로 맞춘다 — 자릿수가 세로로 맞아야 크기가 견주어진다.
            aligns[column] = "right" if values and all(_NUMERIC.match(v) for v in values) else "left"
    pad = "padding:7px 12px;"
    head = "".join(
        f'<th align="{aligns[i]}" style="{pad} border-bottom:1px solid {theme.BORDER_STRONG}; '
        f'color:{theme.TEXT_MUTED}; font-weight:600;">{_inline(cell)}</th>'
        for i, cell in enumerate(header))
    body = "".join(
        "<tr>" + "".join(
            f'<td align="{aligns[i]}" style="{pad} border-bottom:1px solid {theme.BORDER};">{_inline(cell)}</td>'
            for i, cell in enumerate(row)) + "</tr>"
        for row in rows)
    return ('<table width="100%" cellspacing="0" cellpadding="0" '
            'style="border-collapse:collapse; margin-top:2px; margin-bottom:12px;">'
            f"<tr>{head}</tr>{body}</table>")
