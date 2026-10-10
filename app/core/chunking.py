"""문서를 검색·질의응답용 조각(chunk)으로 나눈다.

v1은 파서가 나눠 둔 문단·표·페이지·시트·슬라이드 경계를 그대로 하나씩
조각으로 썼다. 실측(2026-08, 실제 프로젝트 77건)에서 그 결과가 드러났다.

    조각 길이 중앙값 34자, 50자 미만이 63%.
    예: '문서유형: 보고서'(9자), '작성일: 2024. 9. 12.'(18자)

9자짜리 조각의 임베딩은 의미를 담지 못한다. RAG가 Top-8을 모아도 컨텍스트가
300자 남짓이라 모델에게 답할 재료 자체가 없었다 — 질문 화면이 그럴듯하지만
빈 답을 내놓은 원인 중 하나였다(doc/질문화면_RAG_개선계획.md §1 측정 6).

v2는 산문류(문단·페이지)를 목표 400~800자 창으로 병합한다. locator는
범위로 남긴다("1~5문단") — 근거 위치는 잃지 않는다.

표·시트·슬라이드는 병합하지 않는다. 그 경계 자체가 의미이고, locator가
셀 범위·슬라이드 번호를 정확히 가리켜야 근거로서 값을 한다.

v3은 구조를 지킨다. 상한을 넘는 섹션을 글자 수가 아니라 줄·문장·낱말 경계에서
나누고(행·문장·값이 두 조각에 걸치지 않는다), 파서가 알려 준 제목 계층을 조각의
문맥(context)으로 남기며, 제목이 바뀌는 곳에서 조각을 끊는다.
"""

from __future__ import annotations

import json
import re

MAX_CHUNK_CHARS = 1200        # 이 이상은 임베딩 하나가 감당하기 버겁다 — 나눈다
TARGET_MIN_CHARS = 400        # 이 아래에서는 계속 다음 섹션을 끌어와 합친다
TARGET_MAX_CHARS = 800        # 이 위인 섹션은 병합하지 않고 그대로 쓴다

# 산문류 — 순서대로 이어 읽는 글이라 인접 조각을 합쳐도 뜻이 유지된다.
# 표·시트·슬라이드·노트는 그 자체가 구조이므로 넣지 않는다.
MERGEABLE_KINDS = {"paragraph", "page"}

# v1은 버전을 남기지 않았다. v2부터 추적하므로 "2"에서 시작한다.
# 버전을 올려도 이미 있는 조각을 바로 지우지 않는다 — 옛 조각으로 계속 답하면서 새
# 조각을 준비하고, 검증한 뒤 바꾼다(core/reindex.py).
CHUNKING_VERSION = "3"

CONTEXT_SEPARATOR = " > "
CONTEXT_CHARS = 160           # 조각에 남기는 제목 문맥의 길이 상한

_LOCATOR_NUMBER = re.compile(r"^(\d+)(\D.*)$")
# 긴 섹션을 나눌 자리 — 앞의 것부터 찾는다.
_SPLIT_BOUNDARIES = (re.compile(r"\n+"), re.compile(r"(?<=[.!?。])\s+"), re.compile(r"\s+"))


def build_chunks(sections: list[tuple]) -> list[tuple[int, str, str]]:
    """(kind, locator, text) 목록을 (ordinal, locator, text) 조각으로 만든다.

    산문류는 목표 창에 찰 때까지 인접 섹션을 합친다. 그 밖의 종류(표·시트·
    슬라이드·노트)와, 이미 목표 상한을 넘는 섹션은 있는 그대로 하나의
    조각이 된다 — 다만 여전히 MAX_CHUNK_CHARS를 넘으면 줄·문장 경계에서 나눈다.
    """
    return [piece[:3] for piece in build_chunks_with_context(sections)]


def build_chunks_with_context(
    sections: list[tuple], *, target_min: int = TARGET_MIN_CHARS,
    target_max: int = TARGET_MAX_CHARS, max_chars: int = MAX_CHUNK_CHARS,
) -> list[tuple[int, str, str, str]]:
    """(kind, locator, text[, meta]) 목록을 (ordinal, locator, text, context) 조각으로 만든다.

    meta는 파서의 구조 정보다(사전이나 그 JSON, ingest/parsers/base.py Section.meta).
    context는 조각이 속한 제목들("제2장 예산 > 1. 집행")로, 찾는 데만 쓰고 근거로
    인용하지 않는다. 창 크기는 비교 실험(app/tools/window_eval.py)에서만 바꾼다.
    """
    chunks: list[tuple[int, str, str, str]] = []
    ordinal = 0
    buffer: list[tuple[str, str]] = []   # [(locator, text)], 같은 kind만 쌓인다
    buffer_kind: str | None = None
    buffer_context = ""
    buffer_len = 0

    def emit(locator: str, text: str, context: str) -> None:
        nonlocal ordinal
        for piece in _split_long(text, max_chars):
            if not piece.strip():
                continue
            ordinal += 1
            chunks.append((ordinal, locator, piece, context))

    def flush() -> None:
        nonlocal buffer, buffer_kind, buffer_len
        if not buffer:
            return
        if len(buffer) == 1:
            locator, text = buffer[0]
            emit(locator, text, buffer_context)
        else:
            merged_locator = _range_locator([loc for loc, _ in buffer])
            merged_text = "\n".join(text for _, text in buffer)
            emit(merged_locator, merged_text, buffer_context)
        buffer = []
        buffer_kind = None
        buffer_len = 0

    for section in sections:
        kind, locator, text = section[:3]
        if not text or not text.strip():
            continue
        body = text.strip()
        context = _context(section[3] if len(section) > 3 else None)

        if kind not in MERGEABLE_KINDS or len(body) >= target_max:
            flush()
            emit(locator, body, context)
            continue

        # 다른 종류와는 섞지 않는다. 제목이 바뀌면 새 조각을 시작한다 — 다만 지금까지
        # 모은 것이 너무 짧으면(제목 한 줄, 한두 문장) 다음 절과 합쳐 둔다. 짧은 조각의
        # 임베딩은 뜻을 담지 못한다.
        if buffer and (buffer_kind != kind
                       or (context != buffer_context and buffer_len >= target_min // 2)):
            flush()

        if not buffer:
            buffer_context = context
        buffer.append((locator, body))
        buffer_kind = kind
        buffer_len += len(body)
        # 불변식: 매 반복 시작 시 buffer_len < target_min이다(이 줄이
        # 그 조건을 유지한다). 그래서 buffer_len(<400) + body(<800)는
        # max_chars(1200)를 넘지 않는다 — flush()가 강제 분할을
        # 부를 일이 없다는 뜻이고, 그래서 병합 조각이 중간에서 잘리지 않는다.
        if buffer_len >= target_min:
            flush()

    flush()
    return chunks


def _context(meta) -> str:
    """구조 정보에서 조각의 제목 문맥을 읽는다. 읽을 수 없으면 빈 글이다."""
    if isinstance(meta, str):
        try:
            meta = json.loads(meta or "{}")
        except ValueError:
            return ""
    heading = meta.get("heading") if isinstance(meta, dict) else None
    if not isinstance(heading, list):
        return ""
    titles = [" ".join(str(title).split()) for title in heading if str(title).strip()]
    return CONTEXT_SEPARATOR.join(titles)[:CONTEXT_CHARS]


def _split_long(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """max_chars를 넘는 글을 줄 → 문장 → 낱말 경계 순으로 찾아 나눈다.

    글자 수로만 자르면 표의 행, 문장, '1,234,000원' 같은 값이 두 조각에
    걸친다. 창의 뒤쪽 절반에서 경계를 찾고, 없을 때만 글자 수로 자른다.
    조각을 이으면 원문과 같다 — 글자를 더하거나 빼지 않는다.
    """
    pieces: list[str] = []
    start = 0
    while len(text) - start > max_chars:
        window = text[start : start + max_chars]
        cut = 0
        for pattern in _SPLIT_BOUNDARIES:
            ends = [m.end() for m in pattern.finditer(window) if m.end() > max_chars // 2]
            if ends:
                cut = ends[-1]
                break
        cut = cut or max_chars
        pieces.append(window[:cut])
        start += cut
    pieces.append(text[start:])
    return pieces


def _range_locator(locators: list[str]) -> str:
    """['1문단', ..., '5문단'] → '1~5문단'. 숫자+단위 형식이 아니면 양끝만 잇는다."""
    first, last = locators[0], locators[-1]
    if first == last:
        return first
    head = _LOCATOR_NUMBER.match(first)
    tail = _LOCATOR_NUMBER.match(last)
    if head and tail and head.group(2) == tail.group(2):
        return f"{head.group(1)}~{tail.group(1)}{head.group(2)}"
    return f"{first}~{last}"
