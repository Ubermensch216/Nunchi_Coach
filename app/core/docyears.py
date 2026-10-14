"""한 문서의 여러 '해' (행동형 업무 비서 계획서 M2).

문서의 시점(core/dating.py)은 하나지만, 문서가 말하는 해는 하나가 아니다. 2026년
3월에 낸 자료가 2025년 실적을 담고 2026년 평가에 쓰인다. 이것을 한 해로 접으면
'전년도 자료'를 고를 때 한 해씩 밀린다.

    authored     문서를 쓴 해           documents.eff_year (수정일로만 정한 것은 뺀다)
    performance  실적이 속한 해         "2025년도 실적", "실적연도: 2025"
    evaluation   평가가 시행된 해       "2026년 ○○ 평가", "평가연도: 2026"
    submitted    제출한 해              "제출일: 2026. 3. 14."

지키는 것
  · 적힌 것만 읽는다. 작성 연도에서 실적 연도를 계산해 내지 않는다(작성 연도 - 1 같은
    짐작을 하지 않는다).
  · 근거가 된 원문과 위치를 남긴다.
  · documents.eff_year는 건드리지 않는다. 일정·처리 순서는 지금처럼 그것을 쓴다.
  · 사람이 정한 해는 다시 분석해도 덮지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .dating import MIN_YEAR

AUTHORED, PERFORMANCE, EVALUATION, SUBMITTED = "authored", "performance", "evaluation", "submitted"
KINDS = (AUTHORED, PERFORMANCE, EVALUATION, SUBMITTED)
KIND_LABEL = {AUTHORED: "작성", PERFORMANCE: "실적", EVALUATION: "평가", SUBMITTED: "제출"}

_Y = r"(?<!\d)(?P<y>(?:19|20)\d{2})(?!\d)"
_YY = r"['’‘]?(?P<yy>\d{2})(?!\d)"
# 해와 '평가' 사이에 낱말 둘까지 허용한다. 그 사이에 '실적·성과'가 끼면 그 해는
# 실적의 해다 — "2025년도 실적 평가"의 2025는 평가가 시행된 해가 아니다. 띄어쓰기가 사라진
# PDF 글('2024년도구·군규제혁신실적을평가하여')에서도 그렇게 읽도록 글자마다 본다. 다만
# '실적평가'처럼 붙은 말은 평가의 이름이다('2025년 구·군 규제혁신 실적평가 계획'은 2025년 평가).
_BETWEEN = r"(?:(?:(?!(?:실적|성과)(?!평가))[가-힣A-Za-z·]){1,14}\s*){0,3}?"
_NOT_EVAL = r"(?:(?:(?!평가)[가-힣A-Za-z·]){1,14}\s*){0,2}?"
_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    # 표 머리의 '2025년 실적기간'은 그 칸의 해다. 실적 기간을 말하는 줄은 아래 이름표가 읽는다.
    (PERFORMANCE, re.compile(_Y + r"\s*년도?\s*(?:추진\s*)?(?:실적|성과)(?!\s*기간)")),
    (PERFORMANCE, re.compile(r"실적\s*(?:기준\s*|귀속\s*)?(?:연도|년도)\s*[:：]?\s*" + _Y)),
    # "2024년도 구·군 규제혁신 실적을 평가하여" — 평가하는 대상인 실적의 해
    (PERFORMANCE, re.compile(_Y + r"\s*년도?\s*" + _NOT_EVAL + r"(?:실적|성과)\s*(?:을|를)\s*평가")),
    (EVALUATION, re.compile(_Y + r"\s*년도?\s*" + _BETWEEN + r"평가")),
    (EVALUATION, re.compile(r"평가\s*(?:시행\s*)?(?:연도|년도)\s*[:：]?\s*" + _Y)),
    (SUBMITTED, re.compile(r"제출\s*일자?\s*[:：]\s*" + _Y)),
)
# 이름표로 적은 실적 기간과 '(25실적)'은 실적의 해를 가장 분명히 말한다. 이것이 있으면 같은
# 문서의 다른 '○○년 실적' 언급(비교표의 칸 머리 등)은 버린다.
_STRONG: tuple[tuple[str, re.Pattern], ...] = (
    (PERFORMANCE, re.compile(r"(?:\(\s*실적\s*기간\s*\)\s*[:：]?|실적\s*기간\s*[:：])\s*(?:" + _Y + "|" + _YY
                             + r")\s*[.년]")),
    (PERFORMANCE, re.compile(r"\(\s*" + _YY + r"\s*년?\s*실적\s*\)")),
)

# 규칙이 바뀌면 올린다. 모든 문서의 해를 한 번 다시 읽는다(사람이 정한 해는 그대로).
# 2: '실적을 평가'의 해는 실적, 이름표의 실적 기간과 '(25실적)'이 흐린 언급을 이김, 폴더 이름도 읽음.
VERSION = "2"
HEAD_SECTIONS = 8
HEAD_CHARS = 4000
_RAW_SPAN = 30


@dataclass(frozen=True)
class YearMark:
    kind: str
    year: int
    raw: str
    locator: str
    strong: bool = False     # 이름표로 적은 실적 기간처럼 그 갈래를 분명히 말하는 것


def read(text: str, locator: str = "") -> list[YearMark]:
    """글에서 실적·평가·제출의 해를 읽는다."""
    marks: list[YearMark] = []
    seen: set[tuple[str, int, bool]] = set()
    for strong, patterns in ((True, _STRONG), (False, _PATTERNS)):
        for kind, pattern in patterns:
            for match in pattern.finditer(text):
                groups = match.groupdict()
                year = int(groups["y"]) if groups.get("y") else 2000 + int(groups["yy"])
                if year < MIN_YEAR or (kind, year, strong) in seen:
                    continue
                seen.add((kind, year, strong))
                raw = " ".join(text[max(0, match.start() - 4):match.end() + _RAW_SPAN].split())
                marks.append(YearMark(kind, year, raw[:80], locator, strong))
    return marks


def collect(db, doc_id: int) -> list[YearMark]:
    row = db.document(doc_id)
    if row is None:
        return []
    marks: list[YearMark] = []
    if row["eff_year"] and row["eff_date_kind"] != "fs":
        marks.append(YearMark(AUTHORED, row["eff_year"], row["eff_date"] or "", ""))
    name = f"{Path(row['filename']).stem} {row['doc_title'] or ''}"
    marks.extend(read(name, "파일 이름"))
    # 문서가 든 폴더의 이름: '2026년(25실적) 지방규제혁신 성과평가'
    folder = Path(row["path"] or "").parent.name
    if folder:
        marks.extend(read(folder, "폴더 이름"))
    budget = HEAD_CHARS
    for section in db.con.execute(
            "SELECT locator, text FROM document_sections WHERE doc_id = ? ORDER BY ordinal LIMIT ?",
            (doc_id, HEAD_SECTIONS)):
        if budget <= 0:
            break
        marks.extend(read(section["text"][:budget], section["locator"]))
        budget -= len(section["text"])
    strong = {mark.kind for mark in marks if mark.strong}
    unique: dict[tuple[str, int], YearMark] = {}
    for mark in marks:
        if mark.kind in strong and not mark.strong:
            continue        # 분명한 것이 있으면 흐린 언급은 버린다
        unique.setdefault((mark.kind, mark.year), mark)
    return list(unique.values())


def mark_documents(db, doc_ids=None) -> int:
    """문서의 해를 읽어 저장한다. 해를 하나라도 읽은 문서 수를 돌려준다."""
    if doc_ids is None:
        doc_ids = [row["id"] for row in db.con.execute(
            "SELECT d.id FROM documents d JOIN sources s ON s.id = d.source_id "
            "AND s.kind != 'upload' WHERE d.parse_status IN ('ok', 'partial') "
            "AND d.missing_since IS NULL")]
    marked = 0
    with db.transaction():
        for doc_id in doc_ids:
            decided = {row["kind"] for row in db.con.execute(
                "SELECT DISTINCT kind FROM doc_years WHERE doc_id = ? AND decided_by = 'user'",
                (doc_id,))}
            db.con.execute("DELETE FROM doc_years WHERE doc_id = ? AND decided_by = 'ai'",
                           (doc_id,))
            found = [mark for mark in collect(db, doc_id) if mark.kind not in decided]
            for mark in found:
                db.con.execute(
                    "INSERT OR IGNORE INTO doc_years(doc_id, kind, year, raw, locator) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (doc_id, mark.kind, mark.year, mark.raw, mark.locator))
            marked += bool(found)
    return marked


def refresh_outdated(db) -> int:
    """읽는 규칙이 바뀌었으면 모든 문서의 해를 다시 읽는다. 다시 읽은 문서 수."""
    if db.get_meta("docyears_version") == VERSION:
        return 0
    marked = mark_documents(db)
    db.set_meta("docyears_version", VERSION)
    return marked


def set_year(db, doc_id: int, kind: str, year: int | None) -> None:
    """사람이 그 갈래의 해를 정한다. None이면 '이 문서에는 그 해가 없다'."""
    if kind not in KINDS:
        raise ValueError(f"없는 갈래입니다: {kind}")
    with db.transaction():
        db.con.execute("DELETE FROM doc_years WHERE doc_id = ? AND kind = ?", (doc_id, kind))
        if year is not None:
            db.con.execute(
                "INSERT INTO doc_years(doc_id, kind, year, raw, locator, decided_by) "
                "VALUES (?, ?, ?, '담당자가 정함', '', 'user')", (doc_id, kind, int(year)))
    db.audit("document.year", str(doc_id), detail=kind, result=str(year))


def years(db, doc_ids) -> dict[int, dict[str, list[int]]]:
    """문서별 {갈래: [해…]}."""
    ids = [int(i) for i in doc_ids]
    out: dict[int, dict[str, list[int]]] = {}
    if not ids:
        return out
    for row in db.con.execute(
            f"SELECT doc_id, kind, year FROM doc_years WHERE doc_id IN ({', '.join('?' * len(ids))}) "
            "ORDER BY year", ids):
        out.setdefault(row["doc_id"], {}).setdefault(row["kind"], []).append(row["year"])
    return out


def year_for(marks: dict[str, list[int]], *kinds: str) -> int | None:
    """앞의 갈래부터 보아 해가 하나로 정해지는 첫 갈래의 해. 둘 이상이면 정하지 않는다."""
    for kind in kinds:
        found = marks.get(kind) or []
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            return None
    return None
