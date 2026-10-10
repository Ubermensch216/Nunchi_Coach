"""부서 회신 읽기·점검·증빙 대조 — 회신 취합 도구의 순수한 부분 (계획서 H3·H4).

부서가 채워 보낸 서식(XLSX)을 읽어 실적 한 건을 한 줄로 만들고, 줄마다 점검한다.
프로젝트가 분석한 원본이 아니라 **사용자가 고른 폴더의 바깥 입력**이다. 그래서 파일마다
크기·압축 해제 크기·줄 수에 상한을 두고, 읽을 수 없으면 파일 단위로 사유를 적고 넘어간다.

지키는 것
  · **지우거나 합치지 않는다.** 중복 의심은 묶음 번호만 달고 두 줄을 모두 남긴다.
  · **인정 여부를 말하지 않는다.** 점검은 ‘빈 칸’, ‘날짜 형식’, ‘중복 의심’, ‘증빙 파일이 있는가’
    같은 기계적으로 볼 수 있는 것뿐이다. 증빙이 실적을 뒷받침하는지는 사람이 정한다 —
    낱말이 겹치는 정도를 ‘확인 후보’로 보일 뿐이다.
  · 개인정보 꼴이 있으면 종류와 건수만 적고 값은 적지 않는다.
  · 원본 회신 파일은 읽기만 한다.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from ..core import pii
from .tools.form import column_roles

SHEET_NAME = "03_부서별실적"
EXTENSIONS = (".xlsx", ".xlsm")
MAX_FILES = 60
MAX_BYTES = 15 * 1024 * 1024
MAX_UNCOMPRESSED = 120 * 1024 * 1024
MAX_ROWS = 5000
HEADER_SCAN = 15
NEAR_RATIO = 0.92
NEAR_MIN_CHARS = 8
NEAR_LIMIT = 400                 # 같은 구분 안에서 서로 견주는 줄 수의 상한
OVERLAP_OK = 0.30                # 증빙과 실적 내용의 낱말 겹침 — 실측으로 맞춘 값이 아니다
PROOF_CHARS = 20000
EXCEL_SERIAL = (36526, 60000)    # 2000-01-01 ~ 2064년경

# 줄에 달리는 점검 결과의 이름
MISSING, NO_PROOF, BAD_DATE, FUTURE = "필수값 누락", "증빙 누락", "날짜 형식 확인", "미래 날짜"
DUPLICATE, UNKNOWN_TYPE, PERSONAL = "중복 의심", "구분이 지표 목록에 없음", "개인정보 꼴 있음"
ISSUE_ORDER = (MISSING, NO_PROOF, BAD_DATE, FUTURE, DUPLICATE, UNKNOWN_TYPE, PERSONAL)

EVIDENCE_LABEL = {
    "none": "증빙 없음", "missing": "증빙 파일을 찾지 못함",
    "weak": "파일 있음 · 내용 겹침 낮음(확인 필요)", "ok": "파일 있음 · 내용 겹침 있음(확인 후보)",
    "unread": "파일 있음 · 내용을 읽지 못함",
}
_WORD = re.compile(r"[가-힣A-Za-z]{2,}|\d+")
_SPLIT = re.compile(r"[,;/、·\n]|\s및\s")
_NORMAL = re.compile(r"[\s\W_]+")
_DATE_TEXT = re.compile(r"(?<!\d)((?:19|20)\d{2})\D{1,3}(\d{1,2})\D{1,3}(\d{1,2})")
_EXAMPLE = re.compile(r"^\s*\(?\s*(?:작성\s*예|예시)")


@dataclass
class Row:
    file: str
    line: int
    dept: str = ""
    kind: str = ""
    content: str = ""
    date: str = ""                   # YYYY-MM-DD, 읽지 못하면 빈 값
    date_raw: str = ""
    proof: str = ""
    issues: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    group: int | None = None
    personal: dict[str, int] = field(default_factory=dict)
    evidence: str = ""
    evidence_items: list[dict] = field(default_factory=list)


@dataclass
class ReplyFile:
    name: str
    sha256: str = ""
    rows: int = 0
    problems: list[str] = field(default_factory=list)
    sheet: str = ""


def normal(text: str) -> str:
    return _NORMAL.sub("", text or "").lower()


# ── 읽기 ────────────────────────────────────────────────────────────

def list_files(folder: Path) -> list[Path]:
    """회신으로 읽을 엑셀 파일. 임시 파일(~$)과 숨김 파일은 뺀다. 두 단계 아래까지."""
    found = []
    for path in sorted(folder.rglob("*")):
        depth = len(path.relative_to(folder).parts)
        if depth > 3 or not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            continue
        if path.name.startswith(("~$", ".")):
            continue
        found.append(path)
    return found


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def parse_date(value) -> tuple[str, str]:
    """(YYYY-MM-DD, 적힌 그대로). 날짜로 읽지 못하면 빈 값과 원문이다."""
    raw = _cell_text(value)
    if not raw:
        return "", ""
    if isinstance(value, (datetime, date)):
        return raw, raw
    if isinstance(value, (int, float)) and EXCEL_SERIAL[0] <= value <= EXCEL_SERIAL[1]:
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).strftime("%Y-%m-%d"), raw
    match = _DATE_TEXT.search(raw)
    if match:
        try:
            return date(int(match[1]), int(match[2]), int(match[3])).isoformat(), raw
        except ValueError:
            pass
    return "", raw


def _header(rows: list[tuple]) -> tuple[int, list[str], dict[str, int]] | None:
    for index, row in enumerate(rows[:HEADER_SCAN]):
        names = [_cell_text(c) for c in row]
        filled = [n for n in names if n]
        if len(filled) < 3:
            continue
        roles = column_roles(names)
        if {"dept", "content"} <= set(roles) or {"content", "date", "proof"} <= set(roles):
            return index, names, roles
    return None


def read_file(path: Path, root: Path) -> tuple[ReplyFile, list[Row]]:
    import openpyxl

    info = ReplyFile(path.relative_to(root).as_posix())
    try:
        size = path.stat().st_size
        if size > MAX_BYTES:
            info.problems.append(f"파일이 너무 큽니다 ({size // (1024 * 1024)}MB) — 읽지 않았습니다")
            return info, []
        info.sha256 = _sha256(path)
        with zipfile.ZipFile(path) as archive:
            if sum(item.file_size for item in archive.infolist()) > MAX_UNCOMPRESSED:
                info.problems.append("압축을 풀면 너무 커서 읽지 않았습니다")
                return info, []
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except (OSError, zipfile.BadZipFile, KeyError, ValueError) as exc:
        info.problems.append(f"열 수 없습니다 ({type(exc).__name__}) — 암호가 걸렸거나 손상된 파일일 수 있습니다")
        return info, []
    except Exception as exc:                       # openpyxl이 내는 예외는 갈래가 많다
        info.problems.append(f"읽지 못했습니다 ({type(exc).__name__})")
        return info, []

    try:
        names = [SHEET_NAME] + [n for n in workbook.sheetnames if n != SHEET_NAME]
        for sheet_name in names:
            if sheet_name not in workbook.sheetnames:
                continue
            rows = []
            for number, row in enumerate(workbook[sheet_name].iter_rows(values_only=True), 1):
                if number > MAX_ROWS + HEADER_SCAN:
                    info.problems.append(f"‘{sheet_name}’ 시트는 {MAX_ROWS:,}줄까지만 읽었습니다")
                    break
                rows.append(row)
            found = _header(rows)
            if found is None:
                continue
            info.sheet = sheet_name
            return info, list(_rows(info, path, rows, *found))
        info.problems.append("실적표 시트를 찾지 못했습니다 (부서·추진내용 열이 있는 시트가 없음)")
        return info, []
    finally:
        workbook.close()


def _rows(info: ReplyFile, path: Path, rows: list[tuple], start: int, names: list[str], roles: dict[str, int]):
    fallback = path.stem
    get = lambda cells, role: _cell_text(cells[roles[role]]) if role in roles and roles[role] < len(cells) else ""
    for offset, cells in enumerate(rows[start + 1:], start + 2):
        if not any(c not in (None, "") for c in cells):
            continue
        dept = get(cells, "dept")
        if any(_EXAMPLE.match(_cell_text(cell)) for cell in cells[:3]):
            continue                       # ‘(작성 예)’로 시작하는 예시 줄은 실적이 아니다
        date_value = cells[roles["date"]] if "date" in roles and roles["date"] < len(cells) else None
        iso, raw = parse_date(date_value)
        row = Row(file=info.name, line=offset, dept=dept or fallback, kind=get(cells, "type"),
                  content=get(cells, "content"), date=iso, date_raw=raw, proof=get(cells, "proof"))
        # 부서 열이 아예 없는 파일은 부서를 파일 이름에서 얻는다 — 그것은 빈 칸이 아니다.
        row.missing = [label for role, label, value in (("dept", "부서", dept), ("content", "추진내용", row.content),
                                                        ("date", "추진일", raw))
                       if not value and (role != "dept" or "dept" in roles)]
        info.rows += 1
        yield row


# ── 점검 ────────────────────────────────────────────────────────────

def check_rows(rows: list[Row], today: date, type_names: set[str] | None = None) -> None:
    """줄마다 기계적으로 볼 수 있는 것을 점검한다. 줄을 지우지 않는다."""
    known = {normal(n) for n in type_names or ()}
    for row in rows:
        if row.missing:
            row.issues.append(MISSING)
        if not row.proof:
            row.issues.append(NO_PROOF)
        if row.date_raw and not row.date:
            row.issues.append(BAD_DATE)
        elif row.date and date.fromisoformat(row.date) > today:
            row.issues.append(FUTURE)
        if known and row.kind and not _known_type(normal(row.kind), known):
            row.issues.append(UNKNOWN_TYPE)
        row.personal = pii.scan(" ".join((row.dept, row.kind, row.content, row.proof)))
        if row.personal:
            row.issues.append(PERSONAL)
    mark_duplicates(rows)


def _known_type(kind: str, known: set[str]) -> bool:
    """지표 이름과 같거나 한쪽이 다른 쪽을 품으면 아는 구분이다 (‘규제 발굴’ ⊂ ‘규제 발굴 건수’)."""
    return any(kind == name or (len(kind) >= 2 and (kind in name or name in kind)) for name in known if name)


def mark_duplicates(rows: list[Row]) -> int:
    """같은 내용으로 보이는 줄에 같은 묶음 번호를 단다. 묶음 수를 돌려준다."""
    keyed: dict[str, list[Row]] = {}
    for row in rows:
        if row.content:
            keyed.setdefault(normal(row.content), []).append(row)
    groups = 0
    for same in keyed.values():
        if len(same) > 1:
            groups += 1
            for row in same:
                row.group = groups
                row.issues.append(DUPLICATE)
    by_kind: dict[str, list[Row]] = {}
    for row in rows:
        if row.group is None and len(normal(row.content)) >= NEAR_MIN_CHARS:
            by_kind.setdefault(normal(row.kind), []).append(row)
    for same in by_kind.values():
        same = same[:NEAR_LIMIT]
        for index, left in enumerate(same):
            for right in same[index + 1:]:
                if difflib.SequenceMatcher(None, normal(left.content), normal(right.content)).ratio() >= NEAR_RATIO:
                    number = left.group or right.group
                    if number is None:
                        groups += 1
                        number = groups
                    for row in (left, right):
                        if row.group is None:
                            row.group = number
                            row.issues.append(DUPLICATE)
    return groups


def read_folder(folder: Path, today: date, type_names: set[str] | None = None) -> tuple[list[ReplyFile], list[Row]]:
    files, rows = [], []
    listed = list_files(folder)
    for path in listed[:MAX_FILES]:
        info, found = read_file(path, folder)
        files.append(info)
        rows.extend(found)
    if len(listed) > MAX_FILES:
        files.append(ReplyFile("(생략)", problems=[f"파일이 {len(listed)}개라 앞의 {MAX_FILES}개만 읽었습니다"]))
    check_rows(rows, today, type_names)
    return files, rows


# ── 증빙 대조 ───────────────────────────────────────────────────────

def proof_files(folder: Path, skip: set[str]) -> list[tuple[Path, str]]:
    """증빙 후보 파일. 회신 엑셀 자신은 뺀다. (경로, 이름의 정규형)."""
    out = []
    for path in sorted(folder.rglob("*")):
        if (path.is_file() and not path.name.startswith(("~$", "."))
                and path.relative_to(folder).as_posix() not in skip
                and len(path.relative_to(folder).parts) <= 4):
            out.append((path, normal(path.stem)))
    return out


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text or ""))


def _read_text(path: Path) -> str | None:
    from ..ingest.parsers import parse

    try:
        result = parse(path)
    except Exception:
        return None
    if getattr(result, "status", "") not in ("ok", "partial"):
        return None
    return " ".join(section.text for section in result.sections)[:PROOF_CHARS]


def match_row(row: Row, files: list[tuple[Path, str]], cache: dict[Path, str | None]) -> None:
    """증빙 칸의 항목마다 파일을 찾고, 있으면 실적 내용과 낱말이 얼마나 겹치는지 본다."""
    items = [item.strip() for item in _SPLIT.split(row.proof) if len(normal(item)) >= 2]
    if not items:
        row.evidence = "none"
        return
    content = _words(row.content)
    worst = "ok"
    order = ("ok", "unread", "weak", "missing")
    for item in items:
        key = normal(item)
        hits = [(path, name) for path, name in files if len(name) >= 3 and (key in name or name in key)]
        if not hits:
            status, shown = "missing", ""
        else:
            path = max(hits, key=lambda hit: len(hit[1]))[0]
            if path not in cache:
                cache[path] = _read_text(path)
            text = cache[path]
            if text is None:
                status = "unread"
            else:
                shared = len(content & _words(text)) / len(content) if content else 0.0
                status = "ok" if shared >= OVERLAP_OK else "weak"
            shown = path.name
        row.evidence_items.append({"item": item, "status": status, "file": shown})
        if order.index(status) > order.index(worst):
            worst = status
    row.evidence = worst


def rows_to_json(rows: list[Row]) -> list[dict]:
    return [asdict(row) for row in rows]


def rows_from_json(items: list[dict]) -> list[Row]:
    return [Row(**item) for item in items]
