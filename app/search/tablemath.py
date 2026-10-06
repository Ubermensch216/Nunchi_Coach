"""표에서 가장 큰/작은 값 — 규칙으로 답한다.

"완료율이 가장 높았던 분기는?" 같은 질문은 표의 한 행에서 값을 견주면 끝난다.
경량 모델은 이것을 자주 틀린다(30문항 평가: 92·88·95·90 가운데 90을 골랐다).
규칙으로 정할 수 있는 것을 모델에 맡기지 않는다(제품 원칙 3).

답하는 조건을 좁게 잡는다. 하나라도 어긋나면 None을 돌려주고 평소처럼 모델에 묻는다.
  · 질문에 '가장 높은/낮은', '최대/최소' 같은 말이 있다.
  · 근거의 표 가운데 질문이 이름을 부른 행이 **하나뿐**이다.
  · 그 행의 값이 둘 이상이고 모두 숫자이며, 가장 큰(작은) 값이 하나뿐이다.
  · 그 행이 여러 근거에 나오면 값이 모두 같다(해마다 다른 표를 섞어 견주지 않는다).
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .verify import _tables

_MAX = re.compile(r"가장\s*(?:높|많|큰|크|컸|늘)|최대|최고|최다")
_MIN = re.compile(r"가장\s*(?:낮|적|작|줄)|최소|최저")
_MAX_SOURCES = 3


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
