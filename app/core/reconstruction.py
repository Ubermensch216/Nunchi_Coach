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

from . import docrole, korean, provenance, workstate as ws
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
OUTPUT = "output"
REGULATION = "regulation"
RISK = "risk"
AGENCY = "agency"

KIND_LABEL = {
    DEADLINE: "기한", AMOUNT: "금액", RECIPIENT: "제출처", PURPOSE: "목적",
    INPUT: "준비 자료", FORM: "서식", CONTACT: "담당·문의", PROCEDURE: "절차",
    OUTPUT: "산출물", REGULATION: "관련 규정", RISK: "유의·위험", AGENCY: "관계기관",
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
    OUTPUT: ("최종 산출물", "최종산출물", "산출물", "결과물"),
    REGULATION: ("관련 규정", "관련규정", "근거 규정", "근거규정", "관련 법령", "관련법령",
                 "근거 법령", "근거법령"),
    RISK: ("유의 사항", "유의사항", "주의 사항", "주의사항", "위험 요인", "위험요인", "리스크"),
    AGENCY: ("관계 기관", "관계기관", "유관 기관", "유관기관", "협조 기관", "협조기관"),
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
                 official: bool = False, title: str = "") -> list[Found]:
    """문서 조각 하나에서 이름표가 붙은 값과, 문장으로 적힌 일정·기한을 읽는다.

    title은 그 문서의 이름이다. '…까지 제출'처럼 무엇의 기한인지가 문장에 없을 때
    어느 문서의 기한인지로 구분하는 데 쓴다.
    """
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return []
    table = _read_table_row(lines, doc_id, locator, doc_year, official)
    if table is not None:
        return table

    found: list[Found] = []
    for position, line in enumerate(lines):
        matches = list(_LABEL_RE.finditer(line))
        if not matches:
            stated = _read_statements(line, doc_id, locator, doc_year, official)
            found.extend(stated)
            known = {item.key for item in stated if item.kind == DEADLINE}
            found.extend(item for item in _read_schedule(line, doc_id, locator, doc_year, official, title)
                         if item.key not in known)
        for index, match in enumerate(matches):
            last = index + 1 == len(matches)
            end = matches[index + 1].start() if not last else len(line)
            raw = line[match.end():end].strip(" .;,·-")
            quote = line[match.start():end].strip()
            if last and _KIND_OF[match.group("label")] == PROCEDURE:
                raw, more = _procedure_continuation(raw, lines[position + 1:])
                quote = " ".join([quote, *more])    # 값이 인용문 안에 있어야 한다
            label = match.group("label").replace(" ", "")
            item = _build(_KIND_OF[match.group("label")], label, raw, match.group("unit"),
                          quote, doc_id, locator, doc_year, official)
            if item is not None:
                found.append(item)
    return found


# ── 서술문으로 적힌 값 ──────────────────────────────────────────────
# '제출기한: 2026-11-10' 대신 '제출기한은 2026-11-10입니다.'처럼 문장으로 적은 공문도
# 같은 항목이다. 이름표(콜론) 줄에서는 읽지 않아 기존 동작은 그대로다. 값은 인용문
# 안에 그대로 있어야 하며(_verified) 문장 경계를 넘지 않는다.
_STATEMENT = re.compile(
    r"(?<![가-힣A-Za-z])(?P<label>"
    + "|".join(re.escape(label) for label in sorted(_KIND_OF, key=len, reverse=True))
    + r")\s*(?:은|는)\s*(?P<value>(?:(?![가-힣]\.(?:\s|$)).){1,120}?)\s*(?:입니다|이다)(?![가-힣])"
)
_STATEMENT_LABEL = re.compile(r"(?<![가-힣A-Za-z])(?:" + "|".join(
    re.escape(label) for label in sorted(_KIND_OF, key=len, reverse=True)) + r")\s*(?:은|는)")
_MONEY = re.compile(r"\d\s*(?:원|천원|만원|억원|백만원|KRW)|[원]\s*$")
_ORG_TAIL = r"(?:과|팀|부|처|국|실|청|원|소|단|센터|위원회|사무국|기관|부서)"
_SUBMIT_TO = re.compile(
    r"(?<![가-힣A-Za-z0-9])(?P<value>(?:[가-힣A-Za-z0-9]+\s)?[가-힣A-Za-z0-9]*" + _ORG_TAIL + r")\s*(?:에|으로)\s*"
    r"(?:제출|보고|회신|송부)\s*(?:합니다|한다|하여야 합니다|해야 합니다)"
)
_USES_FORM = re.compile(
    r"(?<![가-힣A-Za-z0-9\-])(?P<value>(?:[가-힣A-Za-z0-9\-]+\s)?[가-힣A-Za-z0-9\-]*(?:서식|양식|신청서))\s*(?:을|를)\s*"
    r"(?:사용|이용|작성)\s*(?:합니다|한다|하여야 합니다|해야 합니다)"
)


def _read_statements(line: str, doc_id: int, locator: str, doc_year: int | None,
                     official: bool) -> list[Found]:
    found: list[Found] = []
    for match in _STATEMENT.finditer(line):
        label = match.group("label").replace(" ", "")
        kind, value = _KIND_OF[match.group("label")], match.group("value").strip(" .;,·-")
        # 한 문장에 이름표가 둘이면('담당은 …이며 문의는 …입니다') 어디까지가 값인지 알 수 없다.
        # 금액은 통화 단위가 있어야 한다 — '총 3건'·'12명'은 금액이 아니다.
        if _STATEMENT_LABEL.search(value) or (kind == AMOUNT and not _MONEY.search(value)):
            continue
        item = _build(kind, label, value, None, match.group(0).strip(), doc_id, locator, doc_year, official)
        if item is not None:
            found.append(item)
    for pattern, kind, slot in ((_SUBMIT_TO, RECIPIENT, "제출처"), (_USES_FORM, FORM, "서식")):
        for match in pattern.finditer(line):
            item = _build(kind, slot, match.group("value").strip(), None, match.group(0).strip(),
                          doc_id, locator, doc_year, official)
            if item is not None:
                found.append(item)
    return found


# ── 문장으로 적힌 일정·기한 ─────────────────────────────────────────
# 계획서와 공문은 기한을 이름표로 적지 않는다. 실제 자료의 꼴은 둘이다.
#     우수공무원 추천(전부서) : 6. 17.(월) 한          ← 추진일정의 한 줄
#     …실행계획을 수립하여 2024. 6. 14.(금)까지 제출하여 주시기 바랍니다
# 이름표('제출기한:')만 읽었을 때 업무 184개에서 기한이 3건 나왔다. 후임자가 가장
# 먼저 묻는 것이 "언제까지 무엇을 하는가"인데 그 답이 여기 있다.
#
# 끝에 '한(限)·까지'가 붙은 날짜만 읽는다. '일시: 6. 28. 10:00'이나 '기간: 11. 1. ~
# 4. 30.'까지 읽으면 행사일과 공적 기간이 기한으로 올라온다.
SCHEDULE_PREFIX = "일정 · "
_WEEKDAY = r"(?:\s*\(\s*[월화수목금토일]\s*\))?"
_SHORT_YEAR = r"['’‘]\s*(?P<yy>\d{2})\s*[.년]\s*(?P<m2>1[0-2]|0?[1-9])\s*[.월]\s*(?P<d2>3[01]|[12]\d|0?[1-9])\s*[.일]?"
_DAY_ANY = r"(?:" + _FULL + r"\.?|" + _SHORT_YEAR + r"|(?P<m3>1[0-2]|0?[1-9])\s*(?:월\s*|\.\s*)(?P<d3>3[01]|[12]\d|0?[1-9])\s*[.일]?)"
_VAGUE_MONTH = r"(?P<vm>1[0-2]|0?[1-9])\s*월\s*(?P<vp>초순|중순|하순|초|중|말)?"
_SCHEDULE_LINE = re.compile(
    r"^[^\w가-힣(「『\[<]*(?P<what>[^:：]{2,45}?)\s*[:：]\s*(?P<lead>[^:：]{0,24}?)"
    r"(?P<when>" + _DAY_ANY + r"|" + _VAGUE_MONTH + r")" + _WEEKDAY
    + r"\s*(?:\d{1,2}\s*[:시]\s*\d{0,2}\s*분?\s*)?(?:한|限|까지)\s*[.)]?\s*$")
_UNTIL = re.compile(
    r"(?P<when>" + _DAY_ANY + r")" + _WEEKDAY + r"\s*(?:오전|오후)?\s*(?:\d{1,2}\s*시\s*)?까지"
    r"[^.。]{0,30}?(?P<verb>제출|회신|신청|추천|통보|보고|송부|등록|납부|접수|작성|입력)")
_TITLE_NOISE = re.compile(
    r"\([^()]*\)|\[[^\[\]]*\]|(?:19|20)\d{2}\s*(?:년도|년)?|\d{6,8}|제\s*\d+\s*(?:회|차)"
    r"|(?:진짜|최)*(?:최종|수정|결재전|결재)\d*|[_\-]+")


# 회의·행사가 열린 날. 기한은 아니지만 "언제 했나"의 가장 분명한 기록이다.
#     ❑ 일 시 : 2024. 6. 28.(금) 10:00
_EVENT_LINE = re.compile(
    r"^[^\w가-힣]*(?P<label>일\s*시|개최\s*일\s*시?|행사\s*일\s*시?|교육\s*일\s*시?|회의\s*일\s*시?)"
    r"\s*[:：]\s*(?P<when>" + _DAY_ANY + r")")
# 전임자가 캡처해 둔 공문 목록(온나라 문서함 화면)을 읽은 줄. 문서번호·보고일자·제목.
#     6423 | 2023.04.03 | 2023년 적극행정 중점과제 선정을 위한 자료 제출 요청 | …
# 그 일을 실제로 한 날짜들이다 — 지난 회차의 순서가 여기 그대로 있다.
_LEDGER_LINE = re.compile(
    r"^\s*\|?\s*\d{3,6}\s*\|\s*(?P<y>(?:19|20)\d{2})\s*[.\-]\s*(?P<m>1[0-2]|0?[1-9])\s*[.\-]\s*"
    r"(?P<d>3[01]|[12]\d|0?[1-9])\s*\|\s*(?P<title>[^|]{4,80}?)\s*(?:\||$)")
# 이름만 적힌 담당자 칸('김명진', '노정란 팀장'은 위에서 걸린다). 두세 글자 한글 한 낱말.
_KOREAN_NAME = re.compile(r"[가-힣]{2,4}(?:\s*[가-힣]{2,4})?")
_PERSON_OR_OFFICE = re.compile(
    r"\d{2,4}\s*[-)]\s*\d{3,4}\s*-\s*\d{4}|☎|내선|@|주무관|사무관|서기관|주사|팀장|계장|과장|국장|"
    r"실장|담당관|[가-힣]{2,}(?:과|팀|실|계|단|센터|담당관)(?![가-힣])")


def same_line(left: str, right: str, least: int = 5) -> bool:
    """두 일정 이름이 같은 일을 가리키는가 — 같은 글자를 least자 이상 이어서 품고 있는가.

    한 계획서가 같은 일정을 요약과 본문에 다른 말로 적는다('우수공무원 추천(전부서)',
    '우수공무원 부서 추천', '적극행정 사례 및 우수공무원 추천(전부서 및 전동)').
    """
    from difflib import SequenceMatcher

    a = re.sub(r"\s+", "", left.removeprefix(SCHEDULE_PREFIX))
    b = re.sub(r"\s+", "", right.removeprefix(SCHEDULE_PREFIX))
    if a == b:
        return True
    return SequenceMatcher(None, a, b, autojunk=False).find_longest_match(
        0, len(a), 0, len(b)).size >= least


def _read_schedule(line: str, doc_id: int, locator: str, doc_year: int | None,
                   official: bool, title: str) -> list[Found]:
    event = _EVENT_LINE.match(line)
    if event:
        label = re.sub(r"\s+", "", event.group("label"))
        slot = SCHEDULE_PREFIX + label + (f" · {_short_title(title)}" if title else "")
        item = _schedule_item(event, slot, line[:_MAX_VALUE * 2], doc_id, locator, doc_year, official)
        return [item] if item else []
    ledger = _LEDGER_LINE.match(line)
    if ledger:
        what = " ".join(ledger.group("title").split())
        item = _schedule_item(ledger, f"{SCHEDULE_PREFIX}기안 · {what[:40]}", line[:_MAX_VALUE * 2],
                              doc_id, locator, doc_year, official)
        return [item] if item else []
    whole = _SCHEDULE_LINE.match(line)
    if whole:
        what = whole.group("what").strip(" -·")
        item = _schedule_item(whole, f"{SCHEDULE_PREFIX}{what[:40]}", line, doc_id, locator,
                              doc_year, official)
        return [item] if item and len(what) >= 2 else []
    found = []
    for match in _UNTIL.finditer(line):
        slot = SCHEDULE_PREFIX + match.group("verb") + (f" · {_short_title(title)}" if title else "")
        item = _schedule_item(match, slot, line[:_MAX_VALUE * 2], doc_id, locator, doc_year, official)
        if item:
            found.append(item)
    return found


def _schedule_item(match: re.Match, slot: str, quote: str, doc_id: int, locator: str,
                   doc_year: int | None, official: bool) -> Found | None:
    groups = match.groupdict()
    year = month = day = None
    if groups.get("y"):
        year, month, day = int(groups["y"]), int(groups["m"]), int(groups["d"])
    elif groups.get("yy"):
        year, month, day = 2000 + int(groups["yy"]), int(groups["m2"]), int(groups["d2"])
    elif groups.get("m3"):
        month, day = int(groups["m3"]), int(groups["d3"])
    if month is None:
        if not groups.get("vm"):
            return None
        value = f"{int(groups['vm'])}월" + (f" {groups['vp']}" if groups.get("vp") else "")
        return Found(DEADLINE, slot, value, doc_id, locator, quote, doc_year, None, official)
    try:
        when = date(year or doc_year, month, day) if (year or doc_year) else None
        if when is None:
            date(2000, month, day)      # 2월 30일 같은 것은 날짜가 아니다
    except ValueError:
        return None
    # 문서에 연도가 적혀 있을 때만 연도를 보인다. 없는 연도를 써 넣지 않는다.
    value = when.isoformat() if year and when else f"{month}월 {day}일"
    return Found(DEADLINE, slot, value, doc_id, locator, quote, doc_year, when, official)


def _short_title(title: str) -> str:
    stem = title.rsplit(".", 1)[0] if "." in title[-6:] else title
    return " ".join(_TITLE_NOISE.sub(" ", stem).split())[:30]


def is_schedule(slot: str) -> bool:
    """문장으로 적힌 일정에서 읽은 항목인가(이름표 '제출기한:'으로 읽은 것과 구분)."""
    return (slot or "").startswith(SCHEDULE_PREFIX)


_STEP_LINE = re.compile(r"^\s*(?:[①-⑳]|\d{1,2}[.)]\s|(?:→|⇒|⇨|➜|➡|▶|->|=>))")
_MAX_PROCEDURE = 400


def _procedure_continuation(first: str, following: list[str]) -> tuple[str, list[str]]:
    """'절차:' 다음 줄들에 번호·동그라미 숫자·화살표로 이어 적힌 단계를 값에 잇는다.

    순서가 드러나는 줄만 잇는다. 이름표가 붙은 줄이나 순서 표시가 없는 줄에서 멈춘다 —
    그 뒤의 문장을 절차로 끌어오지 않는다.
    """
    taken: list[str] = []
    for line in following:
        if not _STEP_LINE.match(line) or _LABEL_RE.search(line):
            break
        taken.append(line)
    if not taken:
        return first, []
    return " ".join([first, *taken] if first else taken)[:_MAX_PROCEDURE], taken


def _read_table_row(lines, doc_id, locator, doc_year, official) -> list[Found] | None:
    """표 조각이면 머리글을 이름표 삼아 읽는다. 표가 아니면 None."""
    head = _TABLE_HEAD.match(lines[0])
    if head is None or len(lines) < 2:
        return None
    headers = [part.strip() for part in head.group(1).split(" | ")]
    cells = {int(number): value.strip() for number, value in _TABLE_CELL.findall(lines[1])}
    unit = next((m.group(1).strip() for m in map(_UNIT_LINE.match, lines[2:]) if m), "")
    row_name = _row_name(headers, cells)
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


_ROW_NAME_COLUMNS = 3


def _row_name(headers: list[str], cells: dict[int, str]) -> str:
    """표 행의 이름 — 첫 값 열 앞에 있는 열들을 잇는다.

    첫 열만 쓰면, 첫 열이 여러 행에 되풀이되는 표(정책사업 아래 세부사업 여럿)에서 서로 다른
    행의 값이 한 항목으로 합쳐져 가짜 '자료가 엇갈림'이 된다. 병합된 첫 열은 파서가 덮는
    모든 행에 같은 값을 채운다. 값 열이 두 번째 열이면 지금까지처럼 첫 열 하나다.
    """
    value_column = next((number for number, header in enumerate(headers, start=1)
                         if number > 1 and _kind_of_header(header) is not None), 0)
    last = value_column - 1 if value_column else 1
    names = [cells.get(number, "") for number in range(1, min(last, _ROW_NAME_COLUMNS) + 1)]
    return " / ".join(name for name in names if name)


def _kind_of_header(header: str) -> str | None:
    labeled = re.match(r"^(?P<name>[^()]+?)\s*(?:\((?P<unit>[^)]{1,12})\))?$", header or "")
    name = (labeled.group("name") if labeled else header or "").replace(" ", "")
    return next((kind for label, kind in _KIND_OF.items() if label.replace(" ", "") == name), None)


def _build(kind, slot, raw, unit, quote, doc_id, locator, doc_year, official) -> Found | None:
    raw = raw[:_MAX_PROCEDURE if kind == PROCEDURE else _MAX_VALUE].strip()
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
    elif kind == CONTACT:
        # 담당자 칸에는 사람·부서·전화가 적힌다. 표에서 '담당자 · 추진분야 / ◇ 선제적 해결이
        # 필요한 현안 …'처럼 과제 유형 문구가 들어온 것은 담당자가 아니다(실측).
        if _PERSON_OR_OFFICE.search(raw) is None and not _KOREAN_NAME.fullmatch(raw.strip()):
            return None
        if len(raw) > 60 or raw.lstrip().startswith(("◇", "○", "▷", "※", "-")):
            return None
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
    shown_slot: dict[tuple[str, str], str] = {}
    for item in found:
        # 같은 일정 줄이 한글 원본과 PDF 사본에서 띄어쓰기만 다르게 읽힌다.
        key = (item.kind, re.sub(r"\s+", "", item.slot) if is_schedule(item.slot) else item.slot)
        groups.setdefault(key, []).append(item)
        shown_slot.setdefault(key, item.slot)

    # 한 계획서가 같은 일정을 요약과 본문에 다른 말로 적는다. 날짜가 같고 이름이 비슷하면
    # 한 일정이다. 이름만 비슷하고 날짜가 다른 것('제출'과 '신청')은 따로 둔다.
    merged: dict[tuple[str, str], list[Found]] = {}
    for key, items in groups.items():
        if not is_schedule(shown_slot[key]):
            merged[key] = items
            continue
        dates = {item.key for item in items}
        twin = next((other for other in merged
                     if other[0] == key[0] and is_schedule(shown_slot[other])
                     and same_line(shown_slot[other], shown_slot[key])
                     and dates & {item.key for item in merged[other]}), None)
        if twin is None:
            merged[key] = items
        else:
            merged[twin] = merged[twin] + items
    groups = merged

    candidates = []
    for key, items in groups.items():
        kind, slot = key[0], shown_slot[key]
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
        # 추진일정의 같은 줄이 한 해에 두 번 나오는 것은 엇갈림이 아니다. 상·하반기에
        # 한 번씩 하는 일이다. 날짜순으로 나란히 보인다.
        schedule = is_schedule(slot)
        conflict = len(values) > 1 and not schedule
        shown = list(values.values())
        if schedule:
            shown.sort(key=lambda item: (item.when or date.max, item.value))
        shown = shown[:_MAX_CONFLICT_VALUES]

        candidates.append({
            "kind": kind, "slot": slot,
            "content": " / ".join(item.value for item in shown),
            "basis_state": ws.CONFLICT if conflict else ws.STATED,
            "applicability": _applicability(shown, newest, basis, conflict),
            "evidence": _evidence(current, shown),
            # 항목으로 올리지는 않지만 버리지도 않는다. 최근 문서라는 이유만으로
            # 지금 값을 확정한 것처럼 보이지 않게, 공백 처리에서 나란히 보여 준다.
            "earlier": _earlier(kind, items, newest, values, period),
        })
    candidates.sort(key=lambda c: (list(KIND_LABEL).index(c["kind"]), c["slot"]))
    return candidates


# 해마다 달라지는 것이 당연한 기한·금액은 '이전과 다름'으로 따지지 않는다.
_TRACK_CHANGES = (PROCEDURE, FORM, RECIPIENT, INPUT)
_MAX_EARLIER = 2


def _earlier(kind: str, items: list[Found], newest: int, values: dict, period) -> list[dict]:
    """가장 최근 시기와 값이 다른, 그 이전 시기의 값(가까운 시기부터)."""
    if kind not in _TRACK_CHANGES:
        return []
    older: dict[str, Found] = {}
    for item in sorted(items, key=lambda i: (-period(i), i.doc_id)):
        if 0 < period(item) < newest and item.key not in values:
            older.setdefault(item.key, item)
    return [{"value": item.value, "year": period(item), "doc_id": item.doc_id,
             "locator": item.locator, "quote": item.quote}
            for item in list(older.values())[:_MAX_EARLIER]]


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
    return choose(read_task(db, task_id), basis or date.today())


def schedule_dates(db, task_id: int) -> list[tuple[int, date]]:
    """이 업무의 문서에 적힌 일정·기한의 날짜들 (문서, 날짜).

    '언제 하는 일인가'의 가장 직접적인 근거다. 문서가 남은 달이 아니라 문서가
    스스로 말한 날짜이기 때문이다.
    """
    return [(item.doc_id, item.when) for item in read_task(db, task_id)
            if item.kind == DEADLINE and item.when is not None]


def read_task(db, task_id: int) -> list[Found]:
    """업무 하나의 문서들에서 읽어 낸 값 전부(고르기 전)."""
    documents = db.con.execute(
        "SELECT d.id, d.eff_year, d.filename FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "WHERE td.task_id = ? AND d.missing_since IS NULL "
        "  AND d.parse_status IN ('ok', 'partial') "
        # 같은 파일의 사본이 근거 자리를 여러 번 차지하지 않게 한다.
        "  AND (d.hash IS NULL OR d.id = (SELECT MIN(x.id) FROM task_docs tx "
        "       JOIN documents x ON x.id = tx.doc_id "
        "       WHERE tx.task_id = td.task_id AND x.hash = d.hash AND x.missing_since IS NULL)) "
        "ORDER BY d.id", (task_id,)).fetchall()
    # 남에게 받은 자료·참고자료·이전 판·홍보 결과물에 적힌 금액과 기한은 이 업무의
    # 것이 아니다. 실제 자료에서 부서가 낸 우수사례의 사업비(34억, 205억)가 이 업무의
    # '총사업비'로 올라와 서로 엇갈린다고 표시됐다.
    from . import foldersteps

    outside = foldersteps.outside_the_work(db, [row["id"] for row in documents])
    documents = [row for row in documents if row["id"] not in outside]
    if not documents:
        return []
    official = provenance.official_sources(db, [row["id"] for row in documents])

    marks = ", ".join("?" * len(documents))
    roles = {row["doc_id"]: row["role"] for row in db.con.execute(
        f"SELECT doc_id, role FROM doc_roles WHERE doc_id IN ({marks})",
        [row["id"] for row in documents])}
    found: list[Found] = []
    for document in documents:
        mine = []
        for section in db.con.execute(
            "SELECT locator, text FROM document_sections WHERE doc_id = ? ORDER BY ordinal",
            (document["id"],),
        ):
            mine.extend(read_section(
                section["text"], document["id"], section["locator"], document["eff_year"],
                official=document["id"] in official, title=document["filename"] or ""))
        if roles.get(document["id"]) in (docrole.DIRECTIVE, docrole.CRITERIA):
            # 상급기관의 요청 공문·평가 기준에 적힌 담당자는 그 기관의 사람이다. 이 업무의
            # 담당자와 같은 칸에 섞이지 않게 어디서 온 것인지 밝힌다.
            for item in mine:
                if item.kind == CONTACT:
                    item.slot = f"{item.slot} · 공문을 보낸 쪽"
        found.extend(mine)
    return found


# ── 현재까지의 기록 (C03) ───────────────────────────────────────────
# "지금 어디까지 됐는가"는 문서만으로 확정할 수 없다. 말할 수 있는 것은
# '마지막으로 남은 기록이 언제의 무엇인가'까지다. 그 뒤에 자료가 없다는 것은
# 끝났다는 뜻도, 안 했다는 뜻도 아니다 — '이후 상태 미상'이다.

RECORD = "record"
KIND_LABEL[RECORD] = "현재까지의 기록"
# 문서에는 없고 사람만 아는 것. 자동 추출이 만들지 않는다 — 사람이 보탤 때만 생긴다.
NOTE = "note"
KIND_LABEL[NOTE] = "보충 설명"
# 이름표 없이 문장으로만 적힌 내용. 모델이 고르고 값은 원문 그대로다(core/statements.py).
STATEMENT = "statement"
KIND_LABEL[STATEMENT] = "문장에서 찾은 내용"
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
# 바깥으로 내는 단계. '통보'는 넣지 않는다 — 신청인에게 결과를 알리는 일이라
# 제출처·제출기한이 걸리는 단계가 아니다(시나리오 평가에서 잘못 걸렸다).
_SUBMITS = re.compile("제출|보고|회신|송부|납부")
# 받는 쪽의 기한. 우리가 내는 기한이 아니므로 내는 단계에 잇지 않는다.
_INTAKE_DEADLINE = re.compile("^(?:신청|접수)")


def closes_step(item: dict) -> bool:
    """이 항목이 '내는 단계'의 완료 조건인가(제출처, 그리고 우리가 지킬 기한)."""
    if item["kind"] == RECIPIENT:
        return True
    return item["kind"] == DEADLINE and not _INTAKE_DEADLINE.match(item["slot"])
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


def build_steps(db, task_id: int, items: list[dict], basis: date | None = None) -> list[dict]:
    """업무 항목 후보와 과거 처리 기록에서 수행 단계 후보를 만든다."""
    basis = basis or date.today()
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

    # 과거 사례는 **끝까지 간 회차**에서 가져온다 — 기준 연도보다 앞선 해 가운데
    # 처리 기록이 남은 가장 최근 해. 올해는 아직 진행 중이라 앞부분만 있고,
    # 그것을 사례로 내밀면 '다음에 무엇을 했는가'가 빠진다. 지난 회차가 없을
    # 때에만 올해 것을 쓴다.
    years = db.task_years(task_id)
    ordered = ([y for y in reversed(years) if y < basis.year]
               + [y for y in reversed(years) if y >= basis.year])
    for year in ordered:
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
            if not closes_step(item):
                continue
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


def restore(db, basis: date | None = None, should_stop=lambda: False,
            client=None, statements: bool = True) -> dict[str, int]:
    """모든 업무의 항목과 수행 단계를 자료에서 다시 읽어 맞춘다.

    전임자 정보 없이 시작한다. 시작 맥락이 아직 없으면 '자료로 시작'으로
    하나 만든다. 사람이 고치거나 확인한 것과 이미 수행한 단계는 저장 계층이 지킨다.

    client(생성 모델)를 주면 이름표 없이 문장으로만 적힌 내용도 찾는다. 주지
    않으면 규칙으로만 읽고, 예전에 모델로 찾아 둔 것은 문장이 그대로인 한 남긴다.
    """
    basis = basis or date.today()
    totals = {"tasks": 0, "items": 0, "added": 0, "updated": 0, "removed": 0,
              "recheck": 0, "conflicts": 0, "steps": 0, "stated_steps": 0, "gaps": 0,
              "statements": 0}
    # 공백 처리는 이 모듈의 읽기 규칙을 쓴다 — 서로 물리지 않게 여기서 들인다.
    from . import continuity, statements as statement_finder

    tasks = db.tasks()
    if not tasks:
        return totals
    context = db.work_context()
    context_id = context["id"] if context else db.start_work_context()
    common = (statement_finder.common_skeletons(db, [task["id"] for task in tasks])
              if statements else frozenset())

    for task in tasks:
        if should_stop():
            break
        candidates = extract_task(db, task["id"], basis)
        steps = build_steps(db, task["id"], candidates, basis)
        # 문장에서 찾은 내용은 항목으로만 보탠다. 절차·공백 계산에는 넣지 않는다.
        told = (statement_finder.find(db, task["id"], client, basis, common)
                if statements else [])
        totals["statements"] += len(told)
        with db.transaction():
            result = db.sync_task_items(
                task["id"], candidates + record_items(db, task["id"], basis) + told,
                context_id, extractor=EXTRACTOR)
            # '항목'은 문서에서 읽어 낸 것만 센다. 기록 요약은 항상 생기므로 세지 않는다.
            step_result = db.sync_work_steps(task["id"], steps, context_id)
            _link_stated_order(db, task["id"])
            # 항목과 단계가 정해진 뒤에 공백을 본다(C04).
            continuity.review_task(db, task["id"], basis, candidates)
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
