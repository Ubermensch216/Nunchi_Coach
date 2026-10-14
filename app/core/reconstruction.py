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

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import PurePath

from . import authority, casedocs, docrole, folders, korean, provenance, workstate as ws
from .dating import _FULL
from .labeling import FALLBACK_LABEL, label_document

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
             "지급금액", "지급 금액", "지급액", "지 급 액", "정산액", "집행액",
             "회의참석수당 지급", "회의참석수당", "참석수당", "강사수당", "예산", "금액"),
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

# ── 누구의 무엇인가 (규제개혁 비교연구 R01·R02) ──────────────────────
# 규제 개선 건의서는 개선하려는 **규제의 현행 내용**을 '(현황)', '現)', '현행)' 뒤에 적는다.
#     ㅇ (현황) … 現) 경계조정 절차 : ① 개발행위(토지분할)허가 → ② 지적측량 → …
# 이 절차는 민원인이 밟는 허가 절차이지 규제개혁 담당자가 할 일이 아니다. 실제 자료에서
# 전국 취합표 한 칸의 이 문장이 '규제혁신 과제 제출' 업무의 '문서에 적힌 절차'가 되어
# "토지분할 허가 단계를 진행할 수 있습니다"라고 안내했다.
_DESCRIBES_RULE = re.compile(
    r"\(\s*(?:현\s*황|문\s*제\s*점|건의\s*내용|개선\s*(?:방안|내용|안)|기대\s*효과)[^()]{0,12}\)"
    r"|(?:現|현행|당초|개선\s*전)\s*\)")
# 괄호 안의 이름표는 문장 속 덧붙임이다 — '(제출서류: 외국인등록증 …)'는 다른 사람이 낼
# 서류를 설명한 것이지 이 업무의 준비물 목록이 아니다. 기한은 괄호로 적는 일이 흔해 남긴다
# ('실적 제출(기한: 9. 30.)').
_PAREN_SKIP_KINDS = frozenset({INPUT, FORM, PROCEDURE, RECIPIENT, CONTACT, PURPOSE, OUTPUT})
# 만료 기간의 주어. '적극행정위원회는 … 위촉기간이 23년 4월~26년 4월'
_SUBJECT = re.compile(r"[가-힣]{2,12}(?:위원회|협의회|자문단|심의회|위원단|추진단)")


def _about_the_rule(before: str, kind: str) -> bool:
    """이 이름표가 업무가 아니라 개선 대상 규제나 남의 서류를 설명하는 자리에 있는가."""
    if _DESCRIBES_RULE.search(before):
        return True
    return kind in _PAREN_SKIP_KINDS and before.count("(") > before.count(")")


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
    subject: str = ""          # 같은 줄이 밝힌 주어('적극행정위원회는 … 위촉기간이 …')

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
    for line in lines:
        expires = _EXPIRES.search(line)
        if expires:
            year = int(expires.group("ey"))
            year, month = (year + 2000 if year < 100 else year), int(expires.group("em"))
            what = re.sub(r"\s+", "", expires.group("what"))
            what = re.sub(r"^위[측족]", "위촉", what)      # 글자 인식의 오자
            # 한 쪽지가 두 위원회를 나란히 설명한다. 기간은 그 줄이 밝힌 위원회의 것이다(R02).
            subjects = _SUBJECT.findall(line[:expires.start()])
            found.append(Found(DEADLINE, EXPIRY_PREFIX + what, f"{year}년 {month}월", doc_id, locator,
                               line[:_MAX_VALUE * 2], doc_year, date(year, month, 1), official,
                               subject=subjects[-1] if subjects else ""))
    for position, line in enumerate(lines):
        matches = list(_LABEL_RE.finditer(line))
        if not matches:
            if _DESCRIBES_RULE.search(line):
                continue        # 개선하려는 규제의 현행 내용이다. 이 업무의 기한·제출처가 아니다
            stated = _read_statements(line, doc_id, locator, doc_year, official)
            found.extend(stated)
            known = {item.key for item in stated if item.kind == DEADLINE}
            found.extend(item for item in _read_schedule(line, doc_id, locator, doc_year, official, title)
                         if item.key not in known)
        for index, match in enumerate(matches):
            if _about_the_rule(line[:match.start()], _KIND_OF[match.group("label")]):
                continue
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
# 평가 안내문의 꼴: "'25. 1. 10.(금) 18시까지 행안부 공무원단체과로 공문 제출"
_SUBMIT_BY_LETTER = re.compile(
    r"(?<![가-힣A-Za-z0-9])(?P<value>(?:(?![가-힣A-Za-z0-9]*까지\s)[가-힣A-Za-z0-9]+\s)?"
    r"[가-힣A-Za-z0-9]*" + _ORG_TAIL + r")\s*(?:에|으로|로)\s*공문(?:으로)?\s*제출")
_NOT_ORG_HEAD = re.compile(r"^\S*(?:에|에서|까지|부터|이내|내에|후에|전에|으로|을|를|은|는|및|통해|통하여|거쳐)\s+")
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
    for pattern, kind, slot in ((_SUBMIT_TO, RECIPIENT, "제출처"), (_SUBMIT_BY_LETTER, RECIPIENT, "제출처"),
                                (_USES_FORM, FORM, "서식")):
        for match in pattern.finditer(line):
            value = match.group("value").strip()
            if kind == RECIPIENT:
                # 기관 이름 앞 낱말이 조사로 끝나면 앞 구절의 꼬리다: '30일 이내에 금융감독원에
                # 보고한다' → '이내에 금융감독원', '시를 통해 행안부로' → '통해 행안부'(R04).
                value = _NOT_ORG_HEAD.sub("", value)
            item = _build(kind, slot, value, None, match.group(0).strip(),
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
EVENT_PREFIX = SCHEDULE_PREFIX + "일시"
# 끝나는 날이 있는 사실: '위촉기간이 23년 4월 ~ 26년 4월까지(3년)', '임기 : 2023. 4. ~ 2026. 4.'
# 전임자가 떠난 뒤에 끝나는 것은 아무도 알려 주지 않는다. 끝나는 달을 기한으로 올린다.
EXPIRY_PREFIX = "만료 · "
_YEAR_MONTH = r"(?:19|20)?\d{2}\s*[년.]\s*\d{1,2}\s*[월.]?"
_EXPIRES = re.compile(
    r"(?P<what>임기|위[촉측족]\s*기간|유효\s*기간|계약\s*기간|협약\s*기간)[^\d]{0,12}" + _YEAR_MONTH
    + r"\s*[~∼\-–]\s*(?P<ey>(?:19|20)?\d{2})\s*[년.]\s*(?P<em>1[0-2]|0?[1-9])\s*[월.]?")
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
    r"^\s*(?:[^\s|\d]{1,2}\s*)?\|?\s*\d{3,6}\s*\|\s*(?P<y>(?:19|20)\d{2})\s*[.\-]\s*(?P<m>1[0-2]|0?[1-9])\s*[.\-]\s*"
    r"(?P<d>3[01]|[12]\d|0?[1-9])\s*(?:\||[\[('][^\s|]{0,3})\s*\|?\s*(?:\d\s+)?"
    r"(?P<title>[^|]{4,80}?)\s*(?:\||$)")
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


_LAW_NAME = re.compile(r"법|령|규칙|조례|규정|지침|고시|훈령|예규|협약|기준|「|제\s*\d+\s*조")


def _build(kind, slot, raw, unit, quote, doc_id, locator, doc_year, official) -> Found | None:
    raw = raw[:_MAX_PROCEDURE if kind == PROCEDURE else _MAX_VALUE].strip()
    if not raw:
        return None
    if kind != FORM and authority.is_placeholder(raw):
        return None                # 서식의 빈칸('0000과', '☎000-000-0000')은 값이 아니다
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
    elif kind == REGULATION and not _LAW_NAME.search(raw):
        return None                # '관련 법령 : 개정안>'처럼 법령 이름이 없는 것은 규정이 아니다
    elif kind == CONTACT:
        # 담당자 칸에는 사람·부서·전화가 적힌다. 표에서 '담당자 · 추진분야 / ◇ 선제적 해결이
        # 필요한 현안 …'처럼 과제 유형 문구가 들어온 것은 담당자가 아니다(실측).
        if _PERSON_OR_OFFICE.search(raw) is None and not _KOREAN_NAME.fullmatch(raw.strip()):
            return None
        if len(raw) > 60 or raw.lstrip().startswith(("◇", "○", "▷", "※", "-")):
            return None
    item = Found(kind, slot, value, doc_id, locator, quote, doc_year, when, official)
    if kind == AMOUNT:
        # 같은 금액에 붙은 꼬리말('513,000원 ※실비에 따라 변동 가능')로 엇갈림이 되지 않게
        # 수와 단위로 견준다.
        unit_word = re.match(r"\s*(천원|만원|백만원|억원|억|원)", raw[number.end():])
        item.key = number.group(0).replace(",", "") + (unit_word.group(1) if unit_word else _normalize(unit or ""))
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
        # 요청 공문을 보낸 곳이 여럿이면(중앙부처와 그것을 전달한 시) 엇갈림이 아니라 둘 다다.
        # 계획서마다 적힌 목적 문장도 엇갈림이 아니다 — 가장 최근 회차의 첫 계획서 것을 보인다.
        sender, purpose = slot == SENDER_SLOT, kind == PURPOSE and slot in (PURPOSE_SLOT, ASKED_SLOT)
        # 예산과목·협조 부서의 일·명단·결과물도 여럿이면 나란히 보일 것이지 엇갈림이 아니다.
        listing = (slot in (BUDGET_SLOT, ROSTER_SLOT, OUTPUT_SLOT) or slot.startswith(PARTNER_PREFIX))
        conflict = len(values) > 1 and not (schedule or sender or purpose or listing)
        shown = list(values.values())
        if schedule:
            shown.sort(key=lambda item: (item.when or date.max, item.value))
        shown = shown[:1 if purpose else _MAX_CONFLICT_VALUES]

        candidates.append({
            "kind": kind, "slot": slot,
            "content": " / ".join(item.value for item in shown),
            # 보낸 곳이 낼 곳이라는 것은 문서가 말한 것이 아니라 읽어 낸 것이다.
            "basis_state": ws.CONFLICT if conflict else (ws.INFERRED if sender else ws.STATED),
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
    candidates = choose(read_task(db, task_id), basis or date.today())
    from . import bundles

    if bundles.kind(db, task_id):
        # 일이 아닌 묶음에는 '이 일의 결과물'이 없다. 이름에 '송부'가 든 받은 파일이 걸린다.
        candidates = [item for item in candidates if item["kind"] != OUTPUT]
    # 다른 업무의 절차를 인용한 것은 이 업무의 절차 항목이 아니다(build_steps 참고).
    return [item for item in candidates
            if item["kind"] != PROCEDURE
            or not _belongs_elsewhere(db, task_id, split_procedure(item["content"]))]


def borrowed_procedure(db, task_id: int, related: dict, basis: date) -> list[dict]:
    """엮인 업무의 문서에 인용된 이 업무의 절차.

    위원회 심의자료에 적힌 선발 절차는 위원회의 절차가 아니라 선발의 절차다. 그쪽에서는
    떼어 냈으니 이쪽에 붙인다. 이 업무에 적힌 절차가 따로 없고, 엮인 업무의 문서에 적힌
    절차가 그 업무의 것이 아니라고 판정됐으며, 이 업무의 지난 단계와 절반 넘게 겹칠 때만.
    """
    own = db.con.execute("SELECT label FROM task_steps WHERE task_id = ? AND label IS NOT NULL",
                         (task_id,)).fetchall()
    text = " ".join(row["label"] for row in own)
    if not text:
        return []
    names = {row["id"]: row["name"] for row in db.con.execute("SELECT id, name FROM tasks")}
    for other in related.get(task_id, {}):
        for item in choose(read_task(db, other), basis):
            stated = split_procedure(item["content"]) if item["kind"] == PROCEDURE else []
            if not stated or item["basis_state"] != ws.STATED or not _belongs_elsewhere(db, other, stated):
                continue
            hits = sum(1 for step in stated
                       if any(word in text for word in _STEP_WORD.findall(step)
                              if word not in _COMMON_STEP_WORDS))
            if hits * 2 > len(stated):
                return [{**item, "slot": f"{item['slot']} · ‘{names.get(other, other)}’ 자료에서"}]
    return []


def schedule_dates(db, task_id: int) -> list[tuple[int, date]]:
    """이 업무의 문서에 적힌 일정·기한의 날짜들 (문서, 날짜).

    '언제 하는 일인가'의 가장 직접적인 근거다. 문서가 남은 달이 아니라 문서가
    스스로 말한 날짜이기 때문이다.
    """
    # 임기처럼 '끝나는 달'은 일하는 달이 아니다.
    return [(item.doc_id, item.when) for item in read_task(db, task_id)
            if item.kind == DEADLINE and item.when is not None
            and not item.slot.startswith(EXPIRY_PREFIX)]


_ASKED_FOR = re.compile(r"^일정 · (?:제출|회신|보고|송부|추천|신청)")


def request_deadlines(db, task_id: int) -> list[tuple[int, date]]:
    """받은 요청 공문·안내문이 적은 기한 (문서, 날짜).

    한 해 치 자료뿐이어도 "언제까지 내라"는 공문은 그 일이 언제 오는지를 말한다.
    우리가 다른 부서·위원에게 낸 요청의 기한은 세지 않는다 — 그것은 우리가 정한 날이다.
    받은 것인지는 접수 번호(보낸 부서가 우리와 다름)나, 참고·공문 폴더에 둔 것으로 안다.
    """
    from . import foldersteps

    founds = [item for item in read_task(db, task_id)
              if item.kind == DEADLINE and item.when is not None
              and (_ASKED_FOR.match(item.slot) or not is_schedule(item.slot))
              and not item.slot.startswith(EXPIRY_PREFIX)]
    ids = sorted({item.doc_id for item in founds})
    if not ids:
        return []
    marks = ", ".join("?" * len(ids))
    asked = {row["doc_id"] for row in db.con.execute(
        f"SELECT doc_id FROM doc_roles WHERE doc_id IN ({marks}) AND role IN (?, ?)",
        (*ids, docrole.DIRECTIVE, docrole.CRITERIA))}
    kept_aside = foldersteps.outside_the_work(db, ids, keep_requests=False)
    received = set()
    for doc_id in asked:
        if doc_id in kept_aside:
            received.add(doc_id)
            continue
        sections = db.con.execute(
            "SELECT locator, text FROM document_sections WHERE doc_id = ? ORDER BY ordinal", (doc_id,)).fetchall()
        issued = _ISSUED.search(" ".join(_flat(sections)))
        if issued and issued.group("dept") != issued.group("mine"):
            received.add(doc_id)
    return [(item.doc_id, item.when) for item in founds if item.doc_id in received]


def read_task(db, task_id: int) -> list[Found]:
    """업무 하나의 문서들에서 읽어 낸 값 전부(고르기 전)."""
    documents = db.con.execute(
        "SELECT d.id, d.eff_year, d.filename, d.path, s.path AS root "
        "FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "LEFT JOIN sources s ON s.id = d.source_id "
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

    every = [row["id"] for row in documents]
    # AI가 쓴 보고서·정기간행물은 아이디어의 출처일 뿐 업무의 목적·기한·제출처를 말할 자격이
    # 없다. 파일 연도가 더 최근이라는 이유로 결재된 계획을 이기기까지 했다(R03·R04).
    outside = foldersteps.outside_the_work(db, every) | set(authority.reference_only(db, every))
    # 참고 폴더에 넣어 둔 요청 공문·평가 기준은 기한과 담당을 읽으려고 남긴다. 금액은
    # 읽지 않는다 — 다른 위원회의 심사기준에 적힌 예산이 이 업무의 예산이 됐다(실측).
    kept_from_aside = foldersteps.outside_the_work(db, every, keep_requests=False) - outside
    documents = [row for row in documents if row["id"] not in outside]
    if not documents:
        return []
    official = provenance.official_sources(db, [row["id"] for row in documents])

    marks = ", ".join("?" * len(documents))
    roles = {row["doc_id"]: row["role"] for row in db.con.execute(
        f"SELECT doc_id, role FROM doc_roles WHERE doc_id IN ({marks})",
        [row["id"] for row in documents])}
    cases = casedocs.collections(db, [row["id"] for row in documents])
    ours = _own_office(db, [row["id"] for row in documents])
    whose = _task_subjects(db, task_id)
    found: list[Found] = []
    second: list[Found] = []     # 목적을 계획서에서 못 찾았을 때 쓸 것
    third: list[Found] = []
    for document in documents:
        if roles.get(document["id"]) == docrole.ROSTER:
            # 명단은 열어 볼 문서로만 가리킨다. 이름·전화·생년월일을 항목으로 옮겨 적지 않는다.
            found.append(Found(CONTACT, ROSTER_SLOT, document["filename"], document["id"], "",
                               document["filename"], document["eff_year"], None,
                               document["id"] in official))
            continue
        sections = db.con.execute(
            "SELECT locator, text, meta FROM document_sections WHERE doc_id = ? ORDER BY ordinal",
            (document["id"],)).fetchall()
        requested = roles.get(document["id"]) in (docrole.DIRECTIVE, docrole.CRITERIA)
        listed_sheets = _record_sheets(sections)
        # 문서의 목적은 첫머리에 적는다. 긴 보고서 77쪽의 '목적은 …이다'는 그 쪽에서 다루는
        # 남의 제도의 목적이다(R04). 문단 단위로 쪼갠 한글 문서는 조각 수가 많으므로 글자
        # 위치로 잰다.
        head_chars = max(_PURPOSE_HEAD_CHARS, sum(len(s["text"] or "") for s in sections) * _OVERVIEW_PART)
        offset = 0
        mine = []
        for index, section in enumerate(sections):
            far = offset > head_chars
            offset += len(section["text"] or "")
            if _sheet_of(section) in listed_sheets:
                continue        # 건별 목록의 한 행 — 그 건의 내용이지 이 업무의 값이 아니다(R01)
            read = read_section(
                section["text"], document["id"], section["locator"], document["eff_year"],
                official=document["id"] in official, title=document["filename"] or "")
            # 사업의 규모는 그 사업의 계획서가 앞머리(개요)에 적는다. 뒤쪽의 '참고',
            # '전년도 추진현황'에 나오는 사업비는 다른 사업의 것이다.
            late = index >= max(1, len(sections) * _OVERVIEW_PART)
            mine.extend(item for item in read
                        if not (late and item.kind == AMOUNT and _PROJECT_AMOUNT.match(item.slot))
                        and not (far and item.kind == PURPOSE)
                        and not (item.subject and _foreign(item.subject, whose)))
        # '사 업 비'처럼 띄어 적은 것까지 세어, 사업비가 여럿 적힌 문서면 여러 사업을 늘어놓은
        # 것이다(전년도 과제 현황). 그 가운데 하나를 이 업무의 사업비로 올리지 않는다.
        if len(_PROJECT_COST.findall(" ".join(_flat(sections)))) > 1:
            mine = [item for item in mine
                    if not (item.kind == AMOUNT and _PROJECT_AMOUNT.match(item.slot))]
        mine = _own_values(mine, case_collection=document["id"] in cases,
                           from_aside=document["id"] in kept_from_aside, requested=requested)
        role = roles.get(document["id"])
        if role == docrole.FORM:
            # 서식의 칸('제출기관 :', '담당자 :', '관련법령 :')은 채울 자리이거나 작성 예시다.
            # 이 업무의 제출처·담당자가 아니다(R07). 서식에 안내로 적어 둔 제출 기한은 남긴다.
            mine = [item for item in mine if item.kind in (FORM, DEADLINE)]
        if document["id"] not in cases:
            if role == docrole.DIRECTIVE:
                mine.extend(_sender(sections, document["id"], document["eff_year"],
                                    document["id"] in official))
            elif role == docrole.PLAN and document["id"] not in kept_from_aside:
                mine.extend(_purpose(sections, document["id"], document["eff_year"],
                                     document["id"] in official))
            elif role in (docrole.EXPENSE, docrole.RESULT) and document["id"] not in kept_from_aside:
                # 계획서가 없는 일은 우리가 낸 지급·보고 공문의 본문이 무슨 일인지 말한다.
                second.extend(_purpose(sections, document["id"], document["eff_year"],
                                       document["id"] in official))
            if role == docrole.DIRECTIVE and _ISSUED.search(" ".join(_flat(sections))):
                # 그것도 없으면 받은 요청 공문이 시킨 일이 이 업무다.
                third.extend(_purpose(sections, document["id"], document["eff_year"],
                                      document["id"] in official, _REQUESTED, ASKED_SLOT))
            if document["id"] not in kept_from_aside:
                mine.extend(_budget_lines(sections, document["id"], document["eff_year"],
                                          document["id"] in official))
            # 협조 부서는 우리가 쓴 계획·보고·지출 문서에서만 읽는다. 받은 공문·법령·증빙의
            # 괄호 안 부서는 그 문서를 쓴 쪽의 사정이다.
            if role in _OWN_PAPERS:
                mine.extend(_partners(sections, document["id"], document["eff_year"],
                                      document["id"] in official, ours))
        # 위원이 낸 의결서, 부서가 낸 서식처럼 '제출' 폴더에 든 남의 문서는 결과물이 아니다.
        if role in (docrole.DECISION, docrole.FORM, docrole.RECEIVED, docrole.ROSTER):
            pass
        elif role == docrole.SUBMISSION or folders.SUBMITTED in {
                folders.parse_part(name).modifier
                for name in folders.relative_parts(document["path"] or "", document["root"] or "")}:
            # 제출 폴더에 든 것, 제출본이라고 이름 붙은 것이 이 일의 결과물이다.
            mine.append(Found(OUTPUT, OUTPUT_SLOT, document["filename"], document["id"], "",
                              document["filename"], document["eff_year"], None,
                              document["id"] in official))
        if requested:
            # 상급기관의 요청 공문·평가 기준에 적힌 담당자는 그 기관의 사람이다. 이 업무의
            # 담당자와 같은 칸에 섞이지 않게 어디서 온 것인지 밝힌다.
            for item in mine:
                if item.kind == CONTACT:
                    item.slot = f"{item.slot} · 공문을 보낸 쪽"
        found.extend(mine)
    if not any(item.kind == PURPOSE for item in found):
        found.extend(second or third)
    return found


# ── 요청 공문을 보낸 곳, 계획서가 밝힌 목적 ─────────────────────────
# 실제 자료에서 제출처는 업무 13개 가운데 12개가 '자료에 없음'이었다. 공문은 '제출처:'라고
# 적지 않는다 — 요청한 곳이 곧 낼 곳이고, 그것은 공문 아래의 시행 번호에 있다.
#     시행 규제혁신추진단-4843 (2024. 6. 26.)  접수 기획실-10745 (2024. 6. 26.)
# 접수 번호가 있는(우리가 받은) 공문에서만 읽는다. 우리가 낸 공문의 시행 부서는 우리다.
SENDER_SLOT = "요청 공문을 보낸 곳"
PURPOSE_SLOT = "목적"
_ISSUED = re.compile(
    r"시행\s+(?P<dept>[가-힣A-Za-z·]{2,20})-\d+\s*(?:\(\s*[\d.\s]+\))?\s*"
    r"접수\s+(?P<mine>[가-힣A-Za-z·]{2,20})-\d+")
_ASKS = re.compile(r"제출|회신|추천하여|신청하여|신청해|통보하여")
_ORG_LINE = re.compile(r"^[가-힣]{2,12}(?:특별시|광역시|특별자치시|특별자치도|시|도|군|구|부|처|청|위원회)$")
# 계획서 첫머리의 목적 문장: '…공직문화를 조성하고자 함.'
_PURPOSE_END = re.compile(r"(?:하고자\s*(?:함|합니다|한다)|하기\s*위함|하려는\s*것임"
                          # "…실천을 유도하기 위한 ‘2024년 적극행정 교육 계획’임"
                          r"|위한\s[^.]{0,60}(?:계획|방안|것)[’'”」』]?\s*임)\.?$")
# 받은 요청 공문의 본문: "…안내하오니, 실적점검 자료를 '24. 7. 19.(금)까지 제출해 주시기 바랍니다."
_REQUESTED = re.compile(r"(?:제출|회신|추천|신청|통보)[^.]{0,20}(?:주시기|하여\s*주시기)\s*바랍니다\.?$")
ASKED_SLOT = "요청받은 일"
_PURPOSE_MIN, _PURPOSE_MAX = 25, 260


def _sender(sections, doc_id: int, doc_year: int | None, official: bool) -> list[Found]:
    lines = [" ".join(line.split()) for section in sections for line in (section["text"] or "").splitlines()]
    lines = [line for line in lines if line]
    whole = " ".join(lines)
    issued = _ISSUED.search(whole)
    if issued is None or issued.group("dept") == issued.group("mine"):
        return []
    # 무엇을 내라는 공문이어야 한다. 다른 부서가 계획을 알려 온 공문은 낼 곳을 말하지 않는다.
    if _ASKS.search(whole) is None:
        return []
    org = next((line.replace(" ", "") for line in lines[:8] if _ORG_LINE.match(line.replace(" ", ""))), "")
    value = f"{org} {issued.group('dept')}".strip()
    quote = " ".join(part for part in (org, " ".join(issued.group(0).split())) if part)
    return [Found(RECIPIENT, SENDER_SLOT, value, doc_id, sections[-1]["locator"] if sections else "",
                  quote, doc_year, None, official)]


def _purpose(sections, doc_id: int, doc_year: int | None, official: bool,
             ending: re.Pattern | None = None, slot: str = PURPOSE_SLOT) -> list[Found]:
    ending = ending or _PURPOSE_END
    overview = sections[:max(3, int(len(sections) * _OVERVIEW_PART))] if ending is _PURPOSE_END else sections
    for section in overview:
        for line in (section["text"] or "").splitlines():
            line = " ".join(line.split())
            if _PURPOSE_MIN <= len(line) <= _PURPOSE_MAX and ending.search(line):
                return [Found(PURPOSE, slot, line, doc_id, section["locator"], line,
                              doc_year, None, official)]
    return []


# ── 예산과목, 협조 부서, 명단, 결과물 ───────────────────────────────
# 사람이 인수인계하며 말해 주는 것들이다: 돈은 어느 과목에서 나가는가, 어느 부서와 무슨
# 일로 엮이는가, 명단은 어디 있는가, 끝나면 무엇이 남는가.
_OWN_PAPERS = (docrole.PLAN, docrole.RESULT, docrole.EXPENSE, docrole.MEETING, docrole.DECISION)
BUDGET_SLOT = "예산과목"
PARTNER_PREFIX = "협조 부서 · "
ROSTER_SLOT = "명단·연락망"
OUTPUT_SLOT = "결과물"
_BUDGET_LINE = re.compile(r"(?:예산|지출)\s*과목\s*[:：]\s*(?P<value>.{4,120})$")
_OFFICE = r"[가-힣]{2,8}(?:과|실|담당관|추진단)"
# 부서가 아닌 '…실'
_NOT_OFFICE = frozenset({"사무실", "회의실", "상황실", "민원실", "강의실", "대회의실", "중회의실", "소회의실"})
# 추진일정·절차의 괄호 안 부서: '인센티브 부여 요청(총무과) : 12월 초', '(소통감사실, 총무과)'
_IN_PARENS = re.compile(r"\((?P<offices>" + _OFFICE + r"(?:\s*[,·ㆍ]\s*" + _OFFICE + r")*)\)")
# 우리가 다른 부서에 낸 공문: '수신 재무과장'
_ADDRESSED = re.compile(r"수신\s+(?P<office>" + _OFFICE + r")장(?![가-힣])")
_OWN_NUMBER = re.compile(r"(?:등록번호|문서관리카드)\s*(?P<office>[가-힣]{2,10})-\d{2,6}")
_BULLET = re.compile(r"^[^\w가-힣(「『\[<]+")
_MAX_PARTNER_TASK = 40


def _flat(sections) -> list[str]:
    lines = [" ".join(line.split()) for section in sections for line in (section["text"] or "").splitlines()]
    return [line for line in lines if line]


def _budget_lines(sections, doc_id: int, doc_year: int | None, official: bool) -> list[Found]:
    """'예산과목 : 행정역량강화, 직원능력향상, …' — 쉼표로 끝나면 다음 줄까지가 과목이다."""
    lines = _flat(sections)
    for index, line in enumerate(lines):
        match = _BUDGET_LINE.search(line)
        if match is None:
            continue
        value, quote = match.group("value").strip(" .▷"), line
        if value.endswith(",") and index + 1 < len(lines):
            value, quote = f"{value} {lines[index + 1].strip(' .')}", f"{line} {lines[index + 1]}"
        return [Found(AMOUNT, BUDGET_SLOT, value[:_MAX_VALUE], doc_id, "", quote, doc_year, None, official,
                      key=_normalize(value))]
    return []


def _own_office(db, doc_ids) -> str:
    """이 문서들을 만든 부서 — 등록번호 앞에 가장 자주 나오는 부서 이름('기획실-16857')."""
    from collections import Counter

    ids = [int(i) for i in doc_ids]
    if not ids:
        return ""
    marks = ", ".join("?" * len(ids))
    counted: Counter = Counter()
    for row in db.con.execute(f"SELECT body FROM document_index WHERE doc_id IN ({marks})", ids):
        counted.update(m.group("office") for m in _OWN_NUMBER.finditer(" ".join((row["body"] or "").split())))
    return counted.most_common(1)[0][0] if counted else ""


def _partners(sections, doc_id: int, doc_year: int | None, official: bool, ours: str) -> list[Found]:
    """어느 부서와 무슨 일로 엮이는가. 값은 그 부서가 나온 줄의 일 이름이다."""
    lines = _flat(sections)
    found: list[Found] = []

    def add(office: str, what: str, quote: str) -> None:
        what = _BULLET.sub("", what).split(" : ")[0].split(":")[0].strip(" ·-")
        if office == ours or office in _NOT_OFFICE or not (2 <= len(what) <= _MAX_PARTNER_TASK):
            return
        if what.startswith(("붙임", "별첨", "수신", "참고")):
            return
        found.append(Found(AGENCY, PARTNER_PREFIX + office, what, doc_id, "", quote, doc_year, None, official))

    for line in lines:
        if "열:" in line:
            continue        # 표의 칸. 어느 일의 부서인지 줄만으로는 알 수 없다
        for match in _IN_PARENS.finditer(line):
            what = line[:match.start()]
            for office in re.split(r"\s*[,·ㆍ]\s*", match.group("offices")):
                add(office, what, line)
    whole = " ".join(lines)
    addressed = _ADDRESSED.search(whole)
    if addressed:
        title = re.search(r"제목\s+(?P<title>.{4,60}?)\s+\d\.\s", whole)
        if title:
            add(addressed.group("office"), title.group("title"), f"수신 {addressed.group('office')}장 제목 {title.group('title')}")
    return found


# 사업 하나의 규모를 말하는 이름표. 업무를 하는 데 드는 돈(소요예산·지급액)과 다르다.
_PROJECT_AMOUNT = re.compile(r"^(?:총사업비|사업비)(?:$|\s*·)")
_PROJECT_COST = re.compile(r"사\s*업\s*비\s*[:：]")
# 문서의 앞쪽 이만큼을 개요로 본다.
_OVERVIEW_PART = 0.4


def _amount_number(item: Found) -> str:
    number = _NUMBER.search(item.value)
    return number.group(0).replace(",", "") if number else item.key


def _own_values(found: list[Found], case_collection: bool = False, from_aside: bool = False,
                requested: bool = False) -> list[Found]:
    """이 문서에서 읽은 값 가운데 이 업무의 것이 아닌 금액·담당을 뺀다.

    못 가리면 내지 않는다. 실제 자료에서 금액 15건 가운데 14건이 부서 우수사례와
    중점과제의 사업비였고, 그것들끼리 '자료가 엇갈림'이 됐다. 틀린 값 열넷이 맞는 값
    하나를 가린다.
      · 남의 사례를 옮겨 실은 문서(추천 현황, 심의자료, 발표 자료)의 금액은 사례의 것이다.
        담당자도 그렇다 — 다만 요청 공문·평가 안내의 담당자는 문의할 곳이라 남긴다.
      · 참고 폴더에서 건진 문서의 금액은 읽지 않는다.
      · 한 문서가 같은 이름표로 서로 다른 금액을 여럿 적었으면 목록이다(사례마다의 사업비).
    표에서 읽은 값은 행 이름으로 이미 갈려 있어 목록 규칙을 적용하지 않는다.
    """
    if case_collection or from_aside:
        found = [item for item in found if item.kind != AMOUNT
                 and (item.kind != CONTACT or requested or not case_collection)]
    if case_collection:
        # 사례 안의 '일시: 2024. 3. 13.'은 사례가 연 행사의 날이지 이 업무의 일정이 아니다.
        found = [item for item in found if not item.slot.startswith(EVENT_PREFIX)]
    listed: dict[str, set[str]] = {}
    for item in found:
        if item.kind == AMOUNT:
            listed.setdefault(item.slot, set()).add(_amount_number(item))
    # 한 문서가 '목적:'·'절차:'·'제출처:'를 서로 다른 값으로 여러 번 적었으면 항목마다의
    # 설명을 늘어놓은 것이다(지표별 '목적:' 다섯 가지가 계획 업무의 목적이 됐다, R03).
    entries: dict[tuple[str, str], set[str]] = {}
    for item in found:
        if item.kind in _LISTED_KINDS:
            entries.setdefault((item.kind, item.slot), set()).add(item.key)
    return [item for item in found
            if (item.kind != AMOUNT or item.slot == BUDGET_SLOT or len(listed[item.slot]) == 1)
            and len(entries.get((item.kind, item.slot), ())) < _LISTED_ENTRIES]


_PURPOSE_HEAD_CHARS = 4000
_LISTED_KINDS = (PURPOSE, PROCEDURE, RECIPIENT)
_LISTED_ENTRIES = 3
# 건별 목록으로 보는 표의 행 수. 추진일정표·실적표는 이보다 짧다. 전국 건의과제 취합표는
# 수백 행이고, 한 행이 한 건이다.
_RECORD_ROWS = 30


def _sheet_of(section) -> str | None:
    """표 계산 문서의 조각이면 그 시트 이름."""
    try:
        meta = json.loads(section["meta"] or "{}") if "meta" in section.keys() else {}
    except (TypeError, ValueError):
        return None
    return meta.get("sheet") if isinstance(meta, dict) and meta.get("rows") else None


def _record_sheets(sections) -> set[str]:
    """한 행이 한 건인 긴 목록 시트. 그 칸의 '절차:'·'제출서류:'는 그 건의 것이다(R01)."""
    last: dict[str, int] = {}
    for section in sections:
        sheet = _sheet_of(section)
        if sheet is None:
            continue
        rows = json.loads(section["meta"])["rows"]
        last[sheet] = max(last.get(sheet, 0), int(rows[-1]))
    return {sheet for sheet, end in last.items() if end >= _RECORD_ROWS}


def _task_subjects(db, task_id: int) -> set[str]:
    """이 업무가 다루는 위원회·협의회 — 업무 이름과 문서가 든 폴더 이름에서."""
    task = db.task(task_id)
    text = [task["name"] if task else ""]
    text += [row["path"] or "" for row in db.con.execute(
        "SELECT d.path FROM task_docs td JOIN documents d ON d.id = td.doc_id WHERE td.task_id = ?",
        (task_id,))]
    places = text[:1] + [str(PurePath(path).parent) for path in text[1:]]
    return {re.sub(r"\s+", "", found) for place in places for found in _SUBJECT.findall(place)}


def _foreign(subject: str, ours: set[str]) -> bool:
    """줄이 밝힌 주어가 이 업무의 것과 다른 위원회인가.

    이 업무가 다루는 같은 종류의 기관이 있는데 그 어느 것과도 닮지 않았을 때만 남의 것이다.
    글자 인식의 오자('적극험정위원회' ↔ '적극행정위원회')는 같은 것으로 본다. 업무 쪽에 그런 기관이 없으면
    가릴 수 없으므로 남긴다.
    """
    from difflib import SequenceMatcher

    subject = re.sub(r"\s+", "", subject)
    tail = next((t for t in ("위원회", "협의회", "자문단", "심의회", "위원단", "추진단") if subject.endswith(t)), "")
    same_kind = [name for name in ours if name.endswith(tail)]
    if not same_kind:
        return False
    return not any(subject in name or name in subject
                   or SequenceMatcher(None, subject, name, autojunk=False).ratio() >= 0.7
                   for name in same_kind)


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
    from . import bundles

    bundled = bundles.kind(db, task_id)
    if bundled:
        # 일이 아닌 묶음이다. '이번 회차를 시작했는지'를 말하지 않고 무엇인지만 밝힌다.
        return [{"kind": RECORD, "slot": SLOT_LAST_RECORD,
                 "content": f"{bundles.NOTE[bundled]} 가장 최근 자료: {when} — {last['filename']}",
                 "basis_state": ws.INFERRED, "applicability": ws.PAST,
                 "evidence": [(last["id"], "", last["filename"])]}]
    empty_years = basis.year - last["eff_year"]
    cycle = db.task_cycle(task_id)
    if empty_years >= 2 and cycle is not None:
        # 해마다 하는 일인데 여러 해가 비었다. '올해 자료가 없다'보다 큰 일이다 — 그 사이의
        # 회차가 이 폴더에 통째로 없다.
        afterwards = (f"마지막 기록은 {last['eff_year']}년입니다. 반복하는 일인데 "
                      f"{last['eff_year'] + 1}년부터 {basis.year}년까지 {empty_years}개 연도의 자료가 "
                      f"없습니다. 그 사이의 회차를 했는지, 무엇이 바뀌었는지는 자료로 알 수 없습니다.")
    elif last["eff_year"] < basis.year:
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


_MAX_STEP_FILES = 4


def _step_folder(db, task_id: int, row) -> dict:
    """번호·날짜 폴더로 된 단계에 그 폴더의 문서를 갈라 붙인다 — 서식, 받은 자료, 만든 문서.

    전임자가 단계 폴더에 모아 둔 것이 곧 그 단계의 준비물과 결과물이다. 폴더가 단계가
    아닌(문서를 날짜순으로 늘어놓은) 단계에는 붙이지 않는다.
    """
    path = row["path"] if "path" in row.keys() else None
    if not path:
        return {}
    source = db.con.execute(
        "SELECT s.path FROM documents d JOIN sources s ON s.id = d.source_id WHERE d.id = ?",
        (row["doc_id"],)).fetchone()
    base = (source["path"] if source else "").rstrip(BACKSLASH)
    names = folders.relative_parts(path, base)
    parsed = [folders.parse_part(name) for name in names]
    skip = {folders.OLD, folders.BACKUP, folders.REFERENCE}
    at = next((i for i in range(len(parsed) - 1, -1, -1)
               if parsed[i].is_step and parsed[i].modifier not in skip), None)
    if at is None or not base:
        return {}
    prefix = BACKSLASH.join([base, *names[:at + 1]]) + BACKSLASH
    members = db.con.execute(
        "SELECT d.id, d.filename, d.path, r.role FROM task_docs td "
        "JOIN documents d ON d.id = td.doc_id LEFT JOIN doc_roles r ON r.doc_id = d.id "
        "WHERE td.task_id = ? AND d.missing_since IS NULL AND d.parse_status != 'skipped' "
        "ORDER BY d.filename", (task_id,)).fetchall()
    members = [m for m in members if (m["path"] or "").startswith(prefix)]
    aside = {folders.OLD, folders.BACKUP}
    forms, inputs, made = [], [], []
    for member in members:
        below = folders.relative_parts(member["path"], prefix.rstrip(BACKSLASH))
        kinds = {folders.parse_part(name).modifier for name in below}
        if kinds & aside:
            continue        # 이전 판
        if member["role"] == docrole.FORM:
            forms.append(member["filename"])
        elif (member["role"] in (docrole.RECEIVED, docrole.REFERENCE)
              or kinds & {folders.RECEIVED, folders.REFERENCE}):
            inputs.append(member["filename"])
        else:
            made.append(member["filename"])

    def listed(files: list[str]) -> str:
        files = list(dict.fromkeys(files))
        more = f" 외 {len(files) - _MAX_STEP_FILES}건" if len(files) > _MAX_STEP_FILES else ""
        return ", ".join(files[:_MAX_STEP_FILES]) + more

    out = {}
    if forms:
        out["forms"] = listed(forms)
    if inputs:
        out["inputs"] = listed(inputs)
    if made:
        out["output"] = listed(made)
    return out


BACKSLASH = chr(92)

# 이름 붙은 단계가 가장 많은 해의 이만큼은 돼야 끝까지 간 회차로 본다.
_WHOLE_ROUND = 0.75


def _named_steps(rows) -> int:
    """무슨 일인지 이름이 붙은 단계의 수. '문서 처리'는 파일이 있었다는 것뿐이다."""
    return sum(1 for row in rows if row["label"] and row["label"] != FALLBACK_LABEL)


_STEP_WORD = re.compile(r"[가-힣]{2,}")
# 어느 절차에나 나오는 말. 이것만 겹쳐서는 그 업무의 절차라고 할 수 없다.
_COMMON_STEP_WORDS = frozenset({"계획", "보고", "결과", "제출", "요청", "알림", "통보", "수립", "실시", "추진"})


def _belongs_elsewhere(db, task_id: int, stated: list[str]) -> bool:
    """문서에 적힌 절차가 이 업무가 아니라 다른 업무의 것인가.

    위원회 심의자료에는 심의할 안건의 절차가 인용돼 있다. 실제 자료에서 우수공무원
    선발 절차(추천 → 1차 심사평가 → 최종심사 → 인센티브)가 위원회 운영 업무의 '적힌
    절차'가 됐다. 지난 회차에 이 업무가 실제로 밟은 단계 이름과 견준다 — 이 업무의
    단계와는 한 군데도 겹치지 않고 다른 업무의 단계와 절반 넘게 겹치면 남의 절차다.
    지난 단계가 없는 업무(견줄 것이 없음)에서는 적힌 대로 둔다.
    """
    labels: dict[int, str] = {}
    for row in db.con.execute("SELECT task_id, label FROM task_steps WHERE label IS NOT NULL"):
        labels[row["task_id"]] = labels.get(row["task_id"], "") + " " + row["label"]
    if task_id not in labels:
        return False

    def hits(text: str) -> int:
        return sum(1 for step in stated
                   if any(word in text for word in _STEP_WORD.findall(step)
                          if word not in _COMMON_STEP_WORDS))

    if hits(labels[task_id]):
        return False
    return any(hits(text) * 2 > len(stated) for other, text in labels.items() if other != task_id)


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
    stated = split_procedure(procedure["content"]) if procedure is not None else []
    if stated and _belongs_elsewhere(db, task_id, stated):
        stated = []
    if stated:
        for number, label in enumerate(stated, start=1):
            steps.append({
                "slot": f"stated-{number}", "label": label, "ordinal": number,
                "procedure_kind": ws.PROCEDURE_STATED, "basis_state": ws.STATED,
                "applicability": procedure["applicability"],
                "evidence": procedure["evidence"],
            })
    if steps:
        # 준비 자료·서식은 그 절차가 적힌 문서에서 읽은 것만 단계에 붙인다. 다른 문서의
        # 준비물은 다른 건의 것일 수 있다 — 토지분할 절차에 다른 행의 외국인등록증이
        # 붙었다(R01). 붙이지 않은 것은 업무 항목으로 그대로 남는다.
        same = _doc_ids(procedure["evidence"])
        _attach(steps, [item for item in by_kind.get(INPUT, []) if _doc_ids(item["evidence"]) & same],
                "inputs", fallback=0)
        _attach(steps, [item for item in by_kind.get(FORM, []) if _doc_ids(item["evidence"]) & same],
                "forms", fallback=None)
        _closing(steps[-1], by_kind)

    # 과거 사례는 **끝까지 간 회차**에서 가져온다 — 기준 연도보다 앞선 해 가운데
    # 처리 기록이 남은 가장 최근 해. 올해는 아직 진행 중이라 앞부분만 있고,
    # 그것을 사례로 내밀면 '다음에 무엇을 했는가'가 빠진다. 지난 회차가 없을
    # 때에만 올해 것을 쓴다.
    #
    # 가장 최근 해가 곧 끝까지 간 회차는 아니다. 실제 자료에서 선발 업무는 2024년에
    # 아홉 단계가 남아 있는데 2025년의 발표자료 넉 장이 사례로 올라왔다. 이름이 붙은
    # 단계가 가장 많은 해에 견주어 크게 모자란 해는 조각 회차로 보고 건너뛴다.
    years = db.task_years(task_id)
    by_year = {year: db.task_steps(task_id, year) for year in years}
    past = [y for y in reversed(years) if y < basis.year and by_year[y]]
    fullest = max((_named_steps(by_year[y]) for y in past), default=0)
    whole = [y for y in past if _named_steps(by_year[y]) >= fullest * _WHOLE_ROUND]
    ordered = (whole + [y for y in past if y not in whole]
               + [y for y in reversed(years) if y >= basis.year])
    for year in ordered:
        rows = by_year[year]
        if not rows:
            continue
        for row in rows:
            where = f"{year}년 {row['day_hint'] or (str(row['month']) + '월' if row['month'] else '')}"
            around = _step_folder(db, task_id, row)
            steps.append({
                **{key: value for key, value in around.items() if key != "output"},
                "slot": f"precedent-{year}-{row['ordinal']}",
                "label": f"{row['label']} ({where.strip()})",
                "ordinal": PRECEDENT_BASE + row["ordinal"],
                "procedure_kind": ws.PROCEDURE_PRECEDENT,
                # 사람이 확정한 과거 순서라도 '그때 그렇게 했다'는 기록이다.
                "basis_state": ws.INFERRED, "applicability": ws.PAST,
                "task_step_id": row["id"],
                # 받은 자료만 든 단계(부서 제출)에는 우리가 만든 결과물이 없다.
                "output": around.get("output") or ("" if around else row["filename"]),
                "evidence": [(row["doc_id"], "", row["filename"])] if row["doc_id"] else [],
            })
        break
    return steps


def _doc_ids(evidence) -> set[int]:
    """근거 목록의 문서들. 후보는 (문서, 위치, 인용) 묶음이고 저장된 근거는 행이다."""
    return {proof[0] if isinstance(proof, (tuple, list)) else proof["doc_id"] for proof in evidence or []}


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
              "statements": 0, "matters": 0}
    # 공백 처리는 이 모듈의 읽기 규칙을 쓴다 — 서로 물리지 않게 여기서 들인다.
    from . import continuity, handover_notes, matters, relations, statements as statement_finder

    tasks = db.tasks()
    if not tasks:
        return totals
    # 건 목록을 먼저 세운다. 업무의 다음 행동이 '이번 회차 건 중 회신이 없는 것'을 묻는다.
    totals["matters"] = matters.rebuild(db, should_stop)["matters"]
    context = db.work_context()
    context_id = context["id"] if context else db.start_work_context()
    common = (statement_finder.common_skeletons(db, [task["id"] for task in tasks])
              if statements else frozenset())
    related = relations.find(db)

    for task in tasks:
        if should_stop():
            break
        candidates = extract_task(db, task["id"], basis)
        if not any(item["kind"] == PROCEDURE for item in candidates):
            candidates += borrowed_procedure(db, task["id"], related, basis)
        steps = build_steps(db, task["id"], candidates, basis)
        # 문장에서 찾은 내용은 항목으로만 보탠다. 절차·공백 계산에는 넣지 않는다.
        told = (statement_finder.find(db, task["id"], client, basis, common)
                if statements else [])
        totals["statements"] += len(told)
        with db.transaction():
            result = db.sync_task_items(
                task["id"], candidates + record_items(db, task["id"], basis)
                + handover_notes.items(db, task["id"], basis)
                + relations.items(db, task["id"], related, basis) + told,
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
