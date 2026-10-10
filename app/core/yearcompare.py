"""연도별 비교 — 같은 업무의 해들을 같은 모양에 담아 칸끼리 견준다 (계획서 A3).

두 해의 보고서를 요약해서 견주지 않는다. 연도별로 같은 구조(지표 행·절차 단계·
항목 값·실적 요약)로 바꾼 뒤 칸을 대조한다. 지표 이름이 같아도 배점·산식이 달라졌을
수 있고, 그것이 이 업무에서 가장 중요한 변화이기 때문이다.

지키는 것
  · **'같음'을 함부로 말하지 않는다.** 이름이 같으면 짝으로 보되, 칸이 하나라도
    다르면 changed다. 한쪽에 칸이 비어 있어도 '적혀 있지 않음'으로 다르다고 적는다.
  · **자료가 모자라면 모자란다고 한다.** 한 해에 지표 표를 못 읽었으면 다른 해의
    지표가 전부 '추가'된 것이 아니라 그 영역이 insufficient다.
  · 이름이 비슷한 지표가 한쪽에서 없어지고 다른 쪽에서 생겼으면 둘을 같은 지표로
    짝짓지도, 각각 삭제·추가로 말하지도 않는다. uncertain으로 두고 사람이 정한다.
  · 모든 결론에 근거(문서·위치·원문)를 붙인다.
  · 과거 해의 값은 과거 해의 값이다. 대상 연도(올해)의 값은 올해 공식 문서가 있는
    영역에서만 채워지고, 없으면 비어 있다.
  · 업무에 묶이지 않은 후보 문서의 값은 읽지 않는다(core/continuity.py의 규칙과 같다).
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field

from . import compare as procedure_compare
from . import docrole, docyears, indicators, reconstruction as rc, versions

SAME, CHANGED, ADDED, REMOVED, UNCERTAIN = "same", "changed", "added", "removed", "uncertain"
OK, INSUFFICIENT = "ok", "insufficient"

STATUS_LABEL = {SAME: "같음", CHANGED: "달라짐", ADDED: "새로 생김", REMOVED: "보이지 않음",
                UNCERTAIN: "확인 필요"}
FIELD_LABEL = indicators.FIELD_LABEL | {"": ""}
COMPARED_FIELDS = (indicators.SCORE, indicators.DEFINITION, indicators.FORMULA, indicators.PROOF)
BLANK = "(적혀 있지 않음)"
SIMILAR_NAME = 0.7
_EXAMPLE = re.compile(r"^\s*\(?\s*(?:작성\s*예|예시)")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Ref:
    """결론의 근거 한 곳."""

    doc_id: int
    locator: str
    quote: str


@dataclass
class IndicatorChange:
    name: str
    status: str
    fields: list[tuple[str, str, str]] = field(default_factory=list)   # (칸, 이전, 이후)
    note: str = ""
    refs: list[Ref] = field(default_factory=list)


@dataclass
class ItemRow:
    kind: str
    label: str
    values: dict[int, list[str]]          # 연도 → 적힌 값들
    refs: list[Ref] = field(default_factory=list)

    @property
    def differs(self) -> bool:
        distinct = {tuple(values) for values in self.values.values()}
        return len(distinct) > 1


@dataclass
class Summary:
    """부서별 실적표의 해별 요약. 센 것만 적는다 — 인정 여부를 말하지 않는다."""

    year: int
    rows: int = 0
    departments: list[str] = field(default_factory=list)
    without_proof: int = 0
    refs: list[Ref] = field(default_factory=list)


@dataclass
class Pair:
    earlier: int
    later: int
    indicators: list[IndicatorChange] = field(default_factory=list)
    indicators_state: str = OK
    indicators_note: str = ""
    procedure: procedure_compare.Comparison | None = None
    procedure_state: str = OK
    procedure_note: str = ""
    items: list[ItemRow] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        tally = {status: 0 for status in STATUS_LABEL}
        for change in self.indicators:
            tally[change.status] += 1
        return tally


@dataclass
class Materials:
    """비교에 쓸 문서를 연도×역할로 놓은 것."""

    by_year: dict[int, dict[str, list[int]]] = field(default_factory=dict)
    undated: list[int] = field(default_factory=list)
    folded: dict[int, int] = field(default_factory=dict)       # 묶음의 다른 판 → 대표 판
    uncertain_groups: list[dict] = field(default_factory=list)

    def docs(self, year: int, *roles: str) -> list[int]:
        found = self.by_year.get(year, {})
        return [doc for role in roles for doc in found.get(role, [])]

    def years(self, *roles: str) -> list[int]:
        return sorted(year for year in self.by_year if self.docs(year, *roles))


# ── 재료 모으기 ─────────────────────────────────────────────────────

def materials(db, doc_ids) -> Materials:
    """문서들을 연도×역할로 놓는다. 판 묶음은 대표 판 하나로 접는다."""
    ids = list(dict.fromkeys(int(i) for i in doc_ids))
    out = Materials()
    heads = versions.representatives(db, ids)
    out.uncertain_groups = [group for group in versions.groups_for(db, ids)
                            if group["latest_doc_id"] is None]
    roles = docrole.roles(db, ids)
    marks = docyears.years(db, ids)
    seen: set[int] = set()
    for doc_id in ids:
        head = heads.get(doc_id, doc_id)
        if head != doc_id:
            out.folded[doc_id] = head
        if head in seen:
            continue
        seen.add(head)
        year = docyears.year_for(marks.get(head, {}), docyears.EVALUATION, docyears.AUTHORED)
        if year is None:
            out.undated.append(head)
            continue
        role = roles.get(head, {}).get("role", docrole.OTHER)
        out.by_year.setdefault(year, {}).setdefault(role, []).append(head)
    return out


def _clean(value: str) -> str:
    return _SPACE.sub(" ", value or "").strip()


# ── 지표 ────────────────────────────────────────────────────────────

def read_indicators(db, doc_ids) -> tuple[dict[str, indicators.Indicator], list[str]]:
    """한 해의 지표 행. (이름꼴 → 행, 한 해 안에서 값이 엇갈리는 이름들)."""
    rows: dict[str, indicators.Indicator] = {}
    conflicts: list[str] = []
    for doc_id in doc_ids:
        for row in indicators.read_document(db, doc_id):
            key = row.key()
            before = rows.get(key)
            if before is None:
                rows[key] = row
            elif _values(before) != _values(row):
                conflicts.append(row.name)
    return rows, conflicts


def _values(row: indicators.Indicator) -> tuple[str, ...]:
    return tuple(_clean(getattr(row, name)) for name in COMPARED_FIELDS)


def _ref(row: indicators.Indicator) -> Ref:
    return Ref(row.doc_id, row.locator, row.quote)


def compare_indicators(left: dict, right: dict) -> list[IndicatorChange]:
    out: list[IndicatorChange] = []
    removed = [key for key in left if key not in right]
    added = [key for key in right if key not in left]
    paired: dict[str, str] = {}
    for old in removed:
        best = max(added, key=lambda new: difflib.SequenceMatcher(None, old, new).ratio(),
                   default=None)
        if best and best not in paired.values() and \
                difflib.SequenceMatcher(None, old, best).ratio() >= SIMILAR_NAME:
            paired[old] = best

    for key, before in left.items():
        if key in paired:
            after = right[paired[key]]
            out.append(IndicatorChange(
                after.name, UNCERTAIN,
                note=f"이전 해의 '{before.name}'과 같은 지표인지 확인이 필요합니다 (이름이 비슷함)",
                refs=[_ref(before), _ref(after)]))
        elif key in right:
            after = right[key]
            diffs = [(name, _clean(getattr(before, name)) or BLANK, _clean(getattr(after, name)) or BLANK)
                     for name in COMPARED_FIELDS
                     if _clean(getattr(before, name)) != _clean(getattr(after, name))]
            out.append(IndicatorChange(after.name, CHANGED if diffs else SAME, diffs,
                                       refs=[_ref(before), _ref(after)]))
        else:
            out.append(IndicatorChange(before.name, REMOVED, refs=[_ref(before)]))
    for key in added:
        if key not in paired.values():
            out.append(IndicatorChange(right[key].name, ADDED, refs=[_ref(right[key])]))
    return out


# ── 처리 절차 ───────────────────────────────────────────────────────

def read_procedure(db, doc_ids) -> tuple[list[str], list[Ref]]:
    """시행문에 적힌 처리 절차를 단계로. 순서가 드러나지 않으면 빈 목록이다."""
    for doc_id in doc_ids:
        for found in _found(db, doc_id):
            if found.kind == rc.PROCEDURE:
                steps = rc.split_procedure(found.value)
                if steps:
                    return steps, [Ref(doc_id, found.locator, found.quote)]
    return [], []


def _found(db, doc_id: int) -> list[rc.Found]:
    row = db.document(doc_id)
    out: list[rc.Found] = []
    for section in db.con.execute(
            "SELECT locator, text FROM document_sections WHERE doc_id = ? ORDER BY ordinal", (doc_id,)):
        out.extend(rc.read_section(section["text"], doc_id, section["locator"],
                                   row["eff_year"] if row else None))
    return out


def compare_procedure(earlier: int, left: list[str], later: int, right: list[str]):
    rows = lambda steps: [{"label": label, "month": None, "day_hint": "", "ordinal": index}
                          for index, label in enumerate(steps, 1)]
    return procedure_compare.compare(earlier, rows(left), later, rows(right))


# ── 항목 값 ─────────────────────────────────────────────────────────

ITEM_KINDS = (rc.DEADLINE, rc.RECIPIENT, rc.FORM, rc.PURPOSE, rc.INPUT)


def read_items(db, years: list[int], mat: Materials) -> list[ItemRow]:
    """해마다 시행문에 적힌 기한·제출처·서식 등. 값을 합치거나 고르지 않고 해별로 늘어놓는다."""
    table: dict[str, ItemRow] = {}
    for year in years:
        for doc_id in mat.docs(year, docrole.DIRECTIVE):
            for found in _found(db, doc_id):
                if found.kind not in ITEM_KINDS:
                    continue
                row = table.setdefault(found.kind, ItemRow(found.kind, rc.KIND_LABEL[found.kind], {}))
                values = row.values.setdefault(year, [])
                if found.value not in values:
                    values.append(found.value)
                    row.refs.append(Ref(doc_id, found.locator, found.quote))
    return [table[kind] for kind in ITEM_KINDS if kind in table]


# ── 부서별 실적 요약 ────────────────────────────────────────────────

def summarize_performance(db, year: int, mat: Materials) -> Summary:
    summary = Summary(year)
    departments: list[str] = []
    for doc_id in mat.docs(year, docrole.PERFORMANCE):
        for section in db.con.execute(
                "SELECT kind, locator, text, meta FROM document_sections WHERE doc_id = ? "
                "ORDER BY ordinal", (doc_id,)):
            header = json.loads(section["meta"] or "{}").get("header")
            if section["kind"] != "sheet" or not header:
                continue
            names = header.split("\t")
            dept = next((i for i, n in enumerate(names) if "부서" in n), None)
            proof = next((i for i, n in enumerate(names) if "증빙" in n), None)
            counted = 0
            for line in section["text"].splitlines():
                if not line.strip() or line.startswith("[") or line == header:
                    continue
                cells = line.split("\t")
                if dept is None or dept >= len(cells) or not cells[dept].strip() \
                        or _EXAMPLE.match(cells[dept]):
                    continue
                counted += 1
                if cells[dept].strip() not in departments:
                    departments.append(cells[dept].strip())
                if proof is not None and (proof >= len(cells) or not cells[proof].strip()):
                    summary.without_proof += 1
            if counted:
                summary.rows += counted
                summary.refs.append(Ref(doc_id, section["locator"], _clean(section["text"])[:200]))
    summary.departments = departments
    return summary


# ── 한꺼번에 ────────────────────────────────────────────────────────

@dataclass
class YearComparison:
    mat: Materials
    pairs: list[Pair]
    summaries: dict[int, Summary]
    years: list[int]
    unreadable_tables: list[int] = field(default_factory=list)   # 지표 표를 못 읽은 해
    conflicts: dict[int, list[str]] = field(default_factory=dict)
    procedures: dict[int, tuple[list[str], list[Ref]]] = field(default_factory=dict)


def build(db, doc_ids, target_year: int | None = None, include_target: bool = False) -> YearComparison:
    """연이은 해끼리 견준다. include_target이 거짓이면 대상 연도는 빼고 과거 해만 본다."""
    mat = materials(db, doc_ids)
    years = [y for y in mat.years(docrole.CRITERIA, docrole.DIRECTIVE, docrole.FORM,
                                  docrole.PERFORMANCE)
             if include_target or target_year is None or y < target_year]
    result = YearComparison(mat, [], {}, years)
    rows_by_year: dict[int, dict] = {}
    for year in years:
        rows, conflicts = read_indicators(db, mat.docs(year, docrole.CRITERIA))
        rows_by_year[year] = rows
        if conflicts:
            result.conflicts[year] = conflicts
        if mat.docs(year, docrole.CRITERIA) and not rows:
            result.unreadable_tables.append(year)
        result.summaries[year] = summarize_performance(db, year, mat)
    steps_by_year = {year: read_procedure(db, mat.docs(year, docrole.DIRECTIVE)) for year in years}
    items = read_items(db, years, mat)

    for earlier, later in zip(years, years[1:]):
        pair = Pair(earlier, later, items=[row for row in items
                                           if earlier in row.values or later in row.values])
        left, right = rows_by_year[earlier], rows_by_year[later]
        if left and right:
            pair.indicators = compare_indicators(left, right)
        else:
            pair.indicators_state = INSUFFICIENT
            missing = [str(y) for y, rows in ((earlier, left), (later, right)) if not rows]
            pair.indicators_note = f"{', '.join(missing)}년의 지표 표를 읽지 못해 견주지 않았습니다"
        (left_steps, _), (right_steps, _) = steps_by_year[earlier], steps_by_year[later]
        if left_steps and right_steps:
            pair.procedure = compare_procedure(earlier, left_steps, later, right_steps)
        else:
            pair.procedure_state = INSUFFICIENT
            missing = [str(y) for y, s in ((earlier, left_steps), (later, right_steps)) if not s]
            pair.procedure_note = f"{', '.join(missing)}년의 처리 절차를 읽지 못해 견주지 않았습니다"
        result.pairs.append(pair)
    result.procedures = steps_by_year
    return result


# ── 지난 평가에서 지적된 것 ─────────────────────────────────────────

_FINDING = re.compile(r"인정되지|인정하지|불인정|미흡|보완|감점|지적|누락|반려|제외되")
_SENTENCE = re.compile(r"(?<=[다요음함됨])\.\s*|\n+")
FINDING_LIMIT = 6


def past_findings(db, mat: Materials, before: int | None = None) -> list[tuple[Ref, str]]:
    """결과 통보 문서에서 '인정되지 않았다' 꼴의 문장. 올해 점검 항목의 단서가 된다.

    문장을 요약하지 않고 적힌 그대로 둔다. 결과 문서가 말한 것이 지금도 같은 기준인지는
    알 수 없으므로 부르는 쪽이 '과거 지적사항'으로 표시해야 한다.
    """
    out: list[tuple[Ref, str]] = []
    for year in sorted(mat.by_year, reverse=True):
        if before is not None and year >= before:
            continue
        for doc_id in mat.docs(year, docrole.RESULT):
            for section in db.con.execute(
                    "SELECT locator, text FROM document_sections WHERE doc_id = ? ORDER BY ordinal",
                    (doc_id,)):
                for sentence in _SENTENCE.split(section["text"]):
                    sentence = _clean(sentence)
                    if len(sentence) >= 8 and _FINDING.search(sentence):
                        out.append((Ref(doc_id, section["locator"], sentence), sentence))
                        if len(out) >= FINDING_LIMIT:
                            return out
    return out
