"""HWP 5.0 파서 (OLE 복합문서).

한컴오피스 설치나 외부 변환 도구에 의존하지 않는다. 폐쇄망 반입 대상이므로
의존성을 늘리지 않는 것이 중요하다.

구조
  FileHeader           256바이트. 서명 + 버전 + 속성 비트(압축/암호)
  BodyText/Section0..N 레코드 스트림. 속성 비트가 서면 raw deflate로 압축
  PrvText              미리보기 텍스트(UTF-16LE, 무압축). 본문 실패 시 폴백

레코드 헤더는 4바이트 리틀엔디언에 tag(10) / level(10) / size(12)가 담긴다.
size가 0xFFF이면 뒤따르는 4바이트가 실제 크기다.

표 안의 글자도 PARA_TEXT로 나오므로 텍스트는 얻지만 표 구조는 보존되지 않는다.
이 한계는 parse_report에서 그대로 드러난다.
"""

from __future__ import annotations

import zlib
from pathlib import Path

import olefile

from ._aes import decrypt_ecb
from .base import (
    EMPTY,
    ENCRYPTED,
    MAX_CHARS,
    OK,
    PARTIAL,
    UNSUPPORTED,
    DocMeta,
    ParseResult,
    Section,
    clip,
    failed,
)

PARSER = "hwp"

HWPTAG_BEGIN = 0x10
HWPTAG_PARA_TEXT = HWPTAG_BEGIN + 51  # 67
HWPTAG_DISTRIBUTE_DOC_DATA = HWPTAG_BEGIN + 12  # 28

# HWP 제어문자 분류 (한글 문서 파일 형식 5.0 기준)
CHAR_CTRL = {0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31}          # 1 wchar
INLINE_CTRL = {4, 5, 6, 7, 8, 9, 19, 20}                          # 8 wchar
EXTENDED_CTRL = {1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23}  # 8 wchar


def parse(path: Path) -> ParseResult:
    if not olefile.isOleFile(str(path)):
        head = path.open("rb").read(400)
        if head.startswith(b"HWP Document File V3"):
            return failed(PARSER, "HWP 3.0 형식 — 지원하지 않음", UNSUPPORTED)
        if head.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"<?xml") and b"HWPML" in head:
            return _parse_hwpml(path)
        if head.startswith(b"PK\x03\x04"):
            # 이름만 .hwp인 HWPX. 법제처·국가법령정보센터에서 받은 파일에 흔하다.
            from . import hwpx
            return hwpx.parse(path)
        return failed(PARSER, "OLE 복합문서가 아님", UNSUPPORTED)

    try:
        ole = olefile.OleFileIO(str(path))
    except Exception as exc:  # olefile은 다양한 예외를 던진다
        return failed(PARSER, f"OLE 열기 실패: {exc}")

    with ole:
        if not ole.exists("FileHeader"):
            return failed(PARSER, "FileHeader 없음", UNSUPPORTED)

        header = ole.openstream("FileHeader").read()
        if len(header) < 40 or not header.startswith(b"HWP Document File"):
            return failed(PARSER, "HWP 서명 불일치", UNSUPPORTED)

        props = int.from_bytes(header[36:40], "little")
        compressed = bool(props & 0x01)
        if props & 0x02:
            # 암호 문서는 해제를 시도하지 않는다 (PAR-006)
            return failed(PARSER, "암호로 보호된 문서", ENCRYPTED)

        meta = _read_summary(ole)
        # 배포용 문서는 본문이 ViewText에 암호화돼 있고, BodyText에는 "상위 버전의
        # 배포용 문서입니다…" 안내문만 있다. 안내문을 본문으로 읽으면 핵심 계획서가
        # 106자짜리 '읽은 문서'가 된다(실측 23건).
        distributed = bool(props & 0x04)
        folder = "ViewText" if distributed else "BodyText"
        streams = sorted(
            (e for e in ole.listdir() if len(e) == 2 and e[0] == folder),
            key=lambda e: e[1],
        )
        if distributed and not streams:
            return failed(PARSER, "배포용 문서 — 본문(ViewText)이 없음", UNSUPPORTED)

        budget = [MAX_CHARS]
        out: list[Section] = []
        ordinal = 0
        errors: list[str] = []
        visited = 0
        limited = False
        def scope(preview=False, preview_chars=0):
            omissions = []
            if errors:
                omissions.append({"reason": "body_stream_error", "count": len(errors)})
            if limited:
                omissions.append({"reason": "character_limit"})
            if preview:
                omissions.append({"reason": "preview_fallback_body_unavailable"})
            return {"version": 1, "scope": "hwp_preview" if preview else "hwp_paragraph_records",
                    "completeness": "unverified", "body_streams_total": len(streams),
                    "body_streams_visited": visited, "body_streams_failed": len(errors),
                    "body_streams_unvisited": len(streams) - visited,
                    "paragraphs_emitted": len(out),
                    "characters_emitted": preview_chars if preview else sum(len(s.text) for s in out),
                    "character_limit": MAX_CHARS, "preview_fallback": preview,
                    "omissions": omissions,
                    "limitations": ["table_structure_not_preserved", "record_completeness_not_verified",
                                    "images_and_layout_not_extracted"]}

        for entry in streams:
            if budget[0] <= 0:
                limited = True
                break
            visited += 1
            try:
                data = ole.openstream(entry).read()
                if distributed:
                    data = _open_distributed(data)
                if compressed:
                    data = zlib.decompressobj(-15).decompress(data)
            except Exception as exc:
                errors.append(f"{'/'.join(entry)}: {exc}")
                continue

            for tag, _level, payload in _records(data):
                if tag != HWPTAG_PARA_TEXT:
                    continue
                para = _decode_para(payload)
                if not para.strip():
                    continue
                body = clip(para, budget)
                if len(body) < len(para):
                    limited = True
                if not body:
                    break
                ordinal += 1
                out.append(Section("paragraph", ordinal, f"{ordinal}문단", body))

        if out:
            note = f"일부 스트림 실패: {'; '.join(errors)}" if errors else None
            if limited:
                note = (note + "; " if note else "") + "글자 상한으로 일부만 읽음"
            status = PARTIAL if errors or limited else OK
            return ParseResult(
                status=status, parser=PARSER, sections=out, meta=meta, note=note, coverage=scope()
            )

        if distributed:
            # 미리보기로 물러서지 않는다 — 배포용 문서의 미리보기도 안내문일 수 있다.
            result = failed(PARSER, "배포용 문서 — 본문을 풀지 못함"
                            + (f" ({'; '.join(errors)})" if errors else ""), UNSUPPORTED)
            result.meta, result.coverage = meta, scope()
            return result

        # 본문을 못 읽으면 미리보기 텍스트로 폴백한다.
        preview = _preview_text(ole)
        if preview:
            body = clip(preview, [MAX_CHARS])
            limited = len(body) < len(preview)
            return ParseResult(
                status=PARTIAL,
                parser=PARSER,
                sections=[Section("preview", 1, "미리보기", body)],
                meta=meta,
                note="본문 스트림 추출 실패 — PrvText 미리보기로 폴백",
                coverage=scope(preview=True, preview_chars=len(body)),
            )

    if errors:
        result = failed(PARSER, "; ".join(errors))
        result.coverage = scope()
        return result
    return ParseResult(status=EMPTY, parser=PARSER, meta=meta, coverage=scope())


def _parse_hwpml(path: Path) -> ParseResult:
    """HWPML(한글의 XML 저장 형식). 확장자는 .hwp지만 속은 XML이다.

    국가법령정보센터에서 내려받은 법령·조례가 이 형식이다(실측: 조례·운영규정·법
    조문 6건이 'OLE 복합문서가 아님'으로 빠졌다). 문단은 <P>, 글자는 <P>/<TEXT>/<CHAR>에
    있다. 표 안의 문단도 <P>라서 바로 아래의 글자만 모으면 겹치지 않는다.

    DOCTYPE은 떼고 읽는다 — 외부 개체를 풀 길을 열지 않는다.
    """
    import re

    from lxml import etree

    raw = path.read_bytes()
    raw = re.sub(rb"<!DOCTYPE[^>\[]*(\[[^\]]*\])?\s*>", b"", raw, count=1)
    raw = raw.replace(b"&nbsp;", b"&#160;")
    try:
        root = etree.fromstring(raw, etree.XMLParser(
            resolve_entities=False, no_network=True, recover=True, huge_tree=False))
    except etree.XMLSyntaxError as exc:
        return failed(PARSER, f"HWPML 구조 손상: {exc}")
    if root is None:
        return failed(PARSER, "HWPML 구조 손상")

    meta = DocMeta(title=_clean(root.findtext(".//DOCSUMMARY/TITLE")),
                   author=_clean(root.findtext(".//DOCSUMMARY/AUTHOR")))
    budget = [MAX_CHARS]
    out: list[Section] = []
    limited = False
    for paragraph in root.iter("P"):
        text = "".join(paragraph.xpath("./TEXT/CHAR/text()")).replace("\xa0", " ").strip()
        if not text:
            continue
        body = clip(text, budget)
        if len(body) < len(text):
            limited = True
        if not body:
            break
        out.append(Section("paragraph", len(out) + 1, f"{len(out) + 1}문단", body))
    scope = {"version": 1, "scope": "hwpml_paragraphs", "completeness": "unverified",
             "paragraphs_emitted": len(out), "characters_emitted": sum(len(s.text) for s in out),
             "limitations": ["table_structure_not_preserved", "images_and_layout_not_extracted"]}
    if not out:
        return ParseResult(status=EMPTY, parser=PARSER, meta=meta, coverage=scope)
    return ParseResult(status=PARTIAL if limited else OK, parser=PARSER, sections=out, meta=meta,
                       note="글자 상한으로 일부만 읽음" if limited else None, coverage=scope)


def _open_distributed(data: bytes) -> bytes:
    """배포용 문서의 ViewText 스트림을 푼다.

    스트림 맨 앞 레코드(HWPTAG_DISTRIBUTE_DOC_DATA, 256바이트)에 키가 들어 있다.
    앞 4바이트를 씨앗으로 MSVC rand()를 돌려 나머지를 XOR로 풀면, 그 안의
    한 자리(4 + 씨앗 아래 4비트)부터 16바이트가 AES-128 키다. 레코드 뒤의
    나머지가 ECB로 암호화된 본문이다.

    암호 문서(속성 비트 0x02)와 다르다. 배포용 문서는 암호를 묻지 않고 누구나
    열어 읽는 문서이고, 막는 것은 편집·복사다. 이 프로그램은 읽기만 한다.
    """
    header = next(_records(data), None)
    if header is None or header[0] != HWPTAG_DISTRIBUTE_DOC_DATA or len(header[2]) != 256:
        raise ValueError("배포용 문서 키 레코드가 없음")
    payload = bytearray(header[2])
    seed = state = int.from_bytes(payload[:4], "little")

    def rand() -> int:
        nonlocal state
        state = (state * 214013 + 2531011) & 0xFFFFFFFF
        return (state >> 16) & 0x7FFF

    left = key = 0
    for index in range(256):
        if left == 0:
            key = rand() & 0xFF
            left = (rand() & 0x0F) + 1
        if index >= 4:
            payload[index] ^= key
        left -= 1
    offset = 4 + (seed & 0x0F)
    return decrypt_ecb(bytes(payload[offset:offset + 16]), data[4 + 256:])


def _records(buf: bytes):
    """레코드 스트림을 (tag, level, payload)로 순회한다."""
    pos, size = 0, len(buf)
    while pos + 4 <= size:
        header = int.from_bytes(buf[pos : pos + 4], "little")
        pos += 4
        tag = header & 0x3FF
        level = (header >> 10) & 0x3FF
        length = (header >> 20) & 0xFFF
        if length == 0xFFF:
            if pos + 4 > size:
                return
            length = int.from_bytes(buf[pos : pos + 4], "little")
            pos += 4
        if pos + length > size:
            return
        yield tag, level, buf[pos : pos + length]
        pos += length


def _decode_para(data: bytes) -> str:
    """PARA_TEXT 레코드를 UTF-16LE로 읽고 제어문자를 걷어낸다."""
    out: list[str] = []
    i, end = 0, len(data) - 1
    while i < end:
        code = int.from_bytes(data[i : i + 2], "little")
        if code in CHAR_CTRL:
            if code in (10, 13):
                out.append("\n")
            i += 2
        elif code in INLINE_CTRL or code in EXTENDED_CTRL:
            i += 16  # 제어문자 1 + 정보 6 + 제어문자 1 = 8 wchar
        elif 0xD800 <= code <= 0xDFFF:
            i += 2  # 단독 서로게이트는 버린다
        else:
            out.append(chr(code))
            i += 2
    return "".join(out)


def _preview_text(ole: olefile.OleFileIO) -> str:
    if not ole.exists("PrvText"):
        return ""
    try:
        return ole.openstream("PrvText").read().decode("utf-16-le", errors="replace").strip()
    except Exception:
        return ""


def _read_summary(ole: olefile.OleFileIO) -> DocMeta:
    """\x05HwpSummaryInformation에서 제목·작성자·작성일을 취한다."""
    meta = DocMeta()
    try:
        props = ole.getproperties("\x05HwpSummaryInformation", convert_time=True)
    except Exception:
        return meta
    # 표준 SummaryInformation 프로퍼티 ID
    meta.title = _clean(props.get(2))
    meta.author = _clean(props.get(4))
    meta.created = _clean(props.get(12))
    meta.modified = _clean(props.get(13))
    return meta


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
