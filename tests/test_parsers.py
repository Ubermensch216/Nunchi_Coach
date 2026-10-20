"""파서 계약 시험.

HWP 5.0은 합성이 어려워 실파일 표본이 없다. 대신 컨테이너(OLE) 밖의 순수
로직 — 레코드 스트림 파싱과 제어문자 디코딩 — 을 합성 바이트로 검증한다.
컨테이너 부분은 실제 문서로 별도 확인이 필요하다.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.ingest.parsers import parse, supported_extensions
from app.ingest.parsers.base import ENCRYPTED, OK, PARTIAL, UNSUPPORTED
from app.ingest.parsers.hwp import HWPTAG_PARA_TEXT, _decode_para, _records

FIXTURES = Path(__file__).parent / "fixtures" / "sample_tree"


# ── HWP 레코드 스트림 ────────────────────────────────────────────────

def _record(tag: int, level: int, payload: bytes) -> bytes:
    """HWP 레코드 헤더 4바이트: tag(10) | level(10) | size(12)."""
    size = len(payload)
    if size >= 0xFFF:
        header = tag | (level << 10) | (0xFFF << 20)
        return header.to_bytes(4, "little") + size.to_bytes(4, "little") + payload
    header = tag | (level << 10) | (size << 20)
    return header.to_bytes(4, "little") + payload


def test_records_reads_tag_level_and_payload():
    stream = _record(HWPTAG_PARA_TEXT, 1, b"ABCD") + _record(99, 2, b"XY")
    got = list(_records(stream))
    assert got == [(HWPTAG_PARA_TEXT, 1, b"ABCD"), (99, 2, b"XY")]


def test_records_handles_extended_size_marker():
    """size가 0xFFF이면 뒤따르는 4바이트가 실제 크기다."""
    payload = b"Z" * 5000
    got = list(_records(_record(HWPTAG_PARA_TEXT, 0, payload)))
    assert got == [(HWPTAG_PARA_TEXT, 0, payload)]


def test_records_stops_on_truncated_stream():
    """잘린 스트림에서 무한 루프나 예외 없이 멈춰야 한다."""
    stream = _record(HWPTAG_PARA_TEXT, 0, b"OK!!")
    truncated = stream[:-2]  # payload 일부 소실
    assert list(_records(truncated)) == []
    assert list(_records(b"\x01\x02")) == []  # 헤더도 안 되는 길이


# ── HWP 문단 텍스트 디코딩 ──────────────────────────────────────────

def _wchars(*codes: int) -> bytes:
    return b"".join(c.to_bytes(2, "little") for c in codes)


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-le")


def test_decode_para_reads_korean_text():
    assert _decode_para(_utf16("행정사무감사 제출자료")) == "행정사무감사 제출자료"


def test_decode_para_turns_para_break_into_newline():
    data = _utf16("첫째줄") + _wchars(13) + _utf16("둘째줄")
    assert _decode_para(data) == "첫째줄\n둘째줄"


def test_decode_para_skips_extended_control_block():
    """확장 제어문자는 8 wchar(16바이트)를 차지한다. 그 안의 값이 본문으로
    새어 나오면 안 된다."""
    control = _wchars(2) + _wchars(0x0041) * 6 + _wchars(2)  # 총 8 wchar
    assert len(control) == 16
    data = _utf16("앞") + control + _utf16("뒤")
    assert _decode_para(data) == "앞뒤"


def test_decode_para_skips_inline_control_block():
    control = _wchars(9) + _wchars(0x0042) * 6 + _wchars(9)
    data = _utf16("가") + control + _utf16("나")
    assert _decode_para(data) == "가나"


def test_decode_para_drops_lone_surrogates():
    data = _utf16("정상") + _wchars(0xD800) + _utf16("문자")
    assert _decode_para(data) == "정상문자"


def test_decode_para_on_odd_length_does_not_raise():
    assert _decode_para(_utf16("가나다")[:-1]) == "가나"


# ── 디스패처 계약 ────────────────────────────────────────────────────

def test_parse_never_raises_on_garbage(tmp_path: Path):
    """어떤 쓰레기 입력에도 예외 대신 ParseResult가 나와야 한다 (ING-007)."""
    cases = {
        "broken.hwp": b"not an ole file at all",
        "broken.hwpx": b"PK\x03\x04 truncated garbage",
        "broken.pdf": b"%PDF-1.4 but not really",
        "broken.xlsx": b"\x00\x01\x02",
        "broken.docx": b"",
        "broken.pptx": b"random",
    }
    for name, blob in cases.items():
        path = tmp_path / name
        path.write_bytes(blob)
        result = parse(path)
        assert result.status != OK, name
        assert result.error, name


def test_parse_rejects_unknown_extension(tmp_path: Path):
    path = tmp_path / "sample.exe"
    path.write_bytes(b"MZ")
    assert parse(path).status == UNSUPPORTED


def test_parse_reports_legacy_hwp3_as_unsupported(tmp_path: Path):
    path = tmp_path / "old.hwp"
    path.write_bytes(b"HWP Document File V3.00 \x1a" + b"\x00" * 64)
    result = parse(path)
    assert result.status == UNSUPPORTED
    assert "3.0" in (result.error or "")


def test_text_parser_reads_cp949(tmp_path: Path):
    """옛 공직 자료에는 CP949 텍스트가 흔하다."""
    path = tmp_path / "old.txt"
    path.write_bytes("2024년 수질통계 보고".encode("cp949"))
    result = parse(path)
    assert result.status == OK
    assert "수질통계" in result.text


def test_every_registered_extension_is_dispatchable():
    assert ".hwp" in supported_extensions()
    assert ".hwpx" in supported_extensions()


# ── 표본 트리 (있을 때만) ────────────────────────────────────────────

needs_fixtures = pytest.mark.skipif(
    not FIXTURES.is_dir(),
    reason="python -m app.tools.make_fixtures 로 표본을 먼저 생성하세요",
)


@needs_fixtures
def test_fixture_tree_parses_without_failure():
    failures = []
    for path in FIXTURES.rglob("*"):
        if not path.is_file():
            continue
        result = parse(path)
        if not result.ok:
            failures.append((path.name, result.status, result.error))
    assert not failures, failures


@needs_fixtures
def test_sections_always_carry_a_locator():
    """근거 위치가 없는 조각이 있으면 나중에 출처를 붙일 수 없다 (PAR-002)."""
    for path in FIXTURES.rglob("*.xlsx"):
        result = parse(path)
        assert result.sections
        for section in result.sections:
            assert section.locator, (path.name, section.ordinal)
            assert section.kind


@needs_fixtures
def test_xlsx_locator_contains_sheet_and_range():
    path = next(FIXTURES.rglob("*.xlsx"))
    result = parse(path)
    locators = [s.locator for s in result.sections]
    assert any("'" in loc and ":" in loc for loc in locators), locators


@needs_fixtures
def test_parsing_does_not_modify_originals():
    """원본 무변경은 제품 원칙 1이다. 파싱만으로 바이트가 바뀌면 안 된다."""
    targets = [p for p in FIXTURES.rglob("*") if p.is_file()][:40]
    before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in targets}
    for path in targets:
        parse(path)
    after = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in targets}
    assert before == after


def test_xlsx_long_sheet_breaks_between_rows_and_repeats_the_header(tmp_path):
    """실측: 한 행이 250자쯤인 시트가 글자 수로 잘려 조각이 행 중간('ight fluorescent…')에서 시작했다."""
    import openpyxl

    from app.core.chunking import MAX_CHUNK_CHARS, build_chunks

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "테마"
    sheet.append(["작사용 테마 목록"])
    sheet.append(["번호", "테마명", "키워드"])
    for number in range(1, 21):
        sheet.append([number, f"테마{number}", "낱말 " * 50])
    path = tmp_path / "목록.xlsx"
    book.save(path)

    result = parse(path)
    assert result.status == OK and len(result.sections) > 1
    chunks = build_chunks([(s.kind, s.locator, s.text) for s in result.sections])
    assert len(chunks) == len(result.sections)              # 글자 수로 다시 잘리지 않는다
    assert len({locator for _n, locator, _text in chunks}) == len(chunks)
    seen = []
    for _n, locator, text in chunks:
        assert len(text) <= MAX_CHUNK_CHARS and locator.startswith("'테마' A")
        lines = text.split("\n")
        assert lines[0] == "[테마]" and "번호\t테마명\t키워드" in lines[:3]
        rows = [line for line in lines if line.split("\t")[0].isdigit()]
        assert all(row.endswith("낱말") for row in rows)     # 행이 중간에서 끊기지 않았다
        seen += [row.split("\t")[0] for row in rows]
    assert seen == [str(number) for number in range(1, 21)]


def test_only_documents_of_a_revised_parser_are_read_again():
    import sqlite3

    from app.ingest.parsers.base import PARSER_VERSION, ParseResult, outdated_clause, version_of

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE documents (name TEXT, parser TEXT, parser_version TEXT)")
    con.executemany("INSERT INTO documents VALUES (?,?,?)", [
        ("옛 엑셀", "xlsx", PARSER_VERSION), ("새 엑셀", "xlsx", version_of("xlsx")),
        ("PDF", "pdf", version_of("pdf")), ("옛 PDF", "pdf", "1"), ("판본 없음", None, None)])
    stale = [row[0] for row in con.execute(f"SELECT name FROM documents d WHERE {outdated_clause('d')}")]
    assert stale == ["옛 엑셀", "옛 PDF", "판본 없음"]
    assert ParseResult(OK, "xlsx").parser_version == version_of("xlsx") != PARSER_VERSION
    assert ParseResult(OK, "pdf").parser_version == version_of("pdf")


# ── DOCX 구조 보존 (업무노트 RAG 2단계) ──────────────────────────────

def _structured_docx(path, rows):
    import docx
    document = docx.Document()
    document.add_heading("제2장 예산", level=1)
    document.add_heading("1. 집행 현황", level=2)
    document.add_paragraph("센터별 교부액과 집행액은 다음과 같다.")
    table = document.add_table(rows=len(rows), cols=3)
    for line, cells in zip(table.rows, rows):
        for cell, value in zip(line.cells, cells):
            cell.text = value
    document.add_heading("2. 반납", level=2)
    document.add_paragraph("집행잔액은 기한 안에 반납한다.")
    document.save(str(path))


def test_docx_keeps_the_heading_path_apart_from_the_body(tmp_path):
    from app.ingest.parsers import docx as docx_parser
    path = tmp_path / "정산.docx"
    _structured_docx(path, [("센터", "교부액", "집행액"), ("새봄", "31,500", "30,400")])
    sections = docx_parser.parse(path).sections
    by_text = {s.text: s for s in sections}
    assert by_text["제2장 예산"].meta["heading"] == ["제2장 예산"]
    assert by_text["센터별 교부액과 집행액은 다음과 같다."].meta["heading"] == ["제2장 예산", "1. 집행 현황"]
    # 같은 수준의 다음 제목이 앞 제목을 밀어낸다.
    assert by_text["집행잔액은 기한 안에 반납한다."].meta["heading"] == ["제2장 예산", "2. 반납"]
    table = next(s for s in sections if s.kind == "table")
    assert table.locator == "표1" and table.text == "센터\t교부액\t집행액\n새봄\t31,500\t30,400"
    assert table.meta == {"heading": ["제2장 예산", "1. 집행 현황"], "table": 1, "rows": [1, 2],
                          "header": "센터\t교부액\t집행액"}
    assert all("제2장" not in s.text for s in sections if s.kind == "table")   # 제목을 본문에 섞지 않는다


def test_long_docx_table_is_split_between_rows_and_every_piece_carries_the_header(tmp_path):
    """실측한 문제: 표 하나가 통째로 한 조각이라 1,200자에서 잘렸고, 둘째 조각부터 열 이름이 없었다."""
    from app.core.chunking import MAX_CHUNK_CHARS, build_chunks
    from app.ingest.parsers import docx as docx_parser
    rows = [("센터", "교부액(천원)", "집행액(천원)")] + [
        (f"{n}번 지역아동센터", f"{30000 + n * 1500:,}", f"{29000 + n * 1400:,}") for n in range(1, 121)]
    path = tmp_path / "정산.docx"
    _structured_docx(path, rows)
    pieces = [s for s in docx_parser.parse(path).sections if s.kind == "table"]
    assert len(pieces) > 2
    header = "\t".join(rows[0])
    lines = ["\t".join(row) for row in rows]
    covered = []
    for piece in pieces:
        first, last = piece.meta["rows"]
        assert piece.locator == f"표1 · {first}~{last}행" and piece.meta["header"] == header
        body = piece.text.splitlines()
        assert body[0] == header and len(piece.text) <= MAX_CHUNK_CHARS
        assert (body if first == 1 else body[1:]) == lines[first - 1:last]
        covered += range(first, last + 1)
    assert covered == list(range(1, len(rows) + 1))
    # 조각 단계에서 다시 잘리지 않는다.
    assert len(build_chunks([(s.kind, s.locator, s.text) for s in pieces])) == len(pieces)


def test_spreadsheet_and_ocr_sections_say_what_they_are(tmp_path):
    import openpyxl
    from app.ingest.parsers import xlsx as xlsx_parser
    path = tmp_path / "집계.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "집계"
    sheet.append(["항목", "금액"])
    sheet.append(["인건비", 1200])
    book.save(path)
    section, = xlsx_parser.parse(path).sections
    assert section.meta == {"sheet": "집계", "rows": [1, 2], "header": "항목\t금액"}


def test_text_parser_reads_utf16_with_and_without_bom(tmp_path: Path):
    text = "2024년 안전점검 계획\n\n작성일: 2024. 10. 14."
    with_bom = tmp_path / "bom.txt"
    with_bom.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    without_bom = tmp_path / "nobom.txt"
    without_bom.write_bytes(text.encode("utf-16-le"))
    for path in (with_bom, without_bom):
        result = parse(path)
        assert result.ok and "안전점검 계획" in result.text and "\x00" not in result.text
