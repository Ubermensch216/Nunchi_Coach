"""증거창 — 조각에서 질문과 관련된 부분을 예산 안에 골라 담는다.

예전에는 조각의 앞 400자만 모델에 보여 줬다. 조각은 1,200자까지 커질 수
있어서 뒤쪽에 적힌 금액·기한은 검색에는 걸리고도 답변 재료에서는 빠졌다.
모델은 보이지 않는 값을 답할 수 없고, 검증도 같은 앞 400자만 봤으므로
옳은 답이 '근거에 없는 숫자'로 버려졌다.

규칙으로만 고른다 — 생성 호출을 늘리지 않는다.
  · 예산 안에 다 들어가면 그대로 쓴다(짧은 조각은 예전과 같다).
  · 표 조각의 문맥 줄(첫 행·머리글·단위)은 항상 남긴다. 그 줄이 빠지면
    '820'이 무엇의 얼마인지 알 수 없다.
  · 나머지는 문장 단위로 나눠 질문 낱말이 들어 있는 문장부터 담는다.
  · 원문 순서를 지키고, 건너뛴 자리는 '…'로 표시한다. 없는 문장을 이어
    붙인 것처럼 보이면 안 된다.
  · 질문 낱말이 어디에도 없으면 예전처럼 앞부분을 쓴다.
  · 표의 칸과 행의 경계를 남긴다. 한 줄로 펴면서 탭과 줄바꿈을 공백으로
    바꾸면 "집행액 0 210 430 180"이 되어 어느 값이 어느 분기인지 모델도
    검증도 알 수 없다(30문항 평가에서 2분기 값을 물었는데 3분기 값이 나갔다).
    칸은 ' | ', 행의 끝은 ' /'로 적는다.

여기서 만든 글이 곧 생성 문맥·사실 검증·화면의 근거 발췌다. 셋이 서로
다른 글을 보면 검증이 의미를 잃는다.
"""

from __future__ import annotations

import re

from ..core import korean

DEFAULT_BUDGET = 400
GAP = " … "
# 표 파서(hwpx·odt)가 행마다 붙이는 문맥 줄.
_PINNED = ("첫 행:", "머리글:", "단위:")
# 문장 끝. 숫자 뒤의 마침표("2024. 9. 12.")에서는 끊지 않는다.
_SENTENCE_END = re.compile(r"(?<=[^\d\s][.!?])\s+")
_HAS_DIGIT = re.compile(r"\d")


ROW_END = " /"
_CELL_LINE = re.compile(r"^\s*1열:")


def mark_tables(text: str) -> str:
    """표의 칸·행 경계를 한 줄로 펴도 남는 표시로 바꾼다."""
    lines = []
    for line in (text or "").splitlines():
        if "\t" in line:
            line = " | ".join(cell.strip() for cell in line.split("\t")) + ROW_END
        elif _CELL_LINE.match(line):
            line = line.rstrip() + ROW_END
        lines.append(line)
    return "\n".join(lines)


def window(text: str, question: str, budget: int = DEFAULT_BUDGET) -> str:
    text = mark_tables(text)
    flat = " ".join(text.split())
    if len(flat) <= budget:
        return flat

    terms = list(dict.fromkeys(korean.tokens(question or "")))
    pinned: list[str] = []
    segments: list[str] = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        if line.startswith(_PINNED):
            pinned.append(line)
        else:
            segments.extend(_split(line, max(budget // 2, 40)))

    scores = [_score(segment, terms) for segment in segments]
    if not any(scores):
        return flat[:budget]

    # 문맥 줄이 예산을 다 써 버리면 정작 값이 들어갈 자리가 없다.
    head = " ".join(pinned)[: budget // 3]
    room = budget - len(head) - (1 if head else 0)

    chosen: set[int] = set()
    by_relevance = sorted(range(len(segments)), key=lambda i: (-scores[i], i))
    for index in by_relevance:
        if not scores[index]:
            break
        room = _take(index, segments, chosen, room)
    # 남는 자리는 고른 문장의 앞뒤로 채운다 — 값 하나만 덩그러니 두지 않는다.
    for index in sorted(chosen):
        for neighbour in (index - 1, index + 1):
            if 0 <= neighbour < len(segments):
                room = _take(neighbour, segments, chosen, room)

    parts: list[str] = []
    previous = None
    for index in sorted(chosen):
        if previous is not None and index != previous + 1:
            parts.append(GAP.strip())
        parts.append(segments[index])
        previous = index
    body = " ".join(parts)
    return f"{head} {body}" if head else body


def _take(index: int, segments: list[str], chosen: set[int], room: int) -> int:
    if index in chosen:
        return room
    cost = len(segments[index]) + len(GAP)
    if cost > room:
        return room
    chosen.add(index)
    return room - cost


def _score(segment: str, terms: list[str]) -> float:
    hits = sum(1 for term in terms if korean.contains(segment, term))
    if not hits:
        return 0.0
    # 같은 낱말이 걸린 문장이 여럿이면 값(금액·기한)이 적힌 쪽을 먼저 담는다.
    return hits + (0.5 if _HAS_DIGIT.search(segment) else 0.0)


def _split(line: str, limit: int) -> list[str]:
    """문장 → 표 칸 → 낱말 순으로, limit를 넘지 않을 때까지만 쪼갠다."""
    out: list[str] = []
    for sentence in _SENTENCE_END.split(line):
        if len(sentence) <= limit:
            out.append(sentence)
            continue
        piece = ""
        for cell in sentence.split(" | "):
            joined = f"{piece} | {cell}" if piece else cell
            if len(joined) <= limit:
                piece = joined
                continue
            if piece:
                out.append(piece)
            piece = cell
            while len(piece) > limit:
                cut = piece.rfind(" ", 0, limit)
                cut = cut if cut > limit // 2 else limit
                out.append(piece[:cut].strip())
                piece = piece[cut:].strip()
        if piece:
            out.append(piece)
    return [part for part in out if part]
