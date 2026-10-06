"""업무 복원 — 문서에 명시된 항목을 근거와 함께 읽어 낸다 (구축계획서 C02, §4.3).

후임자가 폴더를 다 읽고 나서야 알 수 있던 것들 — 언제까지, 얼마, 어디에,
무엇을 준비해서 — 을 문서에서 찾아 업무 항목으로 올린다. 사람이 먼저 입력해
주기를 기다리지 않는다.

규칙으로만 읽는다. 생성 모델을 부르지 않으므로 Ollama가 없어도 돌고, 같은
자료에서는 항상 같은 결과가 나온다.

지키는 것
  · **적힌 것만 올린다.** '제출기한: 2025-09-30'처럼 이름표가 붙은 값과,
    표의 머리글이 이름표 구실을 하는 값만 읽는다. 문맥으로 짐작하지 않는다.
  · **모든 값은 그 값이 적힌 대목을 근거로 갖는다.** 값이 인용문 안에 실제로
    있는지 다시 확인하고(출처 검증), 통과하지 못한 후보는 버린다.
  · **자료가 다르게 말하면 고르지 않는다.** 같은 시기의 문서들이 서로 다른
    값을 적었으면 '충돌'로 올리고 양쪽 근거를 함께 보인다. 온나라 등록본이
    섞여 있어도 등록본이라는 이유로 이기지 않는다 — 근거 순서만 앞선다.
  · **과거 기한을 현재 기한으로 만들지 않는다.** 기준일보다 앞선 날짜와
    지난 연도 문서의 값은 '과거 사례'다.
  · 지난 연도의 값은 최신 값과 섞지 않는다. 가장 최근 시기의 값만 항목으로
    올린다(과거 수행 사례는 처리 순서 쪽에서 다룬다).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from . import korean, provenance, workstate as ws
from .dating import _FULL
from .labeling import label_document

EXTRACTOR = "rule@1"

# 업무 항목의 종류
DEADLINE = "deadline"
AMOUNT = "amount"
RECIPIENT = "recipient"
PURPOSE = "purpose"
INPUT = "input"
FORM = "form"
CONTACT = "contact"
PROCEDURE = "procedure"

KIND_LABEL = {
    DEADLINE: "기한", AMOUNT: "금액", RECIPIENT: "제출처", PURPOSE: "목적",
    INPUT: "준비 자료", FORM: "서식", CONTACT: "담당·문의", PROCEDURE: "절차",
}

# 이름표 → 종류. 공문·계획서에서 실제로 쓰는 표기만 둔다. 넓히면 '기간'이나
# '내용'처럼 아무 데나 붙는 말이 항목으로 올라온다.
_LABELS: dict[str, tuple[str, ...]] = {
    DEADLINE: ("제출기한", "제출 기한", "회신기한", "회신 기한", "신청기한", "신청 기한",
               "접수기한", "접수 마감", "제출마감", "제출 마감", "보고기한", "보고 기한",
               "처리기한", "처리 기한", "납부기한", "납부 기한", "마감일", "마감", "납기", "기한"),
    AMOUNT: ("총사업비", "사업비", "소요예산", "예산액", "예산요구액", "요구액", "계약금액",
             "지급액", "정산액", "집행액", "예산", "금액"),
    RECIPIENT: ("제출처", "제출 대상", "제출기관", "제출 기관", "보고처", "회신처"),
    PURPOSE: ("추진 목적", "추진목적", "목적"),
    INPUT: ("준비 자료", "준비자료", "구비서류", "구비 서류", "제출서류", "제출 서류",
            "첨부서류", "첨부 서류", "필요 자료", "준비물"),
    FORM: ("사용 서식", "제출 서식", "제출서식", "서식", "양식"),
    CONTACT: ("담당자", "담당 부서", "담당부서", "문의처", "문의", "연락처", "협조 부서", "담당"),
    PROCEDURE: ("처리 절차", "처리절차", "처리 순서", "처리순서", "추진 절차", "추진절차",
                "진행 순서", "업무 절차", "절차"),
}
_KIND_OF = {label: kind for kind, labels in _LABELS.items() for label in labels}
# 긴 이름표부터 맞춘다 — '기한'이 '제출기한'의 뒷부분을 가로채지 않게.
_LABEL_RE = re.compile(
    r"(?<![가-힣A-Za-z])(?P<label>"
    + "|".join(re.escape(label) for label in sorted(_KIND_OF, key=len, reverse=True))
    + r")\s*(?:\((?P<unit>[^)]{1,12})\))?\s*[:：]\s*"
)
_FULL_DATE = re.compile(_FULL)
_MONTH_DAY = re.compile(
    r"(?<!\d)(?P<m>1[0-2]|0?[1-9])\s*(?:월\s*|[./]\s*)(?P<d>3[01]|[12]\d|0?[1-9])\s*일?(?!\d)")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_TABLE_HEAD = re.compile(r"^(?:첫 행|머리글):\s*(.+)$")
_TABLE_CELL = re.compile(r"(\d+)열:\s*(.*?)(?=\s\|\s\d+열:|$)")
_UNIT_LINE = re.compile(r"^단위:\s*(.+)$")
_MAX_VALUE = 160
_MAX_EVIDENCE = 5
_MAX_CONFLICT_VALUES = 4


@dataclass(slots=True)
class Found:
    """문서 한 곳에서 읽어 낸 값 하나."""

    kind: str
    slot: str
    value: str                 # 화면에 보일 값
    doc_id: int
    locator: str
    quote: str
    doc_year: int | None = None
    when: date | None = None   # 기한이면 그 날짜
    official: bool = False
    key: str = field(default="")   # 같은 값인지 견주는 정규형

    def __post_init__(self) -> None:
        if not self.key:
            self.key = self.when.isoformat() if self.when else _normalize(self.value)


def _normalize(text: str) -> str:
    return re.sub(r"[\s.,·]+", "", text or "")


# ── 한 조각에서 읽기 ────────────────────────────────────────────────

def read_section(text: str, doc_id: int, locator: str, doc_year: int | None = None,
                 official: bool = False) -> list[Found]:
    """문서 조각 하나에서 이름표가 붙은 값을 모두 읽는다."""
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return []
    table = _read_table_row(lines, doc_id, locator, doc_year, official)
    if table is not None:
        return table

    found: list[Found] = []
    for line in lines:
        matches = list(_LABEL_RE.finditer(line))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
            raw = line[match.end():end].strip(" .;,·-")
            label = match.group("label").replace(" ", "")
            item = _build(_KIND_OF[match.group("label")], label, raw, match.group("unit"),
                          line[match.start():end].strip(), doc_id, locator, doc_year, official)
            if item is not None:
                found.append(item)
    return found


def _read_table_row(lines, doc_id, locator, doc_year, official) -> list[Found] | None:
    """표 조각이면 머리글을 이름표 삼아 읽는다. 표가 아니면 None."""
    head = _TABLE_HEAD.match(lines[0])
    if head is None or len(lines) < 2:
        return None
    headers = [part.strip() for part in head.group(1).split(" | ")]
    cells = {int(number): value.strip() for number, value in _TABLE_CELL.findall(lines[1])}
    unit = next((m.group(1).strip() for m in map(_UNIT_LINE.match, lines[2:]) if m), "")
    row_name = cells.get(1, "")
    quote = " ".join(lines[:3])

    found: list[Found] = []
    for column, header in enumerate(headers, start=1):
        value = cells.get(column, "")
        if column == 1 or not header or not value:
            continue
        # 머리글 '예산(백만원)' → 이름표 '예산', 단위 '백만원'
        labeled = re.match(r"^(?P<name>[^()]+?)\s*(?:\((?P<unit>[^)]{1,12})\))?$", header)
        name = (labeled.group("name") if labeled else header).replace(" ", "")
        kind = next((kind for label, kind in _KIND_OF.items()
                     if label.replace(" ", "") == name), None)
        if kind is None:
            continue
        slot = f"{name} · {row_name}" if row_name else name
        item = _build(kind, slot, value, (labeled.group("unit") if labeled else None) or unit,
                      quote, doc_id, locator, doc_year, official)
        if item is not None:
            found.append(item)
    return found


def _build(kind, slot, raw, unit, quote, doc_id, locator, doc_year, official) -> Found | None:
    raw = raw[:_MAX_VALUE].strip()
    if not raw:
        return None
    when = None
    value = raw
    if kind == DEADLINE:
        when, value = _deadline(raw, doc_year)
        if value is None:
            return None            # 날짜가 없는 '기한: 별도 통보'는 기한 값이 아니다
    elif kind == AMOUNT:
        number = _NUMBER.search(raw)
        if number is None:
            return None            # 숫자 없는 '예산: 미정'은 금액이 아니다
        if unit and unit not in raw:
            value = f"{raw} {unit.strip()}"
    item = Found(kind, slot, value, doc_id, locator, quote, doc_year, when, official)
    return item if _verified(item, raw) else None


def _deadline(raw: str, doc_year: int | None) -> tuple[date | None, str | None]:
    """기한 문구에서 날짜를 읽는다. 연도가 없으면 채우지 않고 적힌 대로 둔다."""
    full = _FULL_DATE.search(raw)
    if full:
        try:
            when = date(int(full.group("y")), int(full.group("m")), int(full.group("d")))
        except ValueError:
            return None, None
        return when, when.isoformat()
    short = _MONTH_DAY.search(raw)
    if short is None:
        return None, None
    month, day = int(short.group("m")), int(short.group("d"))
    when = None
    if doc_year:
        # 견주는 데만 문서의 연도를 빌린다. 화면 값에는 없는 연도를 써 넣지 않는다.
        try:
            when = date(doc_year, month, day)
        except ValueError:
            return None, None
    return when, f"{month}월 {day}일"


def _verified(item: Found, raw: str) -> bool:
    """출처 검증 — 값이 인용문에 실제로 있는가.

    규칙으로 읽은 값은 구조상 통과한다. 이 검사는 읽는 규칙이 바뀌거나 다른
    추출기가 같은 통로로 후보를 넣을 때, 근거에 없는 값이 항목으로 올라가는
    것을 막는 마지막 문이다.
    """
    quote = _normalize(item.quote)
    if _normalize(raw) not in quote:
        return False
    return all(_normalize(number) in quote for number in _NUMBER.findall(raw))


# ── 업무 단위로 모으기 ──────────────────────────────────────────────

def choose(found: list[Found], basis: date) -> list[dict]:
    """읽어 낸 값들을 업무 항목 후보로 접는다. `sync_task_items`에 그대로 넘긴다."""
    groups: dict[tuple[str, str], list[Found]] = {}
    for item in found:
        groups.setdefault((item.kind, item.slot), []).append(item)

    candidates = []
    for (kind, slot), items in groups.items():
        # 가장 최근 시기의 값만 본다. 시기를 모르는 값은 가장 오래된 것으로 친다.
        def period(item: Found) -> int:
            return item.when.year if item.when else (item.doc_year or 0)
        newest = max(period(item) for item in items)
        current = [item for item in items if period(item) == newest]
        # 근거는 공식 등록본을 앞에 둔다. 값의 승패에는 쓰지 않는다.
        current.sort(key=lambda item: (not item.official, item.doc_id))

        values: dict[str, Found] = {}
        for item in current:
            values.setdefault(item.key, item)
        conflict = len(values) > 1
        shown = list(values.values())[:_MAX_CONFLICT_VALUES]

        candidates.append({
            "kind": kind, "slot": slot,
            "content": " / ".join(item.value for item in shown),
            "basis_state": ws.CONFLICT if conflict else ws.STATED,
            "applicability": _applicability(shown, newest, basis, conflict),
            "evidence": _evidence(current, shown),
        })
    candidates.sort(key=lambda c: (list(KIND_LABEL).index(c["kind"]), c["slot"]))
    return candidates


def _applicability(shown: list[Found], newest: int, basis: date, conflict: bool) -> str:
    dated = [item.when for item in shown if item.when]
    if dated and len(dated) == len(shown):
        if all(when < basis for when in dated):
            return ws.PAST
        # 자료가 엇갈리면 어느 날짜가 지금 것인지도 알 수 없다.
        return ws.UNKNOWN if conflict or any(when < basis for when in dated) else ws.CURRENT
    if newest and newest < basis.year:
        return ws.PAST
    return ws.UNKNOWN


def _evidence(current: list[Found], shown: list[Found]) -> list[tuple[int, str, str]]:
    """보이는 값마다 근거가 하나는 들어가게 한 뒤 남는 자리를 채운다."""
    picked: list[Found] = list(shown)
    for item in current:
        if len(picked) >= _MAX_EVIDENCE:
            break
        if all(item is not other and (item.doc_id, item.locator) != (other.doc_id, other.locator)
               for other in picked):
            picked.append(item)
    return [(item.doc_id, item.locator, item.quote) for item in picked[:_MAX_EVIDENCE]]


def extract_task(db, task_id: int, basis: date | None = None) -> list[dict]:
    """업무 하나의 문서들에서 항목 후보를 만든다. 저장하지 않는다."""
    basis = basis or date.today()
    documents = db.con.execute(
        "SELECT d.id, d.eff_year FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "WHERE td.task_id = ? AND d.missing_since IS NULL "
        "  AND d.parse_status IN ('ok', 'partial') "
        # 같은 파일의 사본이 근거 자리를 여러 번 차지하지 않게 한다.
        "  AND (d.hash IS NULL OR d.id = (SELECT MIN(x.id) FROM task_docs tx "
        "       JOIN documents x ON x.id = tx.doc_id "
        "       WHERE tx.task_id = td.task_id AND x.hash = d.hash AND x.missing_since IS NULL)) "
        "ORDER BY d.id", (task_id,)).fetchall()
    if not documents:
        return []
    official = provenance.official_sources(db, [row["id"] for row in documents])

    found: list[Found] = []
    for document in documents:
        for section in db.con.execute(
            "SELECT locator, text FROM document_sections WHERE doc_id = ? ORDER BY ordinal",
            (document["id"],),
        ):
            found.extend(read_section(
                section["text"], document["id"], section["locator"], document["eff_year"],
                official=document["id"] in official))
    return choose(found, basis)


# ── 현재까지의 기록 (C03) ───────────────────────────────────────────
# "지금 어디까지 됐는가"는 문서만으로 확정할 수 없다. 말할 수 있는 것은
# '마지막으로 남은 기록이 언제의 무엇인가'까지다. 그 뒤에 자료가 없다는 것은
# 끝났다는 뜻도, 안 했다는 뜻도 아니다 — '이후 상태 미상'이다.

RECORD = "record"
KIND_LABEL[RECORD] = "현재까지의 기록"
# 문서에는 없고 사람만 아는 것. 자동 추출이 만들지 않는다 — 사람이 보탤 때만 생긴다.
NOTE = "note"
KIND_LABEL[NOTE] = "보충 설명"
SLOT_LAST_RECORD = "마지막기록"
SLOT_AFTERWARDS = "이후상태"


def record_items(db, task_id: int, basis: date) -> list[dict]:
    """마지막 기록과 그 이후 상태를 업무 항목 후보로 만든다."""
    last = db.con.execute(
        "SELECT d.id, d.filename, d.eff_date, d.eff_precision, d.eff_year "
        "FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "WHERE td.task_id = ? AND d.missing_since IS NULL AND d.eff_date IS NOT NULL "
        # 파일 수정일뿐인 시점은 기록의 날짜로 쓰지 않는다 — 복사만 해도 바뀐다.
        "  AND d.eff_date_kind != 'fs' "
        "ORDER BY d.eff_date DESC, d.id DESC LIMIT 1", (task_id,)).fetchone()
    if last is None:
        return [{
            "kind": RECORD, "slot": SLOT_AFTERWARDS,
            "content": "시점을 알 수 있는 자료가 없어 마지막 기록을 정할 수 없습니다.",
            "basis_state": ws.MISSING, "applicability": ws.UNKNOWN, "evidence": [],
        }]

    when = _shown_date(last["eff_date"], last["eff_precision"])
    stage = label_document(last["filename"])
    if last["eff_year"] < basis.year:
        afterwards = (f"{basis.year}년 자료가 없습니다. 이번 회차를 시작했는지, "
                      f"어디까지 했는지는 자료로 알 수 없습니다.")
    else:
        afterwards = (f"{when} 이후의 자료가 없습니다. 그 뒤에 진행됐는지, "
                      f"끝났는지는 자료로 알 수 없습니다.")
    evidence = [(last["id"], "", last["filename"])]
    return [
        {"kind": RECORD, "slot": SLOT_LAST_RECORD,
         "content": f"{when} · {stage} — {last['filename']}",
         # 날짜와 단계 이름은 파일에서 추정한 것이다. 문서가 그렇게 선언한 것이 아니다.
         "basis_state": ws.INFERRED, "applicability": ws.PAST, "evidence": evidence},
        {"kind": RECORD, "slot": SLOT_AFTERWARDS, "content": afterwards,
         "basis_state": ws.MISSING, "applicability": ws.UNKNOWN, "evidence": evidence},
    ]


def _shown_date(value: str, precision: str | None) -> str:
    return {"year": value[:4], "month": value[:7]}.get(precision or "day", value)


# ── 수행 단계 (C03) ─────────────────────────────────────────────────
# 두 갈래를 섞지 않는다.
#   문서에 적힌 절차   '절차: A → B → C'. 순서가 문서에 있으므로 선행 관계를 준다.
#   과거 수행 사례     지난 회차의 문서를 날짜순으로 늘어놓은 것. 그렇게 했다는
#                      기록일 뿐이다. 앞선 날짜라고 반드시 먼저 해야 하는 단계가
#                      아니므로 선행 관계를 주지 않는다.

_ARROW = re.compile(r"\s*(?:→|⇒|⇨|➜|➡|▶|->|=>)\s*")
_CIRCLED = re.compile(r"\s*[①-⑳]\s*")
_NUMBERED = re.compile(r"(?:^|\s)(\d{1,2})[.)]\s+(?=\S)")
_SUBMITS = re.compile("제출|보고|회신|송부|통보|납부")
_MAX_STEP_LABEL = 80
PRECEDENT_BASE = 100   # 과거 사례는 적힌 절차 뒤에 따로 놓인다


def split_procedure(text: str) -> list[str]:
    """절차 문구를 단계로 나눈다. 순서가 드러나지 않으면 나누지 않는다([])."""
    text = " ".join((text or "").split())
    parts: list[str] = []
    if _ARROW.search(text):
        parts = _ARROW.split(text)
    elif len(_CIRCLED.findall(text)) >= 2:
        parts = _CIRCLED.split(text)
    else:
        marks = list(_NUMBERED.finditer(text))
        # 1부터 차례로 매긴 번호만 순서로 본다. '9. 30.' 같은 날짜를 단계로 읽지 않는다.
        if len(marks) >= 2 and [int(m.group(1)) for m in marks] == list(range(1, len(marks) + 1)):
            parts = [text[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(text))]
                     for i, m in enumerate(marks)]
    steps = [part.strip(" .;,·-")[:_MAX_STEP_LABEL] for part in parts]
    steps = [step for step in steps if step]
    return steps if len(steps) >= 2 else []


def build_steps(db, task_id: int, items: list[dict]) -> list[dict]:
    """업무 항목 후보와 과거 처리 기록에서 수행 단계 후보를 만든다."""
    by_kind: dict[str, list[dict]] = {}
    for item in items:
        by_kind.setdefault(item["kind"], []).append(item)

    steps: list[dict] = []
    procedure = next((item for item in by_kind.get(PROCEDURE, [])
                      # 절차가 엇갈리면 어느 쪽도 '적힌 절차'로 세우지 않는다.
                      if item["basis_state"] == ws.STATED), None)
    if procedure is not None:
        for number, label in enumerate(split_procedure(procedure["content"]), start=1):
            steps.append({
                "slot": f"stated-{number}", "label": label, "ordinal": number,
                "procedure_kind": ws.PROCEDURE_STATED, "basis_state": ws.STATED,
                "applicability": procedure["applicability"],
                "evidence": procedure["evidence"],
            })
    if steps:
        _attach(steps, by_kind.get(INPUT, []), "inputs", fallback=0)
        _attach(steps, by_kind.get(FORM, []), "forms", fallback=None)
        _closing(steps[-1], by_kind)

    # 처리 기록이 남아 있는 가장 최근 회차. 문서는 있는데 순서를 아직 못 세운
    # 해는 건너뛴다.
    for year in reversed(db.task_years(task_id)):
        rows = db.task_steps(task_id, year)
        if not rows:
            continue
        for row in rows:
            where = f"{year}년 {row['day_hint'] or (str(row['month']) + '월' if row['month'] else '')}"
            steps.append({
                "slot": f"precedent-{year}-{row['ordinal']}",
                "label": f"{row['label']} ({where.strip()})",
                "ordinal": PRECEDENT_BASE + row["ordinal"],
                "procedure_kind": ws.PROCEDURE_PRECEDENT,
                # 사람이 확정한 과거 순서라도 '그때 그렇게 했다'는 기록이다.
                "basis_state": ws.INFERRED, "applicability": ws.PAST,
                "task_step_id": row["id"],
                "output": row["filename"],
                "evidence": [(row["doc_id"], "", row["filename"])] if row["doc_id"] else [],
            })
        break
    return steps


def _attach(steps: list[dict], items: list[dict], field_name: str, fallback: int | None) -> None:
    """준비 자료·서식을 낱말이 겹치는 단계에 붙인다.

    겹치는 단계가 없으면 준비 자료는 첫 단계에 둔다(시작 전에 갖춰야 한다).
    서식은 붙이지 않는다 — 어느 단계의 서식인지 자료가 말하지 않으면
    업무 항목으로만 남긴다.
    """
    for item in items:
        words = korean.tokens(item["content"])
        scores = [sum(1 for word in words if korean.contains(step["label"], word))
                  for step in steps]
        index = scores.index(max(scores)) if max(scores) else fallback
        if index is None:
            continue
        current = steps[index].get(field_name)
        steps[index][field_name] = f"{current}; {item['content']}" if current else item["content"]


def _closing(step: dict, by_kind: dict[str, list[dict]]) -> None:
    """마지막 단계가 내는 일이면, 어디에 언제까지인지를 완료 확인 방법으로 잇는다."""
    if not _SUBMITS.search(step["label"]):
        return
    parts = []
    for kind in (RECIPIENT, DEADLINE):
        for item in by_kind.get(kind, []):
            note = "" if item["basis_state"] == ws.STATED else f" ({ws.basis_label(item['basis_state'])})"
            if kind == DEADLINE and item["applicability"] == ws.PAST:
                note += " (과거 기한)"
            parts.append(f"{item['slot']}: {item['content']}{note}")
    if parts:
        step["done_criteria"] = " · ".join(parts)


def _link_stated_order(db, task_id: int) -> None:
    """문서에 적힌 절차에만 선행 관계를 준다. 사람이 손댄 단계는 건드리지 않는다."""
    previous = None
    for row in db.con.execute(
        "SELECT id, procedure_kind, origin, human_state FROM work_steps "
        "WHERE task_id = ? AND excluded = 0 ORDER BY ordinal, id", (task_id,)).fetchall():
        if row["procedure_kind"] != ws.PROCEDURE_STATED:
            if row["origin"] == "auto" and row["human_state"] == ws.NONE:
                db.con.execute(
                    "UPDATE work_steps SET after_step_id = NULL WHERE id = ?", (row["id"],))
            continue
        if row["origin"] == "auto" and row["human_state"] == ws.NONE:
            db.con.execute(
                "UPDATE work_steps SET after_step_id = ? WHERE id = ?", (previous, row["id"]))
        previous = row["id"]


def restore(db, basis: date | None = None, should_stop=lambda: False) -> dict[str, int]:
    """모든 업무의 항목과 수행 단계를 자료에서 다시 읽어 맞춘다.

    전임자 정보 없이 시작한다. 시작 맥락이 아직 없으면 '자료로 시작'으로
    하나 만든다. 사람이 고치거나 확인한 것과 이미 수행한 단계는 저장 계층이 지킨다.
    """
    basis = basis or date.today()
    totals = {"tasks": 0, "items": 0, "added": 0, "updated": 0, "removed": 0,
              "recheck": 0, "conflicts": 0, "steps": 0, "stated_steps": 0, "gaps": 0}
    # 공백 처리는 이 모듈의 읽기 규칙을 쓴다 — 서로 물리지 않게 여기서 들인다.
    from . import continuity

    tasks = db.tasks()
    if not tasks:
        return totals
    context = db.work_context()
    context_id = context["id"] if context else db.start_work_context()

    for task in tasks:
        if should_stop():
            break
        candidates = extract_task(db, task["id"], basis)
        steps = build_steps(db, task["id"], candidates)
        with db.transaction():
            result = db.sync_task_items(
                task["id"], candidates + record_items(db, task["id"], basis),
                context_id, extractor=EXTRACTOR)
            # '항목'은 문서에서 읽어 낸 것만 센다. 기록 요약은 항상 생기므로 세지 않는다.
            step_result = db.sync_work_steps(task["id"], steps, context_id)
            _link_stated_order(db, task["id"])
            # 항목과 단계가 정해진 뒤에 공백을 본다(C04).
            continuity.review_task(db, task["id"], basis)
        totals["gaps"] += len(db.info_gaps(task["id"]))
        totals["tasks"] += 1
        totals["items"] += len(candidates)
        totals["steps"] += len(steps)
        totals["stated_steps"] += sum(
            step["procedure_kind"] == ws.PROCEDURE_STATED for step in steps)
        totals["conflicts"] += sum(c["basis_state"] == ws.CONFLICT for c in candidates)
        for name in ("added", "updated", "removed", "recheck"):
            totals[name] += result[name]
        totals["recheck"] += step_result["recheck"]
    return totals
