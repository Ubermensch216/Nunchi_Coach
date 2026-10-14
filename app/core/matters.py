"""건 — 업무 안에서 하나씩 따로 처리되는 일 (규제개혁 개선계획서 P1-1, 비교연구 R01·R05·R10).

규제 개선 건의, 개선 사례, 정비 대상 규제사무는 **한 행이 한 건**인 목록으로 오간다. 부서가
낸 목록이 우리 기관 목록이 되고, 상급기관이 여러 기관 것을 모은 취합표가 되고, 부처 검토
결과가 붙어 돌아온다. 업무 단위로만 읽으면 두 가지가 틀린다.

  · 남의 건이 이 업무의 일이 된다. 전국 취합표 819행 가운데 한 행(다른 시의 건의)에 적힌
    토지분할 허가 절차가 "지금 할 일"로 안내됐다(R01). 그래서 P0는 그런 칸을 읽지 않게 했다.
  · 우리 건이 흩어진다. 같은 건의가 다섯 문서에 나와도 "이 건은 어디까지 갔나"를 이어 볼
    방법이 없다. 업무의 준비 항목만 수백 건으로 불어난다(R05).

여기서는 목록의 행을 건으로 세운다. 지키는 것

  · **우리 기관의 행만 건이 된다.** 다른 기관의 행은 몇 행인지만 센다(참고 사례).
  · **근거는 시트와 행 번호다.** 원본 파일을 해시로 같은 판인지 확인한 뒤 읽기 전용으로 연다.
    판이 다르거나 열 수 없으면 읽지 않고 그렇다고 적는다 — 저장된 본문 조각은 셀 안 줄바꿈과
    끝의 빈 칸을 지워 행·칸 경계를 되살릴 수 없다.
  · **자료가 말한 데까지만 진행 단계를 매긴다.** 목록에 있음 → 상급기관 취합표에 있음 →
    회신(검토의견)이 있음. 회신의 '수용'은 회신 칸의 글로 가르고, '개선 완료'는 자료로 정하지
    않는다 — 법령 개정·시행은 사람이 확인한다.
  · 사람이 정한 진행과 메모, '건이 아님' 표시는 다시 분석해도 남는다.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

LISTED, COMPILED, ANSWERED = "listed", "compiled", "answered"
STAGE_LABEL = {LISTED: "우리 목록에 있음 — 제출 근거 미확인",
               COMPILED: "상급기관 취합 목록에 있음", ANSWERED: "회신(검토의견) 있음"}
ACCEPTED, PARTIAL, REJECTED, PENDING = "accepted", "partial", "rejected", "pending"
OUTCOME_LABEL = {ACCEPTED: "수용", PARTIAL: "부분 수용", REJECTED: "불수용", PENDING: "검토 중"}
HUMAN_STAGES = ("submitted", "accepted", "partial", "rejected", "implemented", "closed")
HUMAN_STAGE_LABEL = {"submitted": "제출 확인", "accepted": "수용 확인", "partial": "부분 수용 확인",
                     "rejected": "불수용 확인", "implemented": "개선 시행 확인", "closed": "종결"}
KIND_LABEL = {"proposal": "건의·과제", "case": "사례", "regulation": "정비 대상", "agenda": "안건"}

OWN, COMPILED_LIST = "own", "compiled"
# 이만큼 서로 다른 기관의 행이 있으면 여러 기관 것을 모은 취합표다.
_COMPILED_LOCALITIES = 3
_HEADER_SEARCH_ROWS = 12
_MAX_ROWS = 5000
_MAX_TEXT = 200
_SAME_TITLE = 0.92
_SPREADSHEETS = (".xlsx", ".xlsm", ".xls")


def _norm(text) -> str:
    return re.sub(r"[\s\n]+", "", str(text or ""))


# 머리글(공백·줄바꿈을 뺀 꼴) → 칸의 뜻
_TITLE = {"제목": "proposal", "과제명": "proposal", "건의과제명": "proposal", "과제제목": "proposal",
          "사례명": "case", "안건명": "agenda", "건명": "proposal"}
_REGULATION_TITLE = {"규제사무명": "regulation"}   # 정비 칸이 함께 있을 때만 건이다
_LOCALITY = re.compile(r"^(?:기초(?:\(시군구\))?|시군구명?|기초명|기초자치단체|자치단체명?|지자체명?|기관명)$")
_REGION = re.compile(r"^(?:광역(?:\(시도\))?|시도명?|광역명|광역자치단체)$")
_DEPT = re.compile(r"^(?:소관부서|주관부서|담당부서|부서명?(?:/소속)?|제출부서)$")
_MINISTRY = re.compile(r"^(?:소관부처1?|중앙부처|소관기관)$")
_LAW = re.compile(r"^(?:관련법령(?:\(규정\))?|관련법령또는자치법규|자치법규근거|자치법규명\(조항\)|근거법령)$")
_ROUND = re.compile(r"^(?:구분|분기|반기|회차)$")
# 상대 기관이 돌려준 답. 우리가 적은 정비 판단('정비대상 여부', '정비내용')은 회신이 아니다.
_OUTCOME = re.compile(r"^(?:부처의견1?|검토의견|부처검토의견|검토결과|수용여부|개선여부(?:\(O/X\))?|"
                      r"실적인정여부|결정|조치결과)$")
_FIX_COLUMN = re.compile(r"정비")

_ROUND_IN_NAME = re.compile(r"(?P<y>20\d{2}|'?\d{2})\s*년?\s*도?\s*(?P<p>상반기|하반기|[1-4]\s*분기)")
_YEAR_IN_NAME = re.compile(r"(?<!\d)(20\d{2})(?!\d)")


@dataclass
class Columns:
    title: int
    kind: str
    locality: int
    header_row: int
    region: int | None = None
    dept: int | None = None
    ministry: int | None = None
    law: int | None = None
    round: int | None = None
    outcome: list[int] = field(default_factory=list)
    extra: list[int] = field(default_factory=list)      # 정비 판단처럼 행에 남겨 둘 칸
    names: list[str] = field(default_factory=list)


def find_columns(rows: list[tuple]) -> Columns | None:
    """시트 앞쪽에서 건 목록의 머리 행을 찾는다. 제목 칸과 시군구 칸이 함께 있어야 한다.

    시군구 칸이 없으면 누구의 건인지 가릴 수 없으므로 건 목록으로 보지 않는다.
    """
    for index, row in enumerate(rows[:_HEADER_SEARCH_ROWS]):
        names = [_norm(cell) for cell in row]
        title = next(((i, _TITLE[n]) for i, n in enumerate(names) if n in _TITLE), None)
        if title is None and any(_FIX_COLUMN.search(n) for n in names):
            title = next(((i, _REGULATION_TITLE[n]) for i, n in enumerate(names) if n in _REGULATION_TITLE), None)
        locality = next((i for i, n in enumerate(names) if _LOCALITY.match(n)), None)
        if title is None or locality is None:
            continue

        def first(pattern):
            return next((i for i, n in enumerate(names) if pattern.match(n)), None)

        return Columns(title=title[0], kind=title[1], locality=locality, header_row=index,
                       region=first(_REGION), dept=first(_DEPT), ministry=first(_MINISTRY),
                       law=first(_LAW), round=first(_ROUND),
                       outcome=[i for i, n in enumerate(names) if _OUTCOME.match(n)],
                       extra=[i for i, n in enumerate(names) if _FIX_COLUMN.search(n)],
                       names=[str(cell or "").replace("\n", " ").strip() for cell in row])
    return None


def classify_outcome(text: str) -> str:
    """회신 칸의 글에서 수용 여부를 가른다. 못 가르면 ''."""
    flat = _norm(text)
    if not flat:
        return ""
    if re.search(r"부분수용|일부수용", flat):
        return PARTIAL
    if re.search(r"불수용|수용불가|수용곤란|불인정|미반영|^X$|^×$", flat):
        return REJECTED
    if re.search(r"중장기|추가검토|검토중|장기검토|협의중", flat):
        return PENDING
    if re.search(r"수용|반영|인정|^O$|^○$", flat):
        return ACCEPTED
    return ""


def title_key(title: str) -> str:
    return re.sub(r"[\s「」『』\"'“”‘’()\[\]·ㆍ,.\-~]", "", title or "")


# ── 우리 기관 ───────────────────────────────────────────────────────
OWN_META = "own_locality"
_PAREN_PLACE = re.compile(r"[(_\s]((?:[가-힣]{1,4}\s)?[가-힣]{1,5}(?:구|군|시))[)_\s.]")
_NOT_PLACE = frozenset({"구", "군", "시", "지자체", "시도", "시군구", "구군", "시군", "부산시", "서울시", "광역시",
                        "특별시", "자치구", "기초", "광역", "각구", "구청", "전체", "기타", "도시"})


def ready(db) -> bool:
    """건 표가 있는 DB인가. 읽기 전용으로 연 옛 프로젝트에는 아직 없다 — 없으면 건 없이 보인다."""
    return db.con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'matter_scans'").fetchone() is not None


def own_locality(db) -> str:
    """이 자료의 주인인 기관(시군구). 사람이 정한 것이 먼저, 없으면 파일 이름에서 가장 자주 나온 곳.

    전임자는 제출본 이름에 기관명을 붙인다 — '사례 제출목록(가나구).xlsx'. 세 번 넘게 나오지
    않으면 정하지 않는다(정하지 못하면 건을 만들지 않는다 — 남의 건을 우리 건으로 만들지 않는다).
    """
    chosen = (db.get_meta(OWN_META) or "").strip()
    if chosen:
        return chosen
    counted: Counter = Counter()
    for row in db.con.execute("SELECT filename FROM documents WHERE missing_since IS NULL"):
        name = f" {Path(row['filename'] or '').stem} "
        for found in _PAREN_PLACE.findall(name):
            place = found.split()[-1]
            if place not in _NOT_PLACE and not place.endswith(("광역시", "특별시")):
                counted[place] += 1
    if not counted:
        return ""
    place, times = counted.most_common(1)[0]
    return place if times >= 3 else ""


def own_locality_basis(db) -> tuple[str, str]:
    """(기관, 출처) — 출처는 'chosen'(담당자가 정함) · 'inferred'(파일 이름에서 추정) · ''(정하지 못함)."""
    if (db.get_meta(OWN_META) or "").strip():
        return own_locality(db), "chosen"
    place = own_locality(db)
    return place, "inferred" if place else ""


def set_own_locality(db, place: str) -> None:
    db.set_meta(OWN_META, " ".join((place or "").split()))


def _ours(value: str, own: str) -> bool:
    return bool(own) and own in _norm(value)


# ── 목록 문서 읽기 ──────────────────────────────────────────────────

@dataclass
class Row:
    sheet: str
    row: int
    title: str
    locality: str
    kind: str
    dept: str = ""
    ministry: str = ""
    law: str = ""
    round_cell: str = ""
    outcome_text: str = ""
    fields: dict = field(default_factory=dict)


@dataclass
class Listing:
    own: list[Row]
    others: int
    compiled: bool


def read_listing(path: Path, own: str) -> Listing | None:
    """표 계산 문서의 건 목록 시트들을 읽는다. 건 목록이 없으면 None."""
    from ..ingest.parsers.xlsx import open_workbook

    workbook = open_workbook(path)
    try:
        own_rows: list[Row] = []
        others = 0
        localities: set[str] = set()
        found_list = False
        for sheet in workbook.worksheets:
            rows = []
            for values in sheet.iter_rows(values_only=True, max_row=_MAX_ROWS):
                rows.append(tuple("" if value is None else str(value).strip() for value in values))
            columns = find_columns(rows)
            if columns is None:
                continue
            found_list = True
            carried = ""        # 병합된 시군구 칸은 첫 행에만 값이 있다
            for offset, values in enumerate(rows[columns.header_row + 1:], start=columns.header_row + 2):
                def cell(index):
                    return values[index] if index is not None and index < len(values) else ""

                title, locality = cell(columns.title), cell(columns.locality) or carried
                carried = locality
                if not title or not locality or _norm(title) == _norm(columns.names[columns.title]):
                    continue
                localities.add(_norm(locality))
                if not _ours(locality, own):
                    others += 1
                    continue
                outcome = " / ".join(cell(i) for i in columns.outcome if cell(i))
                own_rows.append(Row(
                    sheet=sheet.title, row=offset, title=" ".join(title.split())[:_MAX_TEXT],
                    locality=" ".join(locality.split()), kind=columns.kind,
                    dept=" ".join(cell(columns.dept).split())[:80],
                    ministry=" ".join(cell(columns.ministry).split())[:80],
                    law=" ".join(cell(columns.law).split())[:_MAX_TEXT],
                    round_cell=" ".join(cell(columns.round).split())[:20],
                    outcome_text=" ".join(outcome.split())[:_MAX_TEXT],
                    fields={columns.names[i]: " ".join(cell(i).split())[:_MAX_TEXT]
                            for i in [columns.title, columns.locality, columns.dept, columns.ministry,
                                      columns.law, columns.round, *columns.outcome, *columns.extra]
                            if i is not None and cell(i) and columns.names[i]}))
        if not found_list:
            return None
        return Listing(own_rows, others, len(localities) >= _COMPILED_LOCALITIES)
    finally:
        workbook.close()


def _round_of(filename: str, cell: str, year: int | None) -> tuple[str, int | None]:
    """'2025 상반기' — 파일 이름이 먼저, 그다음 행의 구분 칸."""
    match = _ROUND_IN_NAME.search(filename or "")
    if match:
        raw = match.group("y").lstrip("'")
        found_year = int(raw) if len(raw) == 4 else 2000 + int(raw)
        return f"{found_year} {re.sub(r'\s+', '', match.group('p'))}", found_year
    named = _YEAR_IN_NAME.search(filename or "")
    found_year = int(named.group(1)) if named else year
    part = cell if re.fullmatch(r"상반기|하반기|[1-4]\s*분기", cell or "") else ""
    label = " ".join(str(x) for x in (found_year, part) if x)
    return label, found_year


# ── 다시 세우기 ─────────────────────────────────────────────────────

def rebuild(db, should_stop=lambda: False) -> dict[str, int]:
    """표 계산 문서의 건 목록을 읽어 건을 맞춘다. 판이 그대로인 문서는 다시 열지 않는다."""
    from ..ingest.hasher import sha256

    totals = {"lists": 0, "read": 0, "unreadable": 0, "matters": 0, "own_rows": 0, "other_rows": 0}
    own = own_locality(db)
    if not own or not ready(db):
        return totals
    marks = ", ".join("?" * len(_SPREADSHEETS))
    documents = db.con.execute(
        f"SELECT id, path, filename, hash, eff_year FROM documents "
        f"WHERE missing_since IS NULL AND parse_status IN ('ok', 'partial') AND LOWER(ext) IN ({marks}) "
        f"ORDER BY id", _SPREADSHEETS).fetchall()
    scans = {row["doc_id"]: row for row in db.con.execute("SELECT * FROM matter_scans")}
    for document in documents:
        if should_stop():
            break
        scan = scans.get(document["id"])
        if scan is not None and scan["hash"] == document["hash"] and scan["state"] in ("read", "not_a_list") \
                and scan["note"] == own:
            continue        # 같은 판을 같은 기관 기준으로 이미 읽었다
        digest, error = sha256(document["path"])
        if digest is None or (document["hash"] and digest != document["hash"]):
            # 원본이 바뀌었거나 열 수 없다. 전에 읽은 건은 그대로 두고 새로 읽지 않는다.
            state = "unreadable" if digest is None else "changed"
            _save_scan(db, document["id"], document["hash"], state, 0, 0, error or "원본이 분석 후 바뀜")
            totals["unreadable"] += 1
            continue
        try:
            listing = read_listing(Path(document["path"]), own)
        except Exception as exc:  # noqa: BLE001 — 손상된 파일 하나가 전체를 멈추지 않게 한다
            _save_scan(db, document["id"], document["hash"], "unreadable", 0, 0, type(exc).__name__)
            totals["unreadable"] += 1
            continue
        with db.transaction():
            db.con.execute("DELETE FROM matter_sources WHERE doc_id = ?", (document["id"],))
            if listing is None:
                _save_scan(db, document["id"], document["hash"], "not_a_list", 0, 0, own)
                continue
            totals["lists"] += 1
            for row in listing.own:
                _attach(db, document, row, listing.compiled)
            _save_scan(db, document["id"], document["hash"], "read", len(listing.own), listing.others, own)
        totals["read"] += 1
    with db.transaction():
        _settle(db)
    totals["matters"] = db.con.execute("SELECT COUNT(*) FROM matters WHERE missing_since IS NULL").fetchone()[0]
    for row in db.con.execute("SELECT SUM(rows_own) own, SUM(rows_other) other FROM matter_scans WHERE state='read'"):
        totals["own_rows"], totals["other_rows"] = row["own"] or 0, row["other"] or 0
    return totals


def _save_scan(db, doc_id, digest, state, own, other, note) -> None:
    db.con.execute(
        "INSERT INTO matter_scans(doc_id, hash, state, rows_own, rows_other, note) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(doc_id) DO UPDATE SET hash=excluded.hash, state=excluded.state, rows_own=excluded.rows_own, "
        "rows_other=excluded.rows_other, note=excluded.note, updated_at=datetime('now')",
        (doc_id, digest, state, own, other, note or ""))


# 부서가 낸 목록의 이름: '…건의과제 목록(토지정보과).xlsx'
_DEPT_IN_NAME = re.compile(r"\((?P<dept>[가-힣]{2,10}(?:과|팀|계|실|소|단|담당관|회관))\)")


def _attach(db, document, row: Row, compiled: bool) -> None:
    if not row.dept and not compiled:
        named = _DEPT_IN_NAME.search(document["filename"] or "")
        if named:
            row.dept = named.group("dept")
    matter_id = _matter_for(db, row)
    round_label, year = _round_of(document["filename"] or "", row.round_cell, document["eff_year"])
    db.con.execute(
        "INSERT OR IGNORE INTO matter_sources(matter_id, doc_id, sheet, row, list_kind, round, period_year, "
        "fields, quote) VALUES (?,?,?,?,?,?,?,?,?)",
        (matter_id, document["id"], row.sheet, row.row, COMPILED_LIST if compiled else OWN, round_label, year,
         json.dumps(row.fields, ensure_ascii=False), f"{row.locality} · {row.title}"[:_MAX_TEXT]))
    # 비어 있는 칸만 채운다. 앞선 목록이 적은 부서·부처를 뒤의 빈칸이 지우지 않게.
    current = db.con.execute("SELECT dept, ministry, law, outcome_text FROM matters WHERE id = ?",
                             (matter_id,)).fetchone()
    updates = {name: value for name, value in (("dept", row.dept), ("ministry", row.ministry), ("law", row.law))
               if value and not current[name]}
    if row.outcome_text and row.outcome_text not in current["outcome_text"]:
        updates["outcome_text"] = " / ".join(p for p in (current["outcome_text"], row.outcome_text) if p)[:_MAX_TEXT * 2]
    if updates:
        db.con.execute(f"UPDATE matters SET {', '.join(f'{k} = ?' for k in updates)}, updated_at = datetime('now') "
                       f"WHERE id = ?", (*updates.values(), matter_id))


def _matter_for(db, row: Row) -> int:
    """같은 기관의 같은 제목(띄어쓰기·괄호·기호를 뺀 꼴)이면 같은 건이다. 거의 같은 제목도 묶는다."""
    place = _norm(row.locality.split()[-1]) if row.locality else ""
    key = f"{place}|{title_key(row.title)}"
    found = db.con.execute("SELECT id FROM matters WHERE key = ?", (key,)).fetchone()
    if found:
        return found["id"]
    wanted = title_key(row.title)
    for other in db.con.execute("SELECT id, key FROM matters WHERE key LIKE ?", (f"{place}|%",)):
        theirs = other["key"].split("|", 1)[1]
        if min(len(wanted), len(theirs)) >= 8 and \
                SequenceMatcher(None, wanted, theirs, autojunk=False).ratio() >= _SAME_TITLE:
            return other["id"]
    return db.con.execute(
        "INSERT INTO matters(key, title, locality, kind) VALUES (?, ?, ?, ?)",
        (key, row.title, row.locality, row.kind)).lastrowid


def _settle(db) -> None:
    """등장 기록에서 진행 단계와 회신 갈래를 다시 매긴다. 근거가 사라진 건은 표시만 한다."""
    for matter in db.con.execute("SELECT id, outcome_text, human_stage, human_note FROM matters").fetchall():
        kinds = {row["list_kind"] for row in db.con.execute(
            "SELECT list_kind FROM matter_sources WHERE matter_id = ?", (matter["id"],))}
        if not kinds:
            if matter["human_stage"] or matter["human_note"]:
                db.con.execute("UPDATE matters SET missing_since = COALESCE(missing_since, datetime('now')) "
                               "WHERE id = ?", (matter["id"],))
            else:
                db.con.execute("DELETE FROM matters WHERE id = ?", (matter["id"],))
            continue
        outcome = classify_outcome(matter["outcome_text"]) if matter["outcome_text"] else ""
        stage = ANSWERED if matter["outcome_text"] else (COMPILED if COMPILED_LIST in kinds else LISTED)
        db.con.execute("UPDATE matters SET stage = ?, outcome = ?, missing_since = NULL WHERE id = ?",
                       (stage, outcome, matter["id"]))


# ── 사람의 확인 ─────────────────────────────────────────────────────

def review(db, matter_id: int, stage: str | None = None, note: str | None = None,
           excluded: bool | None = None) -> None:
    """사람이 진행을 정하거나 메모를 남기거나 '건이 아님'으로 뺀다. 다시 분석해도 남는다.

    stage=''는 정한 진행을 지운다(자료대로 본다).
    """
    if stage is not None and stage and stage not in HUMAN_STAGES:
        raise ValueError(f"알 수 없는 진행: {stage}")
    sets, values = [], []
    if stage is not None:
        sets.append("human_stage = ?")
        values.append(stage or None)
    if note is not None:
        sets.append("human_note = ?")
        values.append(note)
    if excluded is not None:
        sets.append("excluded = ?")
        values.append(int(excluded))
    if not sets:
        return
    db.con.execute(f"UPDATE matters SET {', '.join(sets)}, updated_at = datetime('now') WHERE id = ?",
                   (*values, matter_id))
    db.audit("matter.review", str(matter_id), result=stage or "")


# ── 업무에서 보기 ───────────────────────────────────────────────────

def for_task(db, task_id: int, include_excluded: bool = False) -> list[dict]:
    """이 업무의 문서에 나온 우리 기관의 건 — 최근 회차부터. 등장마다의 근거를 함께.

    include_excluded는 건 확인 화면용이다 — 담당자가 뺀 건을 되돌릴 수 있어야 한다.
    """
    if not ready(db):
        return []
    rows = db.con.execute(
        "SELECT DISTINCT m.* FROM matters m JOIN matter_sources s ON s.matter_id = m.id "
        "JOIN task_docs td ON td.doc_id = s.doc_id WHERE td.task_id = ? AND (m.excluded = 0 OR ?) "
        "AND m.missing_since IS NULL", (task_id, int(include_excluded))).fetchall()
    out = []
    for matter in rows:
        # 근거가 지금 원문과 같은가 — 그 판(해시)을 읽어 세운 등장인가.
        sources = db.con.execute(
            "SELECT s.*, d.filename, d.path, (c.state = 'read' AND c.hash IS d.hash) AS current "
            "FROM matter_sources s JOIN documents d ON d.id = s.doc_id "
            "LEFT JOIN matter_scans c ON c.doc_id = s.doc_id "
            "WHERE s.matter_id = ? ORDER BY COALESCE(s.period_year, 0) DESC, s.doc_id", (matter["id"],)).fetchall()
        latest = next((s["round"] for s in sources if s["round"]), "")
        out.append({**dict(matter), "round": latest,
                    "year": max((s["period_year"] or 0 for s in sources), default=0),
                    "evidence": [{"doc_id": s["doc_id"], "locator": f"'{s['sheet']}' {s['row']}행",
                                  "quote": s["quote"], "filename": s["filename"], "path": s["path"],
                                  "current": bool(s["current"]), "list_kind": s["list_kind"]}
                                 for s in sources]})
    out.sort(key=lambda m: (-m["year"], m["round"], m["title"]))
    return out


def waiting_for_locality(db, task_id: int) -> int:
    """우리 기관을 정하지 못해 건 목록을 읽지 않은 이 업무의 표 계산 문서 수."""
    if not ready(db) or own_locality(db):
        return 0
    marks = ", ".join("?" * len(_SPREADSHEETS))
    return db.con.execute(
        f"SELECT COUNT(*) FROM task_docs td JOIN documents d ON d.id = td.doc_id WHERE td.task_id = ? "
        f"AND d.missing_since IS NULL AND d.parse_status IN ('ok', 'partial') AND LOWER(d.ext) IN ({marks})",
        (task_id, *_SPREADSHEETS)).fetchone()[0]


def other_rows(db, task_id: int) -> int:
    """이 업무의 목록 문서에 있는 다른 기관의 행 수 — 참고 사례로만 센다."""
    if not ready(db):
        return 0
    row = db.con.execute(
        "SELECT SUM(s.rows_other) FROM matter_scans s JOIN task_docs td ON td.doc_id = s.doc_id "
        "WHERE td.task_id = ? AND s.state = 'read'", (task_id,)).fetchone()
    return row[0] or 0


def progress_line(matter: dict) -> str:
    """'상급기관 취합 목록에 있음 · 회신: 수용' — 자료로 확인한 것과 사람이 정한 것을 나눠 적는다."""
    parts = [STAGE_LABEL.get(matter["stage"], matter["stage"])]
    if matter["outcome_text"]:
        parts.append(f"회신 ‘{matter['outcome_text']}’")
    if matter["outcome"]:
        parts.append(f"갈래: {OUTCOME_LABEL[matter['outcome']]}(회신 글로 판단)")
    if matter.get("human_stage"):
        parts.append(f"담당자: {HUMAN_STAGE_LABEL.get(matter['human_stage'], matter['human_stage'])}")
    return " · ".join(parts)


def describe(matter: dict) -> str:
    """매뉴얼 한 줄: '2025 상반기 · 토지 경계조정 간소화 — 주관 토지정보과 · 소관 국토교통부'."""
    head = " · ".join(p for p in (matter.get("round"), matter["title"]) if p)
    tail = [f"주관 {matter['dept']}" if matter["dept"] else "",
            f"소관 {matter['ministry']}" if matter["ministry"] else ""]
    tail = " · ".join(p for p in tail if p)
    return head + (f" — {tail}" if tail else "")


# 건의 종류마다 '다음에 와야 할 결과'가 다르다.
AWAITED = {"proposal": "부처 회신(검토의견)", "case": "선정·결과 통보", "regulation": "정비 결과(자치법규 개정·등록 반영)",
           "agenda": "심의 결과"}
_LOOKALIKE = 5      # 띄어쓰기를 뺀 제목에서 이만큼 이어서 같으면 같은 건일 수 있다


def lookalike(matter: dict, others: list[dict]) -> dict | None:
    """제목이 바뀌어 따로 잡혔을 수 있는, 진행이 더 나간 건. 합치지 않고 사람에게 묻는다."""
    mine = title_key(matter["title"])
    best, size = None, 0
    for other in others:
        if other["id"] == matter["id"] or other["stage"] == LISTED:
            continue
        theirs = title_key(other["title"])
        found = SequenceMatcher(None, mine, theirs, autojunk=False).find_longest_match(
            0, len(mine), 0, len(theirs)).size
        if found >= _LOOKALIKE and found > size:
            best, size = other, found
    return best


def unanswered(matters: list[dict]) -> list[dict]:
    """가장 최근 회차의 건 가운데 회신이 연결되지 않았고 사람도 진행을 정하지 않은 것."""
    if not matters:
        return []
    latest = matters[0]["round"] or matters[0]["year"]
    return [m for m in matters if (m["round"] or m["year"]) == latest
            and m["stage"] != ANSWERED and not m.get("human_stage")]
