"""그림에서 글자를 읽는다 — 이미지 파일과 글자 레이어가 없는 PDF(스캔본).

별도 OCR 엔진을 반입하지 않고 이 PC의 로컬 모델(그림을 읽을 수 있다고 밝힌
것)에 맡긴다. 그래서 결과는 **원문이 아니라 모델이 읽은 글**이다. 항상
'일부만 읽음'으로 표시하고 그 사실을 note에 남긴다 — 숫자 한 자리를 틀리게
읽어도 화면에서는 원문처럼 보이기 때문이다.

폴더 훑기(ingest/scanner.py)는 이 모듈을 쓰지 않는다. 전임자 폴더의 사진을
전부 문서로 읽어 들이면 건당 수십 초씩 걸린다. 사용자가 노트북에 직접 올린
파일만 읽는다(core/notebook_ingest.py).
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path

import pymupdf

from .parsers.base import EMPTY, FAILED, MAX_CHARS, PARTIAL, ParseResult, Section, clip, failed

PARSER = "vision"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp")
# 스캔본 한 권을 통째로 읽으면 몇십 분이 걸린다. 넘는 쪽은 읽지 않고 그 사실을 적는다.
MAX_PAGES = 20
# 긴 변 기준. 더 크게 보내도 모델이 줄여서 보고, 전송과 처리만 느려진다.
MAX_SIDE = 1600
NOTE = "로컬 AI가 그림에서 읽은 글자입니다. 숫자·이름은 원본과 대조하세요."
NOTHING = "글자 없음"

PROMPT = (
    "이 그림에 보이는 글자를 빠짐없이, 보이는 그대로 옮겨 적으세요.\n"
    "- 표는 행마다 한 줄로 적고 칸은 ' | '로 구분하세요.\n"
    "- 설명·요약·해석을 덧붙이지 마세요. 보이지 않는 글자를 추측해 채우지 마세요.\n"
    f"- 읽을 글자가 없으면 '{NOTHING}'이라고만 답하세요."
)


def is_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def render_pages(path: Path, limit: int = MAX_PAGES) -> tuple[list[tuple[str, bytes]], int]:
    """(위치, PNG) 목록과 전체 쪽수. 그림 파일은 한 쪽짜리 문서로 열린다."""
    pages: list[tuple[str, bytes]] = []
    with pymupdf.open(path) as document:
        total = document.page_count
        for index, page in enumerate(document, start=1):
            if index > limit:
                break
            longest = max(page.rect.width, page.rect.height) or 1
            # PDF는 72dpi 좌표라 그대로 찍으면 글자가 뭉개진다. 긴 변을 MAX_SIDE에 맞춘다.
            scale = MAX_SIDE / longest if path.suffix.lower() == ".pdf" else min(1.0, MAX_SIDE / longest)
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            pages.append((f"{index}쪽" if total > 1 else "그림", pixmap.tobytes("png")))
    return pages, total


def read(client, path: str | Path, should_stop: Callable[[], bool] = lambda: False) -> ParseResult:
    """그림을 모델에 보여 주고 읽은 글자를 파서 결과로 돌려준다. 예외를 던지지 않는다."""
    path = Path(path)
    if not client._supports("vision"):   # noqa: SLF001 — 같은 꾸러미의 기능 조회
        return ParseResult(status=EMPTY, parser=PARSER,
                           note="그림의 글자를 읽으려면 그림을 읽을 수 있는 로컬 모델이 필요합니다.")
    try:
        pages, total = render_pages(path)
    except Exception as exc:
        return failed(PARSER, f"그림 열기 실패: {exc}")

    budget = [MAX_CHARS]
    sections: list[Section] = []
    errors: list[str] = []
    for ordinal, (locator, png) in enumerate(pages, start=1):
        if should_stop():
            break
        text, error = client.generate_text(
            PROMPT, images=[base64.b64encode(png).decode("ascii")], num_predict=1600)
        if error:
            errors.append(f"{locator}: {error}")
            continue
        text = (text or "").strip()
        if not text or text.replace(".", "").strip() == NOTHING:
            continue
        body = clip(text, budget)
        if not body:
            break
        sections.append(Section("page", ordinal, locator, body))

    if not sections:
        if errors:
            return failed(PARSER, "; ".join(errors[:3]), FAILED)
        return ParseResult(status=EMPTY, parser=PARSER, note="그림에서 읽을 글자를 찾지 못했습니다.")
    note = NOTE
    if total > len(pages):
        note += f" 전체 {total}쪽 가운데 앞 {len(pages)}쪽만 읽었습니다."
    if errors:
        note += f" {len(errors)}쪽은 읽지 못했습니다."
    return ParseResult(status=PARTIAL, parser=PARSER, sections=sections, note=note)
