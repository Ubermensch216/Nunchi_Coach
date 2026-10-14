"""읽지 못하던 자료를 읽는다 — 구형 XLS, 보호(DRM) 문서, 압축 안의 문서 (규제개혁 비교연구 R08, P1-2).

실제 자료에서 구형 XLS 9건이 '지원하지 않음', DRM 문서 3건이 '형식 오류', 압축 15개는 이름만
읽혔다. 읽지 못한 이유를 바르게 말하는 것과, 읽을 수 있는 것은 읽는 것을 함께 본다.
"""

from __future__ import annotations

import io
import struct
import zipfile
from datetime import date, datetime
from pathlib import Path

import pytest

from app.core import initial_analysis, reconstruction as rc
from app.db import Database
from app.ingest import parsers
from app.ingest.parsers import archive, xls, xlsx
from app.ingest.parsers.base import ENCRYPTED, FAILED, OK, PARTIAL


# ── 구형 XLS (BIFF8) ───────────────────────────────────────────────

def _rec(kind: int, body: bytes) -> bytes:
    return struct.pack("<HH", kind, len(body)) + body


def _unicode(text: str, high: bool = True, size: int = 2) -> bytes:
    raw = text.encode("utf-16-le") if high else text.encode("latin-1")
    return len(text).to_bytes(size, "little") + bytes([1 if high else 0]) + raw


def _workbook(encrypted: bool = False) -> bytes:
    """시트 하나짜리 Workbook 스트림. 한글 공유 문자열이 CONTINUE 경계에서 갈린다."""
    korean = "가나다라마".encode("utf-16-le")
    sst = _rec(xls.SST, struct.pack("<II", 3, 2) + b"\x05\x00\x01" + korean[:6])
    sst += _rec(xls.CONTINUE, b"\x01" + korean[6:] + _unicode("abc", high=False))
    globals_ = _rec(xls.BOF, b"\x00\x06\x05\x00" + b"\x00" * 12)
    if encrypted:
        globals_ += _rec(xls.FILEPASS, b"\x01\x00")
    globals_ += _rec(xls.FORMAT, struct.pack("<H", 164) + _unicode("yyyy\"년\" m\"월\" d\"일\""))
    globals_ += _rec(xls.XF, struct.pack("<HH", 0, 0) + b"\x00" * 16)       # 0: 일반
    globals_ += _rec(xls.XF, struct.pack("<HH", 0, 164) + b"\x00" * 16)     # 1: 날짜 서식
    globals_ += _rec(xls.DATEMODE, b"\x00\x00")
    name = "목록"
    sheet_head_size = 4 + 8 + len(name) * 2              # BOUNDSHEET 레코드 길이
    offset = len(globals_) + sheet_head_size + len(sst) + 4  # 그 뒤 SST, EOF 다음이 시트
    globals_ += _rec(xls.BOUNDSHEET, struct.pack("<IBB", offset, 0, 0) + bytes([len(name), 1])
                     + name.encode("utf-16-le"))
    globals_ += sst + _rec(xls.EOF, b"")
    assert len(globals_) == offset
    sheet = _rec(xls.BOF, b"\x00\x06\x10\x00" + b"\x00" * 12)
    sheet += _rec(xls.LABELSST, struct.pack("<HHHI", 0, 0, 0, 0))             # A1 = 가나다라마
    sheet += _rec(xls.LABEL, struct.pack("<HHH", 0, 1, 0) + _unicode("시군구"))
    sheet += _rec(xls.NUMBER, struct.pack("<HHHd", 1, 0, 1, 45000.0))          # 날짜 서식
    sheet += _rec(xls.RK, struct.pack("<HHHI", 1, 1, 0, (7 << 2) | 2))        # 정수 7
    sheet += _rec(xls.MULRK, struct.pack("<HH", 2, 0) + struct.pack("<HI", 0, (1 << 2) | 2)
                  + struct.pack("<HI", 0, (2 << 2) | 2) + struct.pack("<H", 1))
    sheet += _rec(xls.FORMULA, struct.pack("<HHH", 2, 2, 0) + bytes([0, 0, 0, 0, 0, 0, 0xFF, 0xFF]) + b"\x00" * 8)
    sheet += _rec(xls.STRING, _unicode("합계"))
    sheet += _rec(xls.LABELSST, struct.pack("<HHHI", 3, 0, 0, 1))             # A4 = abc
    sheet += _rec(xls.EOF, b"")
    return globals_ + sheet


def test_biff_records_become_cells_with_dates_numbers_and_split_strings():
    book = xls._read(_workbook())
    [sheet] = book.worksheets
    assert sheet.title == "목록"
    rows = list(sheet.iter_rows(values_only=True))
    assert rows[0][:2] == ("가나다라마", "시군구")         # CONTINUE 경계를 넘은 한글
    assert rows[1][0] == datetime(2023, 3, 15) and rows[1][1] == 7
    assert rows[2] == (1, 2, "합계")                         # MULRK와 수식의 문자열 결과
    assert rows[3][0] == "abc"


def test_an_encrypted_xls_is_not_opened():
    with pytest.raises(xls.Encrypted):
        xls._read(_workbook(encrypted=True))


def test_xls_goes_through_the_same_sections_as_xlsx(monkeypatch, tmp_path):
    monkeypatch.setattr(xls, "open_book", lambda path: xls._read(_workbook()))
    result = xlsx.parse(tmp_path / "옛 목록.xls")
    assert result.status == OK and result.parser == "xls"
    assert result.sections[0].locator.startswith("'목록' A1:")
    assert "가나다라마\t시군구" in result.sections[0].text
    assert "2023-03-15" in result.sections[0].text


def test_a_broken_xls_is_a_failure_not_an_unsupported_format(tmp_path):
    path = tmp_path / "깨진.xls"
    path.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 600)
    assert parsers.parse(path).status == FAILED


@pytest.mark.parametrize("code, is_date", [("yyyy-mm-dd", True), ('yyyy"년" m"월"', True), ("mm:ss", False),
                                          ("#,##0", False), ("General", False), ('0.00"m"', False)])
def test_date_formats(code, is_date):
    assert xls._is_date_format(code) is is_date


# ── 보호(DRM) 문서 ─────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["결과보고.hwp", "숙박확인서.xlsx", "명단.pdf"])
def test_drm_wrapped_files_are_reported_as_protected(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"\x9b DRMONE  This Document is encrypted and protected" + b"\x00" * 512)
    result = parsers.parse(path)
    assert result.status == ENCRYPTED
    assert result.error == parsers.DRM_NOTE


def test_a_text_file_mentioning_drm_is_just_text(tmp_path):
    path = tmp_path / "메모.txt"
    path.write_text("DRMONE 프로그램 설치 안내", encoding="utf-8")
    assert parsers.parse(path).status == OK


# ── 압축 안의 문서 ─────────────────────────────────────────────────

def _zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _inner(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def test_members_are_read_with_their_path_and_nothing_is_written_beside_the_original(tmp_path):
    path = _zip(tmp_path / "제출 묶음.zip", {
        "1. 계획/추진계획.txt": "제출기한: 2025. 9. 30.".encode("utf-8"),
        "../../밖으로.txt": "경로 탈출 시도".encode("utf-8"),
        "설치.exe": b"MZ" + b"\x00" * 100,
        "속압축.zip": _inner({"증빙.txt": "증빙 본문".encode("utf-8"),
                            "더속.zip": _inner({"깊은.txt": b"deep"})}),
    })
    before = sorted(p.name for p in tmp_path.iterdir())
    result = parsers.parse(path)

    assert sorted(p.name for p in tmp_path.iterdir()) == before     # 원본 옆에 아무것도 풀지 않는다
    assert not (tmp_path.parent / "밖으로.txt").exists()
    assert result.status == PARTIAL and result.coverage["entries_read"] == 3
    locators = [s.locator for s in result.sections]
    assert any(l.startswith("1. 계획/추진계획.txt · ") for l in locators)
    assert any(l.startswith("속압축.zip/증빙.txt") for l in locators)
    reasons = {item["member"]: item["reason"] for item in result.coverage["skipped"]}
    assert reasons["설치.exe"] == "실행 파일"
    assert reasons["속압축.zip/더속.zip"] == "압축 깊이 상한"


def test_a_compression_bomb_is_refused(tmp_path):
    path = _zip(tmp_path / "폭탄.zip", {"zeros.txt": b"0" * 3_000_000, "정상.txt": "본문".encode("utf-8")})
    result = parsers.parse(path)
    reasons = {item["member"]: item["reason"] for item in result.coverage["skipped"]}
    assert reasons == {"zeros.txt": "압축률 이상(압축 폭탄 의심)"}
    assert result.coverage["entries_read"] == 1


def test_only_members_missing_from_the_unpacked_folder_are_read(tmp_path):
    same = "같은 문서".encode("utf-8")
    path = _zip(tmp_path / "제출서식.zip", {"서식A.txt": same, "서식B.txt": "압축에만 있음".encode("utf-8")})
    folder = tmp_path / "제출서식"
    folder.mkdir()
    (folder / "서식A.txt").write_bytes(same)
    result = parsers.parse(path)
    assert result.coverage["entries_read"] == 1
    assert any(s.locator.startswith("서식B.txt") for s in result.sections)
    assert {i["member"]: i["reason"] for i in result.coverage["skipped"]} == {"서식A.txt": "풀린 폴더에 같은 파일"}


def test_archive_contents_do_not_become_work_values(tmp_path):
    db = Database(tmp_path / "p.db")
    db.init()
    try:
        db.con.execute("INSERT INTO tasks(id, name) VALUES (1, '평가 대응')")
        source = db.add_source(str(tmp_path))
        doc = db.upsert_document(source, {"path": str(tmp_path / "증빙.zip"), "filename": "증빙.zip", "ext": ".zip",
                                          "parse_status": "ok", "parser": "archive", "hash": "z", "eff_year": 2026})
        db.replace_sections(doc, [("paragraph", 1, "가.hwpx · 1문단", "제출기한 : 2026. 11. 30.")])
        db.assign_document(1, doc)
        assert rc.extract_task(db, 1, date(2026, 10, 14)) == []
    finally:
        db.close()


# ── 읽지 못한 문서를 다시 시도 ─────────────────────────────────────

def test_failures_are_retried_once_when_failure_handling_improves(tmp_path):
    from app.jobs.pipeline import retry_failures

    db = Database(tmp_path / "p.db")
    db.init()
    try:
        source = db.add_source(str(tmp_path))
        old = db.upsert_document(source, {"path": str(tmp_path / "옛.xls"), "filename": "옛.xls", "ext": ".xls",
                                          "parse_status": "unsupported"})
        good = db.upsert_document(source, {"path": str(tmp_path / "새.xlsx"), "filename": "새.xlsx",
                                           "ext": ".xlsx", "parse_status": "ok"})
        db.con.execute("INSERT INTO jobs(kind, doc_id, state, attempts) VALUES ('parse', ?, 'failed_final', 3)", (old,))
        assert retry_failures(db) == 1
        assert db.document(old)["parse_status"] == "pending" and db.document(good)["parse_status"] == "ok"
        assert not db.con.execute("SELECT 1 FROM jobs WHERE doc_id = ?", (old,)).fetchone()
        assert retry_failures(db) == 0                 # 한 번만
    finally:
        db.close()


def test_protected_documents_are_counted_apart_in_readiness(tmp_path):
    db = Database(tmp_path / "p.db")
    db.init()
    try:
        source = db.add_source(str(tmp_path))
        for name, status in (("a.hwp", "ok"), ("b.hwp", "encrypted"), ("c.xls", "failed")):
            db.upsert_document(source, {"path": str(tmp_path / name), "filename": name, "ext": Path(name).suffix,
                                        "parse_status": status})
        layer = next(l for l in initial_analysis.layers(db) if l.name == "본문 추출")
        assert "암호·보호(DRM) 1" in layer.detail and "읽지 못함·건너뜀 1" in layer.detail
    finally:
        db.close()
