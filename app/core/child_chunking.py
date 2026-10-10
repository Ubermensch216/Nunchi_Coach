"""검색용 자식 창. 부모 원문·위치·ID는 바꾸지 않는다."""
from dataclasses import dataclass
import re

CHILD_VERSION = "1"
CHILD_CHARS = 500


@dataclass(frozen=True, slots=True)
class Child:
    ordinal: int
    start: int
    end: int
    text: str


def child_version(context: str = "") -> str:
    """자식 한 벌의 판. 제목 문맥을 붙여 만든 자식은 따로 표시한다 — 문맥이 생긴
    부모의 자식만 다시 만들고, 나머지는 그대로 둔다."""
    return CHILD_VERSION + ("+c" if context else "")


def build_children(text: str, context: str = "") -> list[Child]:
    """산문은 문장/줄 경계, 탭 표는 행 경계에서 나눈다.

    start/end는 부모 원문 범위다. 표 검색문에는 첫 행(시트명 포함)을
    반복하지만 범위는 반복문을 제외한다. 긴 표 행은 값 연결을 위해 보존한다.
    context(부모가 속한 제목들)는 검색문 맨 앞에만 붙는다 — '집행 현황' 같은 절 제목이
    본문에 다시 나오지 않아도 그 절의 대목을 찾게 한다. 범위와 인용에는 들지 않는다.
    """
    if not text.strip():
        return []
    lead = f"{context}\n" if context else ""
    table = "\t" in text
    prefix = ""
    if table:
        lines = text.splitlines(keepends=True)
        prefix = lines[0]
        if lines[0].strip().startswith("[") and len(lines) > 1:
            prefix += lines[1]
        boundaries = [m.end() for m in re.finditer(r"\n", text)] + [len(text)]
    else:
        boundaries = [m.end() for m in re.finditer(r"\n|(?<=[.!?。])\s+", text)] + [len(text)]
    out = []
    start = 0
    while start < len(text):
        available = max(1, CHILD_CHARS - (len(prefix) if start else 0))
        fits = [end for end in boundaries if start < end <= start + available]
        if fits:
            end = max(fits)
        elif table:
            end = next(end for end in boundaries if end > start)
        else:
            end = min(start + available, len(text))
        body = text[start:end]
        if body.strip():
            out.append(Child(len(out) + 1, start, end, lead + (prefix if start and table else "") + body))
        start = end
    return out
