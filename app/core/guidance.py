"""안내문의 절차 — 누가 무엇을 하는가 (규제개혁 개선계획서 6.3, 비교연구 R08 T25).

상급기관 안내문은 절차를 '절차:' 이름표로 적지 않는다. 실제 꼴은 이렇다.

    □ 추진방법
      오류자료 추출 ➡ 지자체 규제정보 정비 ➡ 모니터링 ➡ 프로그램 개선
      ❶ (오류자료 추출) 법제처 API … 연계를 통해 오류자료 추출
      ❷ (지자체 규제정보 정비) 시도/새올 행정시스템을 통해 등록규제 일괄 정비
    □ 지자체 협조
      ○ 등록규제 파일(엑셀)에서 … 확인 및 수정, 조치결과, 미조치 사유 등 입력, 결과제출
    □ 추진일정
      ○ (지자체)시도/새올 시스템을 통한 등록규제 정비 추진('25. 7월 ~ '25. 8월)
      ○ (행안부)정비현황 모니터링(수시) 및 규제정보 검증('25. 8월~)

이 업무의 절차를 '찾지 못했다'고 하던 문서다. 단계마다 **하는 기관이 다르다** — 오류자료
추출과 모니터링은 행안부가, 정비는 우리가 한다. 모두를 우리 단계로 세우면 "모니터링 단계를
진행할 수 있습니다"라고 안내하게 된다(P0의 원칙: 남의 일을 우리 행동으로 바꾸지 않는다).

그래서 세 가지를 읽는다.
  · 절차의 단계 — 화살표로 이은 줄, 없으면 ❶·①·1) 번호 항목.
  · 단계를 하는 기관 — 단계 이름이나 번호 항목, 추진일정 줄의 '(기관)' 머리. 못 정하면 비워 둔다.
  · 우리에게 요청한 일 — '지자체 협조', '조치사항', '제출방법' 구획의 줄.

규칙으로만 읽는다. 문서에 그렇게 적혀 있을 때만 단계가 된다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import korean

OURS = "우리 기관"

_HEADING = re.compile(r"^\s*(?:□|■|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]\s*\.?)\s*(?P<name>\S.{0,40})$")
# '추진체계'는 대개 조직도다. 절차를 말하는 이름만 둔다.
_PROCEDURE_HEAD = re.compile(
    r"추진\s*(?:방법|절차|단계|흐름)|처리\s*(?:절차|순서|흐름)|업무\s*(?:절차|흐름)|진행\s*(?:절차|순서)"
    r"|작성\s*절차|정비\s*절차")
_SCHEDULE_HEAD = re.compile(r"추진\s*일정|세부\s*일정|향후\s*일정|^일\s*정$")
# 우리(기초 지자체)에게 하라고 적은 구획. '제출 방법'은 주민 신고 안내에도 있어 넣지 않는다.
_DUTY_FOR_US = re.compile(
    r"(?:지자체|자치단체|구\s*[·ㆍ]?\s*군|시\s*[·ㆍ]?\s*군\s*[·ㆍ]?\s*구|기초)\s*(?:협조|조치|이행|제출|요청)")
# 받는 쪽을 밝히지 않은 구획. 남에게서 받은 공문일 때만 우리에게 한 요청이다 — 우리 계획서의
# '부서별 협조사항'은 우리가 부서에 한 요청이다.
_DUTY_ANY = re.compile(r"협조\s*(?:사항|요청)|조치\s*사항|이행\s*사항|요청\s*사항")
# 요청받은 일이 아닌 줄: 다른 주체가 주어인 줄, 흐름(화살표), 연락처
_OTHER_SUBJECT = re.compile(r"^(?:소관\s*)?(?:각\s*)?(?:부서|실·과|실과|시\s*[·ㆍ]?\s*도|광역|\d+\s*개\s*시\s*[·ㆍ]?\s*도)")
_CONTACT = re.compile(r"@|☎|\d{2,4}\s*-\s*\d{3,4}\s*-\s*\d{4}|FAX|팩스")
_STEP_NOISE = re.compile(r"[*※]|\(\s*예시\s*\)")
_MIN_ARROW_STEPS = 3
_MAX_STEP_CHARS = 30
_ARROWS = re.compile(r"\s*(?:➡|→|⇒|⇨|➜|▶|->)\s*")
_NUMBERED = re.compile(r"^\s*(?P<mark>[❶-❿]|[①-⑳]|\d{1,2}\))\s*(?P<body>.+)$")
_LEAD = re.compile(r"^\(\s*(?P<lead>[^()]{1,30})\s*\)\s*(?P<rest>.*)$")
_BULLET = re.compile(r"^\s*(?:○|ㅇ|◦|-|•|▪)\s*(?P<body>.+)$")
_FOOTNOTE = re.compile(r"^\s*(?:\*|※)")
_MAX_STEPS = 12
_MAX_LABEL = 40

# 이 말이 하는 기관이면 우리다. 광역('시', '시·도')은 기초의 상급기관이므로 우리가 아니다.
_OUR_WORDS = re.compile(r"^(?:지자체|자치단체|지방자치단체|시\s*[·ㆍ]?\s*군\s*[·ㆍ]?\s*구|구\s*[·ㆍ]?\s*군|기초|각\s*기관|각\s*부서|우리)$")
_AGENCY_WORDS = re.compile(
    r"^(?:행안부|행정안전부|국조실|국무조정실|법제처|부처|중앙부처|소관부처|시|시\s*[·ㆍ]?\s*도|광역|도|"
    r"[가-힣]{1,8}(?:부|처|청|원|위원회|실))$")


@dataclass
class Step:
    label: str
    actor: str = ""          # OURS, 기관 이름, 또는 '' (모름)
    detail: str = ""
    locator: str = ""
    word: str = ""           # 문서가 쓴 기관 낱말('지자체', '행안부') — 단계 이름 앞에 그대로 붙인다


@dataclass
class Guidance:
    steps: list[Step] = field(default_factory=list)
    duties: list[tuple[str, str]] = field(default_factory=list)      # (줄, 위치)
    chain_locator: str = ""
    chain_quote: str = ""


def actor_of(word: str, own: str = "") -> str:
    """'(행안부)', '(지자체)' 같은 머리말이 하는 기관이면 그 기관을, 아니면 ''."""
    word = " ".join((word or "").split())
    if not word:
        return ""
    if _OUR_WORDS.match(word) or (own and own in word):
        return OURS
    if _AGENCY_WORDS.match(word):
        return word
    return ""


def _lines(sections) -> list[tuple[str, str]]:
    out = []
    for section in sections:
        for line in (section["text"] or "").splitlines():
            line = " ".join(line.split())
            if line:
                out.append((line, section["locator"]))
    return out


def read(sections, own: str = "", received: bool = False) -> Guidance:
    """문서 조각에서 안내 절차와 요청받은 일을 읽는다.

    received는 남에게서 받은 공문인가다. 받는 쪽을 밝히지 않은 '협조사항'은 그때만 우리 일이다.
    """
    found = Guidance()
    block = ""
    numbered: list[Step] = []
    schedule: list[tuple[str, str]] = []           # (하는 기관, 줄)
    for line, locator in _lines(sections):
        heading = _HEADING.match(line)
        if heading:
            name = heading.group("name")
            block = ("procedure" if _PROCEDURE_HEAD.search(name) else
                     "schedule" if _SCHEDULE_HEAD.search(name) else
                     "duty" if _DUTY_FOR_US.search(name) or (received and _DUTY_ANY.search(name)) else "")
            continue
        if block == "procedure":
            parts = [p.strip(" .") for p in _ARROWS.split(line) if p.strip(" .")]
            # 'A → B' 둘짜리는 대개 이름 바꿈·전후 비교다. 단계 이름은 짧고 각주가 없다.
            chain = (len(parts) >= _MIN_ARROW_STEPS
                     and all(len(p) <= _MAX_STEP_CHARS and not _STEP_NOISE.search(p) for p in parts))
            if chain and not found.steps:
                found.steps = [Step(p[:_MAX_LABEL], locator=locator) for p in parts[:_MAX_STEPS]]
                found.chain_locator, found.chain_quote = locator, line
                continue
            number = _NUMBERED.match(line)
            if number:
                body = number.group("body").strip()
                lead = _LEAD.match(body)
                label = lead.group("lead") if lead else body.split(",")[0][:_MAX_LABEL]
                numbered.append(Step(label.strip(), detail=(lead.group("rest") if lead else body)[:200],
                                     locator=locator))
        elif block == "schedule":
            bullet = _BULLET.match(line)
            body = bullet.group("body") if bullet else line
            lead = _LEAD.match(body)
            if lead and actor_of(lead.group("lead"), own):
                schedule.append((lead.group("lead").strip(), lead.group("rest")))
        elif block == "duty":
            if _FOOTNOTE.match(line):
                continue
            bullet = _BULLET.match(line)
            if bullet:
                body = bullet.group("body").strip()
                if _ARROWS.search(body) or _OTHER_SUBJECT.match(body) or _CONTACT.search(body):
                    continue
                found.duties.append((body[:300], locator))

    numbered = [n for n in numbered if len(n.label) <= _MAX_STEP_CHARS and not _STEP_NOISE.search(n.label)]
    if not found.steps and len(numbered) >= 2:
        found.steps = numbered[:_MAX_STEPS]
        found.chain_locator = numbered[0].locator
        found.chain_quote = " → ".join(step.label for step in found.steps)
    elif found.steps:
        # 화살표 줄의 단계마다 번호 항목의 설명을 잇는다(같은 이름일 때).
        for step in found.steps:
            twin = next((n for n in numbered if _same(n.label, step.label)), None)
            if twin is not None:
                step.detail = twin.detail
    for step in found.steps:
        step.word = _actor_for(step, schedule, own)
        step.actor = actor_of(step.word, own)
    return found


def _same(left: str, right: str) -> bool:
    return re.sub(r"\s+", "", left) == re.sub(r"\s+", "", right)


_TRAILING = re.compile(r"\((?P<who>[^()]{2,30})\)\s*$")


def _actor_for(step: Step, schedule: list[tuple[str, str]], own: str) -> str:
    """단계를 하는 기관의 낱말 — 단계 이름의 머리말·기관 낱말, 번호 항목 끝의 '(행안부, 국조실)',
    그다음 추진일정 줄 가운데 낱말이 가장 많이 겹치는 줄의 머리말. 못 정하면 ''."""
    lead = _LEAD.match(step.label)
    if lead and actor_of(lead.group("lead"), own):
        return lead.group("lead").strip()
    words = set(korean.tokens(step.label))
    for word in words:
        if actor_of(word, own) == OURS:
            return word
    trailing = _TRAILING.search(step.detail or "")
    if trailing:
        first = re.split(r"\s*[,·ㆍ]\s*", trailing.group("who"))[0]
        if actor_of(first, own):
            return first
    best, score = "", 0
    for word_of_actor, line in schedule:
        overlap = sum(1 for word in words if len(word) >= 2 and word in line)
        if overlap > score:
            best, score = word_of_actor, overlap
    return best


def labelled(step: Step) -> str:
    """'(행안부) 오류자료 추출' — 문서가 쓴 기관 낱말을 단계 이름 앞에 밝힌다.

    단계 이름이 이미 기관을 말하면('지자체 규제정보 정비') 붙이지 않는다. 모르면 이름만.
    """
    if not step.word or step.label.startswith("(") or step.word in step.label:
        return step.label
    return f"({step.word}) {step.label}"


def step_actor(label: str, own: str = "") -> str:
    """단계 이름에서 하는 기관을 다시 읽는다 — 앞의 '(기관)' 또는 이름 속 '지자체' 같은 우리 낱말."""
    lead = _LEAD.match(label or "")
    if lead and actor_of(lead.group("lead"), own):
        return actor_of(lead.group("lead"), own)
    return OURS if any(actor_of(word, own) == OURS for word in korean.tokens(label or "")) else ""
