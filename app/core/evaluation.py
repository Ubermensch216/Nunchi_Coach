"""평가 문서의 구조 — 어느 해의 평가가 어느 해의 실적을, 어떤 배점으로 보는가 (개선계획서 6.3, C10~C12).

평가 대응(W09)에서 틀리기 쉬운 것은 세 가지다.

  · 해가 셋이다. '2025년 구·군 규제혁신 실적평가 계획'은 2025년에 시행하는 평가이고
    실적은 2024년 것이다. 폴더의 '2026년(25실적)'도 같다. 한 해로 접으면 전년도 자료를 고를 때
    한 해씩 밀린다. 해는 docyears가 읽은 것을 쓴다(적힌 것만).
  · 초안이 있다. '2026년 구·군 규제혁신 평가(안)'은 확정본이 아니다. 확정본을 받기 전에는
    배점으로 자체 점수를 매기지 않는다.
  · 배점이 서로 맞지 않을 수 있다. 실제 초안에서 지표 '내부 추진체계 강화'의 배점은 16점인데
    세부 배점은 교육 3·포상 5·홍보 5·관심도 5로 18점이었다. 같은 문서에 '세부항목의 합산
    점수는 세부지표에 할당된 배점을 넘을 수 없음'이라는 상한 규정도 있다. 어느 쪽이 맞는지
    고르지 않고 두 근거를 나란히 보인다.

표는 한글(HWPX) 파서가 내는 행 조각('표 N · M행', 'k열: 값')을 읽는다. 모양이 맞지 않는 표는
읽지 않는다 — 배점을 짐작하지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from . import docyears, workstate as ws

EVALUATION = "evaluation"
SLOT_PREFIX = "평가 · "
CHECK_PREFIX = "배점 확인 · "

_NAME = re.compile(r"평가")
# 평가를 '하는 쪽'의 문서: 계획·지표·기준·편람, 그리고 초안 '평가(안)'. 우리가 낸 '실적평가표'는 아니다.
_EVAL_DOC = re.compile(r"평가\s*(?:계획|지표|기준|편람|매뉴얼)|평가\s*\(\s*안\s*\)")
_LATEST = 3
_DRAFT = re.compile(r"\(\s*안\s*\)|\[\s*안\s*\]|초안|\(案\)")
_TABLE_ROW = re.compile(r"표\s*(?P<table>\d+)\s*·\s*(?P<row>\d+)\s*행")
_CELL = re.compile(r"(?:^|\|\s*|\n)(?P<col>\d+)열:\s*(?P<value>.*?)(?=\s*\|\s*\d+열:|\n\d+열(?:\s*병합|:)|\Z)", re.S)
_POINTS = re.compile(r"^\s*(?P<n>\d+(?:\.\d+)?)\s*점?\s*$")
_POINTS_IN = re.compile(r"(?P<n>\d+(?:\.\d+)?)\s*점")
_ALLOTMENT = re.compile(r"^\s*(?:ㅇ|○|◦|□)\s*(?P<name>.+?)\(\s*(?P<n>\d+(?:\.\d+)?)\s*점\s*\)")
_TOTAL_ROW = re.compile(r"^(?:합계|총계|총점|계)$")
_CAP_RULE = re.compile(r"합산\s*점수.{0,30}배점.{0,15}넘을\s*수\s*없")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _number(value: float) -> str:
    return f"{value:g}"


@dataclass
class Check:
    indicator: str
    declared: float
    parts: list[tuple[str, float]]
    declared_at: str
    parts_at: str
    quote: str

    @property
    def total(self) -> float:
        return sum(n for _, n in self.parts)


@dataclass
class Reading:
    doc_id: int
    title: str
    evaluation_year: int | None = None
    performance_year: int | None = None
    draft: bool = False
    indicators: list[tuple[str, float]] = field(default_factory=list)
    declared_total: float | None = None
    total_at: str = ""
    checks: list[Check] = field(default_factory=list)
    cap_rule: tuple[str, str] | None = None        # (원문, 위치)

    @property
    def summed(self) -> float:
        return sum(n for _, n in self.indicators)


def is_evaluation(filename: str, title: str = "") -> bool:
    return bool(_EVAL_DOC.search(f"{filename} {title}"))


def _rows(sections) -> dict[int, list[tuple[int, dict[int, str], str]]]:
    """표 번호 → [(행 번호, {열: 값}, 위치)]. 행 조각의 첫 줄('첫 행: …')은 머리라 건너뛴다."""
    tables: dict[int, list] = {}
    for section in sections:
        where = _TABLE_ROW.search(section["locator"] or "")
        if where is None:
            continue
        text = "\n".join(line for line in (section["text"] or "").splitlines()
                         if not line.startswith("첫 행:"))
        cells = {int(m.group("col")): m.group("value").strip() for m in _CELL.finditer(text)}
        if cells:
            tables.setdefault(int(where.group("table")), []).append(
                (int(where.group("row")), cells, section["locator"]))
    return tables


def read(db, doc_id: int) -> Reading | None:
    document = db.document(doc_id)
    if document is None or not is_evaluation(document["filename"] or "", document["doc_title"] or ""):
        return None
    sections = db.con.execute(
        "SELECT locator, text FROM document_sections WHERE doc_id = ? ORDER BY ordinal", (doc_id,)).fetchall()
    marks = docyears.years(db, [doc_id]).get(doc_id, {})
    reading = Reading(doc_id, document["filename"] or "",
                      evaluation_year=docyears.year_for(marks, docyears.EVALUATION),
                      performance_year=docyears.year_for(marks, docyears.PERFORMANCE),
                      draft=bool(_DRAFT.search(f"{document['filename']} {document['doc_title'] or ''}")))
    for section in sections:
        for line in (section["text"] or "").splitlines():
            if _CAP_RULE.search(line):
                reading.cap_rule = (" ".join(line.split()).strip(" ▪-"), section["locator"])
                break
        if reading.cap_rule:
            break
    for rows in _rows(sections).values():
        _summary(rows, reading)
        _detail(rows, reading)
    return reading


def _summary(rows, reading: Reading) -> None:
    """지표 요약표: 머리에 '지표'와 '배점'이 있고, 행마다 배점 숫자가, 끝에 합계 행이 있다."""
    head = rows[0][1] if rows else {}
    names = {col: _norm(value) for col, value in head.items()}
    indicator = next((col for col, name in names.items() if "지표" in name and "세부" not in name), None)
    points = next((col for col, name in names.items() if name in ("배점", "배점(점)")), None)
    if indicator is None or points is None or reading.indicators:
        return
    for _, cells, locator in rows[1:]:
        value = _POINTS.match(cells.get(points, ""))
        if value is None:
            continue
        if any(_TOTAL_ROW.match(_norm(v)) for col, v in cells.items() if col != points):
            reading.declared_total, reading.total_at = float(value.group("n")), locator
            continue
        name = " ".join(cells.get(indicator, "").split())
        if name:
            reading.indicators.append((name, float(value.group("n"))))


def _detail(rows, reading: Reading) -> None:
    """지표 상세표: '지표' 행의 이름, '진단 내용' 행의 배점, '항목 설명' 행의 세부 배점(ㅇ …(N점))."""
    name, declared, declared_at, parts, parts_at, quote = "", None, "", [], "", ""
    for _, cells, locator in rows:
        label = _norm(cells.get(1, ""))
        if label == "지표" and cells.get(2):
            name = " ".join(cells[2].split())
        elif label == "진단내용":
            points = _POINTS_IN.search(cells.get(3, ""))
            if points and _POINTS.match(cells.get(3, "")):
                declared, declared_at = float(points.group("n")), locator
        elif label == "항목설명":
            lines = [line.strip() for line in cells.get(2, "").splitlines()]
            found = [(" ".join(m.group("name").split()), float(m.group("n")))
                     for m in map(_ALLOTMENT.match, lines) if m]
            if found:
                parts, parts_at = found, locator
                quote = " / ".join(line for line in lines if _ALLOTMENT.match(line))
    if declared is not None and len(parts) >= 2 and abs(sum(n for _, n in parts) - declared) > 1e-9:
        reading.checks.append(Check(name or "이름 모름", declared, parts, declared_at, parts_at, quote))


# ── 업무 항목으로 ───────────────────────────────────────────────────

def items(db, task_id: int, basis: date) -> list[dict]:
    """이 업무의 평가 문서마다 구조 한 줄과, 배점이 맞지 않는 곳마다 '자료가 서로 다름' 한 줄."""
    out = []
    seen: set[str] = set()
    for row in db.con.execute(
            "SELECT d.id, d.hash FROM task_docs td JOIN documents d ON d.id = td.doc_id "
            "WHERE td.task_id = ? AND d.missing_since IS NULL AND d.parse_status IN ('ok', 'partial') "
            "ORDER BY COALESCE(d.eff_year, 0) DESC, d.id", (task_id,)):
        reading = read(db, row["id"])
        if reading is None:
            continue
        stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", reading.title)
        key = f"{reading.evaluation_year}|{reading.performance_year}|{reading.draft}|{_norm(stem)}"
        if key in seen:
            continue        # 같은 평가계획의 한글·PDF 판
        seen.add(key)
        if len(seen) > _LATEST:
            break           # 최근 평가만. 지난 평가는 연도 비교에서 본다
        out.append(_structure_item(reading, basis))
        for check in reading.checks:
            out.append(_check_item(reading, check))
    return [item for item in out if item]


def _structure_item(reading: Reading, basis: date) -> dict:
    parts = []
    if reading.evaluation_year:
        parts.append(f"{reading.evaluation_year}년 평가")
    if reading.performance_year:
        parts.append(f"{reading.performance_year}년 실적")
    if reading.draft:
        parts.append("초안(안) — 확정본 아님")
    if reading.indicators:
        parts.append(f"지표 {len(reading.indicators)}개 배점 합 {_number(reading.summed)}점")
    if reading.declared_total is not None:
        same = reading.indicators and abs(reading.summed - reading.declared_total) < 1e-9
        parts.append(f"표의 합계 {_number(reading.declared_total)}점" + ("" if same or not reading.indicators else
                                                                    " (지표 배점 합과 다름)"))
    if not parts:
        return {}
    if reading.draft:
        applies = ws.UNKNOWN
    elif reading.evaluation_year and reading.evaluation_year < basis.year:
        applies = ws.PAST
    else:
        applies = ws.UNKNOWN
    evidence = [(reading.doc_id, reading.total_at, reading.title)]
    conflict = bool(reading.indicators and reading.declared_total is not None
                    and abs(reading.summed - reading.declared_total) > 1e-9)
    return {"kind": EVALUATION, "slot": SLOT_PREFIX + reading.title, "content": " · ".join(parts),
            # 해와 초안 여부는 문서 이름·본문에서 읽은 것이고, 배점은 표에 적힌 것이다.
            "basis_state": ws.CONFLICT if conflict else ws.INFERRED, "applicability": applies,
            "evidence": evidence}


def capped(reading: Reading, check: Check) -> bool:
    """세부 합이 배점을 넘고, 문서가 '넘을 수 없음'이라고 상한을 밝혔는가 — 그러면 배분이 아니라 상한이다."""
    return bool(reading.cap_rule) and check.total > check.declared


def _check_item(reading: Reading, check: Check) -> dict:
    sum_text = "+".join(_number(n) for _, n in check.parts)
    content = f"지표 배점 {_number(check.declared)}점 · 세부 배점 합 {_number(check.total)}점({sum_text})"
    if capped(reading, check):
        content += f" — 문서의 상한 규정대로라면 {_number(check.declared)}점이 상한"
    evidence = [(reading.doc_id, check.declared_at, f"{check.indicator} 배점 {_number(check.declared)}점"),
                (reading.doc_id, check.parts_at, check.quote)]
    if reading.cap_rule:
        evidence.append((reading.doc_id, reading.cap_rule[1], reading.cap_rule[0]))
    # 상한 규정이 설명하는 넘침은 문서가 스스로 말한 것이다. 설명이 없으면 자료가 서로 다르다.
    return {"kind": EVALUATION, "slot": f"{CHECK_PREFIX}{check.indicator}", "content": content,
            "basis_state": ws.INFERRED if capped(reading, check) else ws.CONFLICT,
            "applicability": ws.UNKNOWN if reading.draft else ws.PAST, "evidence": evidence}


def is_check(item) -> bool:
    return item["kind"] == EVALUATION and (item["slot"] or "").startswith(CHECK_PREFIX)
