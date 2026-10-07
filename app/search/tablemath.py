"""표에서 가장 큰/작은 값과 한 칸의 수량 — 규칙으로 답한다.

"완료율이 가장 높았던 분기는?" 같은 질문은 표의 한 행에서 값을 견주면 끝난다.
경량 모델은 이것을 자주 틀린다(30문항 평가: 92·88·95·90 가운데 90을 골랐다).
규칙으로 정할 수 있는 것을 모델에 맡기지 않는다(제품 원칙 3).

답하는 조건을 좁게 잡는다. 하나라도 어긋나면 None을 돌려주고 평소처럼 모델에 묻는다.
  · 질문에 '가장 높은/낮은', '최대/최소' 같은 말이 있다.
  · 근거의 표 가운데 질문이 이름을 부른 행이 **하나뿐**이다.
  · 그 행의 값이 둘 이상이고 모두 숫자이며, 가장 큰(작은) 값이 하나뿐이다.
  · 그 행이 여러 근거에 나오면 값이 모두 같다(해마다 다른 표를 섞어 견주지 않는다).

"요구자료는 몇 건이었어?"처럼 표의 한 칸을 묻는 질문도 같다(count). 표에
`요구자료 | 12`가 그대로 있는데 모델은 "수량은 표를 통해 확인할 수 있습니다"라고만
했다(30문항 평가, 3회 중 2~3회). 조건은 extreme보다 더 좁다.
  · 질문이 '몇 건·몇 개'나 '건수·수량'을 묻고, 견주는 질문이 아니다. 행과 열을
    모두 부른 질문은 '얼마'로 물어도 된다("1분기 예산요구액은 얼마야?" — 모델은
    옆 행의 값을 답했고 검증이 버려 유보로 끝났다. 60문항 평가).
  · 질문이 이름을 부른 행이 하나뿐이다.
  · 질문이 열 이름을 하나 불렀으면 그 칸, 부르지 않았으면 그 행에 숫자 칸이
    하나뿐이고 그 열이 수량·건수 따위다.
  · 수량을 묻는 말을 뺀 질문의 낱말이 모두 그 문서에 있다 — "처리건수 목표치는
    몇 건이야?"의 '목표치'처럼 표가 말하지 않는 것을 물으면 손대지 않는다.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from ..core import korean
from .verify import _tables

_MAX = re.compile(r"가장\s*(?:높|많|큰|크|컸|늘)|최대|최고|최다")
_MIN = re.compile(r"가장\s*(?:낮|적|작|줄)|최소|최저")
_MAX_SOURCES = 3
# 수량을 묻는 말. '몇 월·몇 년'은 수량이 아니고, '얼마'는 금액일 수 있어 뺐다.
_HOW_MANY = re.compile(r"몇\s*(?:건|개|회|차례|번|명|부|종|곳)[가-힣]*|(?:건수|수량|개수|횟수)[가-힣]*")
_HOW_MUCH = re.compile(r"얼마[가-힣]*")
_COUNT_HEADER = re.compile(r"수량|건수|개수|횟수|인원|물량")


def extreme(question: str, source_text: dict[int, str]) -> tuple[str, list[int]] | None:
    """(답 문장, 근거 번호들). 규칙으로 답할 수 없으면 None."""
    highest, lowest = bool(_MAX.search(question)), bool(_MIN.search(question))
    if highest == lowest:
        return None
    squeezed = re.sub(r"\s+", "", question)

    found: dict[str, tuple] = {}        # 행 이름 → (열 이름들, 값들)
    sources: dict[str, list[int]] = {}
    for number, text in source_text.items():
        for headers, rows in _tables(text):
            for row in rows:
                label = re.sub(r"\s+", "", row[0]) if row else ""
                if len(label) < 2 or label not in squeezed:
                    continue
                shape = (tuple(headers[1:]), tuple(row[1:]))
                if found.setdefault(label, shape) != shape:
                    return None         # 같은 행이 근거마다 다른 값이다
                sources.setdefault(label, []).append(number)
    if len(found) != 1:
        return None
    (label, (columns, cells)), = found.items()
    if len(cells) < 2 or len(columns) != len(cells):
        return None
    try:
        values = [Decimal(cell.replace(",", "")) for cell in cells]
    except InvalidOperation:
        return None
    best = max(values) if highest else min(values)
    if values.count(best) != 1:
        return None
    index = values.index(best)
    listing = ", ".join(f"{name} {cell}" for name, cell in zip(columns, cells))
    text = (f"{label}의 값은 {listing}이며, 그 가운데 {columns[index]}의 {cells[index]}이(가) "
            f"가장 {'큽니다' if highest else '작습니다'}.")
    return text, sources[label][:_MAX_SOURCES]


def count(question: str, source_text: dict[int, str],
          support_text: dict[int, str] | None = None) -> tuple[str, list[int]] | None:
    """(답 문장, 근거 번호들). 표의 한 칸으로 답할 수 없는 질문이면 None.

    support_text는 근거 번호마다 같은 문서의 글이다. 질문의 낱말이 그 문서에
    모두 있는 근거만 쓴다 — 해가 다른 문서의 같은 표를 섞지 않는다.
    """
    how_many, how_much = bool(_HOW_MANY.search(question)), bool(_HOW_MUCH.search(question))
    if not (how_many or how_much) or _MAX.search(question) or _MIN.search(question):
        return None
    squeezed = re.sub(r"\s+", "", question)
    # '처리건수는 몇 건'의 '처리건수'는 행 이름이다. 낱말 대조에서는 그대로 두고
    # '몇 건…'과 '얼마…'만 뺀다.
    words = korean.tokens(_HOW_MUCH.sub(" ", re.sub(
        r"몇\s*(?:건|개|회|차례|번|명|부|종|곳)[가-힣]*", " ", question)))
    support_text = support_text or source_text

    answers: dict[tuple[str, str, str], list[int]] = {}
    for number, text in source_text.items():
        about = support_text.get(number, text)
        if not all(_mentions(about, word) for word in words):
            continue
        for headers, rows in _tables(text):
            named = [i for i, name in enumerate(headers)
                     if i > 0 and len(name) >= 2 and re.sub(r"\s+", "", name) in squeezed]
            for row in rows:
                label = re.sub(r"\s+", "", row[0]) if row else ""
                if len(label) < 2 or label not in squeezed or len(row) != len(headers):
                    continue
                numeric = [i for i in range(1, len(row)) if _is_number(row[i])]
                if len(named) == 1 and named[0] in numeric:
                    column = named[0]
                elif (how_many and not named and len(numeric) == 1
                      and _COUNT_HEADER.search(headers[numeric[0]])):
                    column = numeric[0]
                else:
                    return None         # 어느 칸을 묻는지 정할 수 없다
                answers.setdefault((label, headers[column], row[column]), []).append(number)
    if len(answers) != 1:
        return None                     # 행이 없거나, 둘이거나, 근거마다 값이 다르다
    ((label, header, value), sources), = answers.items()
    return f"표의 '{label}' 행 '{header}' 칸 값은 {value}입니다.", sources[:_MAX_SOURCES]


def _is_number(cell: str) -> bool:
    try:
        Decimal(cell.replace(",", ""))
    except InvalidOperation:
        return False
    return bool(cell.strip())


def _mentions(text: str, word: str) -> bool:
    """낱말이 글에 있는가. 숫자는 통째로 같아야 한다 — 어간 대조에 맡기면
    '2025년'이 '2024년'의 '202'와 겹쳐 다른 해의 문서까지 받아들인다."""
    numbers = re.findall(r"\d+", word)
    if numbers:
        return all(re.search(rf"(?<!\d){number}(?!\d)", text) for number in numbers)
    # '집계표에서'는 '[집계]'를 가리킨다 — 합성어는 앞 두 글자로도 받아 준다.
    return korean.contains(text, word) or (len(word) >= 4 and word[:2] in text)
