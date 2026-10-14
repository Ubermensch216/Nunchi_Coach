"""구형 XLS(BIFF8, Excel 97~2003) 읽기 — 새 의존성 없이 olefile로 (규제개혁 비교연구 R08).

실제 인수인계 자료에서 구형 XLS 9건이 '지원하지 않음'이었다. 그중에는 우리 기관의 규제개선
과제 목록과 행정규제 등록 목록(RIMS 내보내기)이 있었다. xlrd를 들이면 잠금 파일·SBOM·라이선스
고지·오프라인 설치본을 모두 바꿔야 한다. 이미 쓰는 olefile로 Workbook 스트림의 레코드를
직접 읽는다.

읽는 것: 시트 이름, 셀의 표시값(공유 문자열·문자열·숫자·RK·MULRK·수식의 마지막 계산값·참거짓),
날짜 서식이 붙은 숫자는 날짜로. 읽지 않는 것: 서식·병합·그림·차트 시트·매크로. 수식을 다시
계산하지 않는다(저장된 값만). 암호(FILEPASS)가 걸린 통합문서는 열지 않는다.

openpyxl 통합문서처럼 쓰도록 `worksheets`, 시트의 `title`·`max_row`·`max_column`·`iter_rows()`를
갖춘 얇은 꼴로 돌려준다. xlsx 파서와 건 목록 읽기가 같은 길로 읽는다.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import olefile

PARSER = "xls"

BOF, EOF, BOUNDSHEET, SST, CONTINUE = 0x0809, 0x000A, 0x0085, 0x00FC, 0x003C
FORMAT, XF, DATEMODE, FILEPASS, CODEPAGE = 0x041E, 0x00E0, 0x0022, 0x002F, 0x0042
LABELSST, LABEL, RSTRING, NUMBER, RK, MULRK = 0x00FD, 0x0204, 0x00D6, 0x0203, 0x027E, 0x00BD
FORMULA, STRING, BOOLERR = 0x0006, 0x0207, 0x0205

# 날짜로 보는 내장 서식 번호 — 영문(14~22, 45~47)과 한국어·CJK 판(27~36, 50~58)
_DATE_FORMATS = frozenset({*range(14, 23), *range(27, 37), 45, 46, 47, *range(50, 59)})
_MAX_ROWS = 65_536


class XlsError(Exception):
    """열 수 없는 XLS. 메시지는 사용자에게 보여도 되는 한글이다."""


class Encrypted(XlsError):
    pass


@dataclass
class Sheet:
    title: str
    cells: dict[tuple[int, int], object] = field(default_factory=dict)

    @property
    def max_row(self) -> int:
        return max((r for r, _ in self.cells), default=-1) + 1

    @property
    def max_column(self) -> int:
        return max((c for _, c in self.cells), default=-1) + 1

    def iter_rows(self, values_only: bool = True, max_col: int | None = None, max_row: int | None = None):
        width = min(self.max_column, max_col) if max_col else self.max_column
        height = min(self.max_row, max_row) if max_row else self.max_row
        for row in range(height):
            yield tuple(self.cells.get((row, col)) for col in range(width))


@dataclass
class Book:
    worksheets: list[Sheet]
    title: str | None = None
    author: str | None = None
    created: datetime | None = None
    modified: datetime | None = None

    def close(self) -> None:
        pass


# ── 레코드 ─────────────────────────────────────────────────────────

def _records(data: bytes, start: int = 0):
    pos = start
    end = len(data)
    while pos + 4 <= end:
        kind, size = struct.unpack_from("<HH", data, pos)
        body = data[pos + 4:pos + 4 + size]
        yield pos, kind, body
        pos += 4 + size


class _Segments:
    """CONTINUE로 이어지는 레코드 몸통을 하나로 읽는다. 글자가 경계를 넘으면 새 플래그 바이트가 온다."""

    def __init__(self, parts: list[bytes]):
        self.parts = parts
        self.index = 0
        self.pos = 0

    def _ensure(self) -> bool:
        while self.index < len(self.parts) and self.pos >= len(self.parts[self.index]):
            self.index += 1
            self.pos = 0
        return self.index < len(self.parts)

    def read(self, count: int) -> bytes:
        out = bytearray()
        while count > 0 and self._ensure():
            part = self.parts[self.index]
            take = min(count, len(part) - self.pos)
            out += part[self.pos:self.pos + take]
            self.pos += take
            count -= take
        return bytes(out)

    def chars(self, count: int, high: bool) -> str:
        out: list[str] = []
        first = True
        while count > 0:
            if not first or self.pos >= len(self.parts[self.index]):
                # 경계를 넘었다. 다음 몸통의 첫 바이트가 이어지는 글자의 폭을 알린다.
                self.index += 1
                self.pos = 0
                if self.index >= len(self.parts):
                    break
                high = bool(self.parts[self.index][0] & 0x01)
                self.pos = 1
            first = False
            part = self.parts[self.index]
            width = 2 if high else 1
            take = min(count, (len(part) - self.pos) // width)
            raw = part[self.pos:self.pos + take * width]
            out.append(raw.decode("utf-16-le", "replace") if high else raw.decode("latin-1"))
            self.pos += take * width
            count -= take
            if count and self.pos < len(part):
                break       # 홀수 바이트가 남는 망가진 몸통 — 더 읽지 않는다
        return "".join(out)

    def unicode(self, length_bytes: int = 2) -> str:
        """XLUnicodeString — 길이, 플래그, (서식 런 수), (확장 길이), 글자, 서식 런, 확장."""
        head = self.read(length_bytes + 1)
        if len(head) < length_bytes + 1:
            return ""
        count = int.from_bytes(head[:length_bytes], "little")
        flags = head[length_bytes]
        runs = struct.unpack("<H", self.read(2))[0] if flags & 0x08 else 0
        extra = struct.unpack("<I", self.read(4))[0] if flags & 0x04 else 0
        text = self.chars(count, bool(flags & 0x01)) if count else ""
        self.read(runs * 4 + extra)
        return text


def _short_string(body: bytes, offset: int) -> str:
    """BOUNDSHEET의 시트 이름: 길이 1바이트, 플래그 1바이트."""
    count, flags = body[offset], body[offset + 1]
    raw = body[offset + 2:offset + 2 + count * (2 if flags & 1 else 1)]
    return raw.decode("utf-16-le", "replace") if flags & 1 else raw.decode("latin-1")


def _rk(value: int) -> float | int:
    if value & 0x02:
        number = value >> 2
        if number & 0x20000000:
            number -= 0x40000000
        return number / 100 if value & 0x01 else number
    number = struct.unpack("<d", struct.pack("<Q", (value & 0xFFFFFFFC) << 32))[0]
    return number / 100 if value & 0x01 else number


def _is_date_format(code: str) -> bool:
    text = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.|_.|\*.', "", code or "")
    lowered = text.lower()
    if not lowered or lowered == "general":
        return False
    return bool(re.search(r"[yd]|[년월일]", lowered) or ("m" in lowered and not re.search(r"[hs0#]", lowered)))


def _as_date(number: float, base_1904: bool) -> datetime | None:
    try:
        if base_1904:
            return datetime(1904, 1, 1) + timedelta(days=number)
        if number < 1:
            return None
        return datetime(1899, 12, 30) + timedelta(days=number)
    except OverflowError:
        return None


def _number(value: float, xf: int, date_xfs: set[int], base_1904: bool):
    if xf in date_xfs:
        when = _as_date(value, base_1904)
        if when is not None:
            return when
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return int(value)
    return value


# ── 통합문서 ───────────────────────────────────────────────────────

def open_book(path: Path | str) -> Book:
    path = Path(path)
    if not olefile.isOleFile(str(path)):
        raise XlsError("OLE 복합문서가 아님 — 구형 XLS가 아니거나 손상된 파일")
    with olefile.OleFileIO(str(path)) as ole:
        stream = next((name for name in ("Workbook", "Book") if ole.exists(name)), None)
        if stream is None:
            raise XlsError("Workbook 스트림이 없음")
        data = ole.openstream(stream).read()
        meta = ole.get_metadata()
    book = _read(data)
    book.title = _text(meta.title)
    book.author = _text(meta.author)
    book.created = meta.create_time if isinstance(meta.create_time, datetime) else None
    book.modified = meta.last_saved_time if isinstance(meta.last_saved_time, datetime) else None
    return book


def _text(value) -> str | None:
    if isinstance(value, bytes):
        for encoding in ("utf-8", "cp949", "latin-1"):
            try:
                return value.decode(encoding).strip() or None
            except UnicodeDecodeError:
                continue
    return (value or "").strip() or None if isinstance(value, str) else None


def _read(data: bytes) -> Book:
    sheets: list[tuple[str, int]] = []
    strings: list[str] = []
    formats: dict[int, str] = {}
    xf_formats: list[int] = []
    base_1904 = False
    globals_end = len(data)

    records = list(_records(data))
    for index, (pos, kind, body) in enumerate(records):
        if kind == FILEPASS:
            raise Encrypted("암호로 보호된 통합문서")
        if kind == BOUNDSHEET and len(body) >= 8:
            offset, sheet_type = struct.unpack_from("<I", body)[0], body[5]
            if sheet_type == 0:                       # 일반 시트만. 차트·매크로 시트는 뺀다
                sheets.append((_short_string(body, 6), offset))
        elif kind == FORMAT and len(body) >= 5:
            formats[struct.unpack_from("<H", body)[0]] = _Segments([body[2:]]).unicode()
        elif kind == XF and len(body) >= 4:
            xf_formats.append(struct.unpack_from("<H", body, 2)[0])
        elif kind == DATEMODE and len(body) >= 2:
            base_1904 = bool(struct.unpack_from("<H", body)[0])
        elif kind == SST and len(body) >= 8:
            parts = [body[8:]]
            for _, next_kind, next_body in records[index + 1:]:
                if next_kind != CONTINUE:
                    break
                parts.append(next_body)
            unique = struct.unpack_from("<I", body, 4)[0]
            reader = _Segments(parts)
            for _ in range(unique):
                if not reader._ensure():
                    break
                strings.append(reader.unicode())
        elif kind == EOF:
            globals_end = pos
            break
    date_xfs = {index for index, fmt in enumerate(xf_formats)
                if fmt in _DATE_FORMATS or _is_date_format(formats.get(fmt, ""))}

    out: list[Sheet] = []
    for title, offset in sheets:
        if offset <= globals_end - 1 or offset >= len(data):
            continue
        out.append(_read_sheet(data, offset, title, strings, date_xfs, base_1904))
    return Book(out)


def _read_sheet(data, offset, title, strings, date_xfs, base_1904) -> Sheet:
    sheet = Sheet(title)
    cells = sheet.cells
    pending: tuple[int, int] | None = None      # 문자열 결과를 기다리는 수식 칸
    for _, kind, body in _records(data, offset):
        if kind == EOF:
            break
        if kind == STRING:
            # 수식의 문자열 결과. 행·열 없이 값만 온다.
            if pending is not None:
                cells[pending] = _Segments([body]).unicode()
                pending = None
            continue
        if len(body) < 6:
            continue
        row, col, xf = struct.unpack_from("<HHH", body)
        if row >= _MAX_ROWS:
            continue
        if kind == LABELSST and len(body) >= 10:
            index = struct.unpack_from("<I", body, 6)[0]
            if index < len(strings):
                cells[(row, col)] = strings[index]
        elif kind in (LABEL, RSTRING):
            cells[(row, col)] = _Segments([body[6:]]).unicode()
        elif kind == NUMBER and len(body) >= 14:
            cells[(row, col)] = _number(struct.unpack_from("<d", body, 6)[0], xf, date_xfs, base_1904)
        elif kind == RK and len(body) >= 10:
            cells[(row, col)] = _number(_rk(struct.unpack_from("<I", body, 6)[0]), xf, date_xfs, base_1904)
        elif kind == MULRK:
            last = struct.unpack_from("<H", body, len(body) - 2)[0]
            for step, column in enumerate(range(col, last + 1)):
                at = 4 + step * 6
                cell_xf, value = struct.unpack_from("<HI", body, at)
                cells[(row, column)] = _number(_rk(value), cell_xf, date_xfs, base_1904)
        elif kind == FORMULA and len(body) >= 14:
            raw = body[6:14]
            if raw[6:8] == b"\xff\xff":
                if raw[0] == 0x00:
                    pending = (row, col)              # 다음 STRING 레코드가 값이다
                elif raw[0] == 0x01:
                    cells[(row, col)] = bool(raw[2])
            else:
                cells[(row, col)] = _number(struct.unpack("<d", raw)[0], xf, date_xfs, base_1904)
        elif kind == BOOLERR and len(body) >= 8 and body[7] == 0:
            cells[(row, col)] = bool(body[6])
    return sheet
