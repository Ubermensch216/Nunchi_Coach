"""그림에서 글자를 읽는다 — 이미지 파일과 글자 레이어가 없는 PDF(스캔본).

별도 OCR 엔진을 반입하지 않고 이 PC의 로컬 모델(그림을 읽을 수 있다고 밝힌
것)에 맡긴다. 그래서 결과는 **원문이 아니라 모델이 읽은 글**이다. 항상
'일부만 읽음'으로 표시하고 그 사실을 note에 남긴다 — 숫자 한 자리를 틀리게
읽어도 화면에서는 원문처럼 보이기 때문이다.

폴더 훑기(ingest/scanner.py)는 이 모듈을 쓰지 않는다. 전임자 폴더의 사진을
전부 문서로 읽어 들이면 건당 수십 초씩 걸린다. 읽는 것은 두 곳뿐이다.
  · 사용자가 노트북에 직접 올린 파일(core/notebook_ingest.py)
  · 자료 폴더에서 골라낸 스캔본과 화면 캡처(core/pictures.py) — 한 번에 정해진 쪽수만

작은 그림은 키워서 보낸다. 화면 캡처(가로 900픽셀 안팎, 작은 글씨)를 그대로 보내면
모델이 없는 글자를 지어낸다 — 실측에서 공문 목록 캡처의 제목이 전부 엉뚱한 말로
나왔고, 2배로 키워 보내자 제대로 읽혔다.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path

import pymupdf

from . import winocr
from .parsers.base import EMPTY, FAILED, MAX_CHARS, PARTIAL, ParseResult, Section, clip, failed

PARSER = "vision"

# 그림을 찍을 때도 빠진 XObject 자원마다 MuPDF가 콘솔에 오류를 쏟는다(pdf.py 참고).
pymupdf.TOOLS.mupdf_display_errors(False)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp")
# 스캔본 한 권을 통째로 읽으면 몇십 분이 걸린다. 넘는 쪽은 읽지 않고 그 사실을 적는다.
MAX_PAGES = 20
# 긴 변 기준. 더 크게 보내도 모델이 줄여서 보고, 전송과 처리만 느려진다.
MAX_SIDE = 1600
# 작은 그림을 키우는 한도. 이보다 키우면 글씨가 뭉개질 뿐 더 읽히지 않는다.
# 쪽 크기(page.rect)에 곱하는 값이다. 그림 파일의 쪽 크기는 포인트로 환산돼 있어
# (96dpi 그림이면 픽셀의 0.75배) 픽셀로는 2배 가까이 커진다. 배율을 1로 묶어 두었던
# 예전에는 그림을 원본보다 작게 보내고 있었다.
MAX_UPSCALE = 2.5
NOTE ="로컬 AI가 그림에서 읽은 글자입니다. 숫자·이름은 원본과 대조하세요."
NOTE_RECOGNIZER = "Windows 글자 인식으로 그림에서 읽은 글자입니다. 숫자·이름은 원본과 대조하세요."
NOTHING = "글자 없음"
BLANK_NOTE = "그림에서 읽을 글자를 찾지 못했습니다."
# 모델이 답은 했지만 글이 아니었다(빈 표, 숫자 격자). 글자 없는 그림과는 다른 일이다 —
# 그림을 못 읽는 모델·실행 환경에서는 읽을 수 있는 캡처에도 이런 답이 온다(실측: 쪽지
# 캡처에 '| 1 | 2 | 3 |' 격자). 그래서 따로 적고, 연달아 나오면 점검 그림으로 확인한다.
GARBLED_NOTE = "로컬 AI가 이 그림을 글로 읽지 못했습니다."
# '읽어 봤지만 글자가 없었다'는 표시의 판. 바뀌면 그렇게 표시된 문서를 다시 읽는다.
# 2: 못 읽는 모델의 답을 '글자 없음'으로 굳히던 것을 되돌린다.
READER_RULES = "2"
# 점검 그림에 적는 글과, 읽은 글에서 찾을 낱말. 낱말 둘 이상이 나오면 읽는 것으로 본다.
CANARY_LINES = ("문서번호 2024-0731", "적극행정위원회 개최 계획", "제출기한은 10월 18일까지")
CANARY_WORDS = ("문서번호", "위원회", "계획", "제출기한")

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
            scale = (MAX_SIDE / longest if path.suffix.lower() == ".pdf"
                     else min(MAX_UPSCALE, MAX_SIDE / longest))
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            pages.append((f"{index}쪽" if total > 1 else "그림", pixmap.tobytes("png")))
    return pages, total


def reader_mark(client) -> str:
    """누가 읽었는가. '글자 없음' 표시는 이 값이 같을 때만 믿는다 — 모델을 바꾸면 다시 읽는다."""
    if winocr.available():
        return f"{READER_RULES}:{winocr.ENGINE}"
    return f"{READER_RULES}:{getattr(client, 'gen_model', '')}"


def canary_png() -> bytes:
    """정답을 아는 점검 그림. 큰 글씨 세 줄이라 그림을 읽는 모델이면 틀릴 수 없다."""
    with pymupdf.open() as document:
        page = document.new_page(width=640, height=240)
        for index, line in enumerate(CANARY_LINES):
            page.insert_text((40, 70 + index * 60), line, fontsize=30, fontname="korea")
        return page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).tobytes("png")


def selfcheck(client) -> tuple[bool, str]:
    """이 PC의 모델이 그림의 글자를 실제로 읽는가. (읽음, 사유).

    모델이 '그림을 읽을 수 있다'고 밝혀도 실행 환경에 따라 읽지 못한다(실측: 같은 모델이
    한 PC에서는 캡처를 읽고 다른 PC에서는 빈 표만 냈다). 못 읽어도 답은 오므로 문서를
    읽혀 봐서는 고장을 알 수 없다. 정답을 아는 그림을 먼저 읽힌다.
    """
    if not client._supports("vision"):   # noqa: SLF001
        return False, "그림을 읽을 수 있는 로컬 모델이 없습니다"
    try:
        png = canary_png()
    except Exception as exc:
        return False, f"점검 그림을 만들지 못했습니다: {exc}"
    text, error = client.generate_text(
        PROMPT, images=[base64.b64encode(png).decode("ascii")], num_predict=200)
    if error:
        return False, error
    packed = "".join((text or "").split())
    if sum(1 for word in CANARY_WORDS if word in packed) >= 2:
        return True, ""
    return False, "점검 그림의 글자를 읽지 못했습니다"


def _has_words(text: str) -> bool:
    """글이라고 할 만한가. 손글씨 서명부 같은 그림에서 모델은 빈 칸마다 숫자와 줄만
    되풀이해 적는다(실측: 참석자 명부). 그런 것을 본문으로 남기지 않는다."""
    solid = [ch for ch in text if not ch.isspace()]
    letters = sum(1 for ch in solid if "가" <= ch <= "힣" or ch.isalpha())
    return letters >= 2 and letters >= len(solid) * 0.2


def _read_with_recognizer(path: Path, should_stop, max_pages: int) -> ParseResult | None:
    """Windows 글자 인식으로 읽는다. 인식기를 부르지 못했으면 None(로컬 모델로 넘어간다)."""
    try:
        pages, total = render_pages(path, max_pages)
    except Exception as exc:
        return failed(PARSER, f"그림 열기 실패: {exc}")
    if should_stop():
        return ParseResult(status=EMPTY, parser=PARSER, note=BLANK_NOTE)
    texts = winocr.read_pngs([png for _locator, png in pages])
    if texts is None:
        return None
    budget = [MAX_CHARS]
    sections: list[Section] = []
    for ordinal, ((locator, _png), text) in enumerate(zip(pages, texts), start=1):
        text = (text or "").strip()
        if not text or not _has_words(text):
            continue
        body = clip(text, budget)
        if not body:
            break
        sections.append(Section("page", ordinal, locator, body, {"ocr": True, "engine": winocr.ENGINE}))
    if not sections:
        return ParseResult(status=EMPTY, parser=PARSER, note=BLANK_NOTE)
    note = NOTE_RECOGNIZER
    if total > len(pages):
        note += f" 전체 {total}쪽 가운데 앞 {len(pages)}쪽만 읽었습니다."
    return ParseResult(status=PARTIAL, parser=PARSER, sections=sections, note=note)


def read(client, path: str | Path, should_stop: Callable[[], bool] = lambda: False,
         max_pages: int = MAX_PAGES) -> ParseResult:
    """그림에서 읽은 글자를 파서 결과로 돌려준다. 예외를 던지지 않는다.

    Windows의 글자 인식을 쓸 수 있으면 그것으로 읽는다(쪽당 1초 안쪽, 모델이 필요 없다).
    쓸 수 없을 때만 로컬 모델에 그림을 보여 준다.
    """
    path = Path(path)
    if winocr.available():
        recognized = _read_with_recognizer(path, should_stop, max_pages)
        if recognized is not None:
            return recognized
    if not client._supports("vision"):   # noqa: SLF001 — 같은 꾸러미의 기능 조회
        return ParseResult(status=EMPTY, parser=PARSER,
                           note="그림의 글자를 읽으려면 그림을 읽을 수 있는 로컬 모델이 필요합니다.")
    try:
        pages, total = render_pages(path, max_pages)
    except Exception as exc:
        return failed(PARSER, f"그림 열기 실패: {exc}")

    budget = [MAX_CHARS]
    sections: list[Section] = []
    errors: list[str] = []
    garbled = 0
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
        if not _has_words(text):
            garbled += 1
            continue
        body = clip(text, budget)
        if not body:
            break
        sections.append(Section("page", ordinal, locator, body, {"ocr": True}))

    if not sections:
        if errors:
            return failed(PARSER, "; ".join(errors[:3]), FAILED)
        return ParseResult(status=EMPTY, parser=PARSER, note=GARBLED_NOTE if garbled else BLANK_NOTE)
    note = NOTE
    if total > len(pages):
        note += f" 전체 {total}쪽 가운데 앞 {len(pages)}쪽만 읽었습니다."
    if errors:
        note += f" {len(errors)}쪽은 읽지 못했습니다."
    return ParseResult(status=PARTIAL, parser=PARSER, sections=sections, note=note)
