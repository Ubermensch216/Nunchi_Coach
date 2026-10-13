"""PDF 파서 (PyMuPDF).

텍스트 레이어가 없는 스캔본을 구분해 OCR 대기 대상으로 표시한다(PAR-004).
MVP에서 OCR은 범위 밖이므로 여기서는 '표시'까지만 한다.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from .base import (
    EMPTY,
    ENCRYPTED,
    MAX_CHARS,
    OK,
    PARTIAL,
    DocMeta,
    ParseResult,
    Section,
    clip,
    failed,
)

PARSER = "pdf"

# 원본 PDF가 쪽 자원 목록에 없는 그림(XObject)을 가리키는 일이 흔하다. MuPDF는 그때마다
# 콘솔에 "cannot find XObject resource"를 쏟지만 글자 추출에는 영향이 없다. 진짜 실패는
# 예외로 올라오고 아래에서 결과에 담기므로 콘솔 출력만 끈다.
pymupdf.TOOLS.mupdf_display_errors(False)

# 쪽당 이 글자 수에 못 미치면 텍스트 레이어가 없다고 본다.
TEXT_LAYER_MIN_CHARS_PER_PAGE = 20


def parse(path: Path) -> ParseResult:
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        return failed(PARSER, f"PDF 열기 실패: {exc}")

    with doc:
        if doc.needs_pass:
            return failed(PARSER, "암호로 보호된 PDF", ENCRYPTED)

        meta = _read_meta(doc)
        budget = [MAX_CHARS]
        out: list[Section] = []
        total = doc.page_count
        visited = 0
        counts = {"textless_page": 0, "page_read_error": 0, "character_limit": 0}
        samples = {key: [] for key in counts}
        # Keep diagnostics bounded even for pathological multi-page inputs.
        def omitted(reason, page):
            counts[reason] += 1
            if len(samples[reason]) < 100:
                samples[reason].append(page)

        for index, page in enumerate(doc, start=1):
            if budget[0] <= 0:
                break
            visited += 1
            try:
                text = page.get_text("text").strip()
            except Exception:
                omitted("page_read_error", index)
                continue
            if not text:
                omitted("textless_page", index)
                continue
            body = clip(text, budget)
            if len(body) < len(text):
                omitted("character_limit", index)
            if body:
                out.append(Section("page", index, f"{index}쪽", body))

    unvisited = total - visited
    omissions = [{"reason": reason, "count": n, "unit": "page",
                  "page_samples": samples[reason], "samples_complete": n <= 100}
                 for reason, n in counts.items() if n]
    if unvisited:
        omissions.append({"reason": "character_limit", "count": unvisited,
                          "unit": "unvisited_page", "page_range": [visited + 1, total]})
    chars = sum(len(s.text) for s in out)
    coverage = {"version": 1, "scope": "pdf_text_layer", "completeness": "unverified",
                "pages_total": total, "pages_visited": visited, "pages_with_text": len(out),
                "pages_textless": counts["textless_page"], "pages_failed": counts["page_read_error"],
                "pages_unvisited": unvisited, "characters_emitted": chars,
                "character_limit": MAX_CHARS, "omissions": omissions,
                "limitations": ["images_and_visual_tables_not_verified", "ocr_not_performed"]}
    notes = []
    if counts["textless_page"]:
        notes.append(f"{counts['textless_page']}쪽에서 텍스트를 얻지 못함 — OCR 필요 여부 확인")
    if counts["page_read_error"]:
        notes.append(f"{counts['page_read_error']}쪽 읽기 실패")
    if counts["character_limit"] or unvisited:
        notes.append(f"글자 상한으로 일부만 읽음 (미방문 {unvisited}쪽)")
    if not out:
        return ParseResult(status=EMPTY, parser=PARSER, meta=meta, coverage=coverage,
                           note="; ".join(notes) or "텍스트 레이어 없음 — OCR 필요")
    weak = bool(total and chars / total < TEXT_LAYER_MIN_CHARS_PER_PAGE)
    if weak:
        notes.append(f"텍스트 레이어 빈약({chars}자/{total}쪽) — 스캔본 가능성, OCR 필요")
    return ParseResult(status=PARTIAL if omissions or weak else OK, parser=PARSER,
                       sections=out, meta=meta, coverage=coverage, note="; ".join(notes) or None)


def _read_meta(doc) -> DocMeta:
    raw = doc.metadata or {}
    return DocMeta(
        title=(raw.get("title") or "").strip() or None,
        author=(raw.get("author") or "").strip() or None,
        created=(raw.get("creationDate") or "").strip() or None,
        modified=(raw.get("modDate") or "").strip() or None,
    )
