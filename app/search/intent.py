"""질문이 묻는 속성과 답의 값이 문서에서 같은 자리에 묶여 있는가 — 규칙 기반, 모델 호출 없음.

문장별 근거 검증(verify.py)은 '문장의 숫자가 인용한 근거에 있는가'를 본다. 그것만으로는
묻는 것과 다른 값을 막지 못한다(개발지시서 P0-03).

    "2025년 용역은 최종적으로 얼마로 정산됐어?"  →  "계약금액은 48,000천원입니다."

48,000은 근거에 있다. 그러나 문서에서 그 값이 묶인 이름표는 '계약금액'이고 질문이 묻는
것은 '정산'이다. 여기서는 그 묶임을 본다.

  ① 질문을 대상·속성·기간·의도로 푼다(frame). 속성은 물음말 앞의 낱말("정산액은 얼마야")
     이거나 물음말 뒤의 서술어("얼마로 정산됐어" → 정산)다.
  ② 답 문장의 값(금액·수량·날짜)마다 인용한 문서에서 그 값이 묶인 자리를 찾는다 —
     이름표 줄('계약금액: 48,000천원')과 표의 칸(행 이름 + 열 이름).
  ③ 묶인 자리가 있는데 어느 것도 묻는 속성을 가리키지 않으면 다른 속성의 값이다.
  ④ 묻는 속성을 가리키는 자리가 여럿이면('계획 인원'·'수료 인원') 질문의 말과 더 많이
     겹치는 쪽의 값이어야 한다.

판정할 수 없으면 건드리지 않는다. 속성을 읽지 못한 질문, 값이 문장 속에만 있어 묶인 자리를
알 수 없는 답은 그대로 지나간다 — 여기서 버리는 것은 '다른 것의 값임을 문서가 보여 주는'
답뿐이다. 날짜는 월·일을 한 덩어리로 견준다. 낱자리로 견주면 '4월 1일'의 4가 '작성일:
2025. 3. 4.'에 걸려 옳은 답을 버린다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as _field
from decimal import Decimal, InvalidOperation

from ..core import korean
from .query import infer_scope

# ── 같은 것을 가리키는 말 ───────────────────────────────────────────
# 사람은 문서의 말로 묻지 않는다. 다른 말로 묻는 24문항에서 초점 검사가 옳은 답 7건을
# 버렸다('제출기한'을 '마감'으로, '서식'을 '양식'으로, '계약금액'을 '계약액'으로 물었다).
# 묶음을 늘릴 때는 반드시 역방향 시험을 붙인다 — 한 묶음의 말이 다른 속성의 이름표까지
# 받아들이면 이 검사는 없는 것과 같다(tests/test_intent.py).
SAME_THING = (("기한", "마감일", "마감", "기일", "시한"), ("서식", "양식"))

# 서술어로 묻는 말 → 문서의 이름표에 쓰이는 말. '정산됐어'처럼 한자어에 되다·하다가 붙은
# 꼴은 규칙으로 읽으므로 여기 없다. 고유어 동사만 적는다.
_NATIVE_VERBS = (
    (re.compile(r"(?:^|\s)(?:썼|쓴|쓰였)"), ("집행", "지출", "사용")),
    (re.compile(r"(?:^|\s)들어(?:온|왔)"), ("접수",)),
    (re.compile(r"(?:^|\s)(?:냈|낸|내야|내는|내)(?=[\s?]|[어야]|$)"), ("제출",)),
    (re.compile(r"(?:^|\s)맡(?:고|은|았|아)"), ("담당",)),
)

_ASK_VALUE = re.compile(r"얼마|몇|언제|며칠")
_OTHER_ASK = re.compile(r"어떻게|어떤|어땠|무슨|뭐|무엇|어디|누구|누가|왜")
_INTENTS = (
    ("why", re.compile(r"왜|이유|원인|까닭|어째서")),
    ("when", re.compile(r"언제|며칠|몇\s*(?:월|일|년|시)")),
    ("amount", re.compile(r"얼마")),
    ("count", re.compile(r"몇")),
    ("where", re.compile(r"어디")),
    ("who", re.compile(r"누구|누가|어느\s*(?:부서|팀|과|업체|기관)")),
    ("how", re.compile(r"어떻게|어떤\s*(?:순서|차례|절차|방법)")),
)
# 한자어 + 되다/하다/받다. '정산됐어' → 정산, '지급한' → 지급, '납품받기로' → 납품.
_LIGHT_VERB = r"(?:됐|되었|됩|되는|되|된|했|하였|합|하는|한|하|해|돼|받았|받기|받은|받)"
_PREDICATE = re.compile(rf"^([가-힣]{{2,5}}?){_LIGHT_VERB}[가-힣]*$")
_ADNOMINAL = re.compile(rf"([가-힣]{{2,5}}?)(?:된|한|받은)\s+(?:돈|금액|비용|액수|인원|사람|날짜|날|건수|수량)")
# 속성이 아닌 서술어. 무엇의 값인지 가르지 못한다.
_NO_FIELD = {"어떻게", "진행", "확인", "기재", "기록", "명시", "표시", "예정", "해당",
             "필요", "가능", "관련", "대해", "위해", "통해", "생각", "정리", "설명", "말씀", "이야기"}


@dataclass(slots=True)
class QueryFrame:
    """질문 하나를 푼 것. 읽지 못한 칸은 비어 있고, 비어 있으면 따지지 않는다."""

    question: str
    intent: str = "what"                 # amount·count·when·where·who·how·why·what
    field: str = ""                      # 묻는 속성 — 질문에 쓰인 말
    stems: tuple[str, ...] = ()          # 문서에서 그 속성을 가리킬 수 있는 말
    subject: list[str] = _field(default_factory=list)   # 대상을 가리키는 나머지 낱말
    years: list[int] = _field(default_factory=list)

    def as_record(self) -> dict:
        """진단에 남길 값. 질문 원문과 대상 낱말은 넣지 않는다."""
        return {"intent": self.intent, "field": self.field, "years": list(self.years)}


def frame(question: str, noun_focus: str = "") -> QueryFrame:
    """noun_focus는 물음말 앞에서 읽은 낱말(rag._focus). 서술어로 물었으면 서술어가 이긴다."""
    intent = next((name for name, pattern in _INTENTS if pattern.search(question)), "what")
    out = QueryFrame(question=question, intent=intent, years=infer_scope(question).years)
    predicate = _predicate_field(question) if intent in ("amount", "count", "when") else ()
    if predicate:
        out.field, out.stems = predicate[0], predicate
    elif noun_focus and not _asked_with_a_verb(question):
        # '용역은 언제까지 끝내야 해?'의 '용역'은 대상이지 속성이 아니다. 물음말 뒤에
        # 서술어가 따로 있는데 거기서 속성을 읽지 못했으면 속성을 모르는 것이다.
        out.field, out.stems = noun_focus, (noun_focus,)
    taken = set(out.stems)
    out.subject = [word for word in korean.tokens(_ASK_VALUE.sub(" ", question))
                   if not re.search(r"\d", word) and not any(stem in word for stem in taken)]
    return out


def _asked_with_a_verb(question: str) -> bool:
    """물음말 뒤에 낱말이 둘 이상 이어지는가('언제까지 끝내야 해'). '얼마야'·'몇 명이야'는 아니다."""
    asked = _ASK_VALUE.search(question)
    return bool(asked) and len(question[asked.end():].strip(" ?.!").split()) >= 2


def _predicate_field(question: str) -> tuple[str, ...]:
    asked = _ASK_VALUE.search(question)
    if not asked:
        return ()
    match = _ADNOMINAL.search(question)
    if match and match.group(1) not in _NO_FIELD:
        return (match.group(1),)
    tail = question[asked.end():].strip(" ?.!")
    if _OTHER_ASK.search(tail):
        # '몇 건이고 어떻게 처리됐어?' — 끝의 서술어는 뒤의 물음에 딸린 것이다.
        return ()
    for pattern, stems in _NATIVE_VERBS:
        if pattern.search(tail) or pattern.search(question[:asked.start()]):
            return stems
    words = tail.split()
    if len(words) < 2:              # '얼마야', '몇 건이야' — 서술어로 묻지 않았다
        return ()
    match = _PREDICATE.match(words[-1])
    if not match or match.group(1) in _NO_FIELD:
        return ()
    return (match.group(1),)


def said_in(focus: str, text: str) -> bool:
    """글(text)이 그 말(focus)을 하는가. 글자 그대로, 같은 뜻의 말로, 또는 줄인 말로."""
    squeezed = re.sub(r"\s+", "", text)
    if focus in squeezed:
        return True
    for group in SAME_THING:
        for tail in group:
            if focus.endswith(tail):
                head = focus[:-len(tail)]
                # 앞말이 붙어 있으면('신청마감일') 문서에도 그 앞말과 함께 있어야 한다.
                if any(head + other in squeezed for other in group):
                    return True
                break
    # 줄인 말: '계약액' ← '계약금액'. 첫 글자와 끝 글자가 같고 글자가 차례대로 들어 있는 낱말.
    if len(focus) >= 3:
        for word in set(re.findall(r"[가-힣]+", text)):
            if (len(focus) < len(word) <= len(focus) + 2 and word[0] == focus[0]
                    and word[-1] == focus[-1] and _in_order(focus, word)):
                return True
    return False


def _in_order(short: str, long: str) -> bool:
    rest = iter(long)
    return all(char in rest for char in short)


# ── 값이 묶인 자리 ──────────────────────────────────────────────────

_DATE_FULL = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*[.년]\s*(\d{1,2})\s*[.월]\s*(\d{1,2})")
_DATE_SHORT = re.compile(r"(?<!\d)(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_DATE_TAIL = re.compile(r"~\s*(\d{1,2})\s*\.\s*(\d{1,2})")          # '4. 7. ~ 4. 11.'
_NUMBER = re.compile(r"(?<![\d.])\d+(?:,\d{3})*(?:\.\d+)?")
_YEAR = re.compile(r"(?:19|20)\d{2}")
_LOOSE_LABEL = re.compile(r"([가-힣A-Za-z]{2,}(?:\([^)]{1,12}\))?)\s*[:：]\s*(\S.*)$")


def values(text: str, skip: str = "") -> set[tuple]:
    """글에 적힌 값. 날짜는 ('date', 월, 일), 그 밖의 수는 ('num', 값).

    skip에 든 수(질문이 스스로 적은 해·분기)와 연도는 값으로 치지 않는다.
    """
    found: set[tuple] = set()
    rest = text
    for pattern in (_DATE_FULL, _DATE_SHORT, _DATE_TAIL):
        for match in pattern.finditer(rest):
            found.add(("date", int(match.group(1)), int(match.group(2))))
        rest = pattern.sub(" ", rest)
    skipped = {_decimal(n) for n in _NUMBER.findall(skip)}
    for number in _NUMBER.findall(rest):
        value = _decimal(number)
        if value is None or value in skipped or _YEAR.fullmatch(number):
            continue
        found.add(("num", value))
    return found


def _decimal(number: str):
    try:
        return Decimal(number.replace(",", ""))
    except InvalidOperation:
        return None


def bindings(cited_text: str) -> list[tuple[str, set[tuple]]]:
    """인용한 글에서 (자리 이름, 그 자리의 값들). 이름표 줄과 표의 칸만 본다."""
    from .verify import _LABELLED, _tables

    out: list[tuple[str, set[tuple]]] = []
    for line in cited_text.splitlines():
        for segment in line.split(" /"):
            if " | " in segment:
                continue
            # 증거창이 '…'로 이은 자리와 문장 끝을 넘어 이름표를 잇지 않는다.
            for part in re.split(r"\s…\s|(?<=[가-힣])\.\s+", segment):
                part = part.strip()
                match = _LABELLED.fullmatch(part)
                if match:
                    # 괄호 안의 말('교육 인원(계획)')도 자리 이름이다.
                    name, held = re.split(r"[:：]", part, maxsplit=1)[0].strip(), values(match.group(2))
                else:
                    # 제목 줄 바로 뒤의 이름표는 줄 경계가 남지 않는다('… 보고 요청 작성일:
                    # 2025. 9. 1.'). 어디까지가 이름표인지 알 수 없으니 쌍점 앞의 한 낱말만 쓴다.
                    loose = _LOOSE_LABEL.search(part)
                    if not loose:
                        continue
                    name, held = loose.group(1), values(loose.group(2))
                if held:
                    out.append((name, held))
    for headers, rows in _tables(cited_text):
        for row in rows:
            for index in range(1, min(len(row), len(headers))):
                held = values(row[index])
                if held:
                    out.append((f"{row[0]} {headers[index]}", held))
    return out


def _answer_values(asked: QueryFrame, answer_text: str) -> set[tuple]:
    """답이 내놓은 값 가운데 질문이 묻는 갈래의 것.

    때를 물었으면 날짜만, 금액·수량을 물었으면 수만 본다. 섞어 보면 '분기마다(1·4·7·10월)
    작성한다'의 달 숫자가 아무 표의 칸에나 묶이고, '4월 15일 현재 12건'의 날짜가 작성일에
    묶여 옳은 답을 버린다(개발용 30문항에서 겪었다). 질문이 스스로 적은 수와 날짜도 뺀다.
    """
    kind = "date" if asked.intent == "when" else "num"
    said = values(answer_text, skip=asked.question) - values(asked.question)
    return {value for value in said if value[0] == kind}


def mismatch(asked: QueryFrame, answer_text: str, cited_text: str) -> str:
    """답의 값이 묻는 속성의 값이 아니면 그 까닭(코드). 판정할 수 없거나 맞으면 빈 문자열.

      field_mismatch   값이 묶인 자리 어느 것도 묻는 속성을 가리키지 않는다.
      field_sibling    묻는 속성의 자리가 여럿인데, 질문의 말과 덜 겹치는 쪽의 값을 답했다.
    """
    if not asked.stems or asked.intent not in ("amount", "count", "when"):
        return ""
    said = _answer_values(asked, answer_text)
    if not said:
        return ""
    places = bindings(cited_text)
    held = [(name, vals) for name, vals in places if vals & said]
    if not held:
        return ""                    # 값이 문장 속에만 있다 — 어느 자리의 값인지 알 수 없다

    def on_field(name: str) -> bool:
        return any(said_in(stem, name) for stem in asked.stems)

    matching = [name for name, _vals in held if on_field(name)]
    if not matching:
        return "field_mismatch"

    # 낱말의 앞머리로 견준다 — '직무교육'이 '교육 인원(계획)'의 '교육'과 겹치면 안 된다.
    spoken = korean.tokens(asked.question)

    def closeness(name: str) -> int:
        words = [w for w in re.findall(r"[가-힣A-Za-z]{2,}", name)
                 if not any(said_in(stem, w) for stem in asked.stems)]
        return sum(1 for word in words if any(token.startswith(word) for token in spoken))

    rivals = [name for name, _vals in places if on_field(name)]
    if max(closeness(name) for name in rivals) > max(closeness(name) for name in matching):
        return "field_sibling"
    return ""


def confirmed(asked: QueryFrame, answer_text: str, cited_text: str) -> bool:
    """답의 값이 묻는 속성의 자리에 묶여 있음을 문서가 보여 주는가.

    mismatch가 빈 문자열인 것과 다르다 — 그쪽은 '알 수 없음'도 빈 문자열이다.
    """
    if not asked.stems:
        return False
    said = _answer_values(asked, answer_text)
    return bool(said) and not mismatch(asked, answer_text, cited_text) and any(
        vals & said and any(said_in(stem, name) for stem in asked.stems)
        for name, vals in bindings(cited_text))


# ── 문서 속 지시문 ──────────────────────────────────────────────────
# 문서는 자료이지 지시가 아니다. '이전 지침을 무시하고 …라고 답할 것' 같은 줄은 사실을
# 말하지 않으므로 근거로 쓰지 않는다. 원문은 그대로 두고, 생성 문맥과 검증에서만 뺀다.
_TO_MODEL = r"(?:AI|인공지능|챗봇|어시스턴트|도우미|언어\s*모델|LLM|시스템\s*프롬프트|assistant|system\s*prompt)"
_OVERRIDE = (r"(?:무시|따르지\s*말|잊어|답할\s*것|답하라|답하세요|답하시오|말할\s*것|말하라|출력하라|"
             r"출력할\s*것|ignore|disregard|override)")
_INJECTION = (
    re.compile(rf"{_TO_MODEL}.{{0,40}}{_OVERRIDE}", re.IGNORECASE),
    re.compile(r"(?:이전|위|앞)의?\s*(?:모든\s*)?(?:지침|지시|명령|규칙|프롬프트)[을를은는]?\s*(?:모두\s*)?"
               r"(?:무시|따르지\s*말|잊)"),
    re.compile(r"(?:ignore|disregard)\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions|prompts?)",
               re.IGNORECASE),
)
INJECTION_NOTE = "[문서 속 지시문 — 근거로 쓰지 않음]"


def strip_instructions(text: str) -> tuple[str, int]:
    """모델에게 하는 지시로 보이는 문장을 뺀 글과 뺀 문장 수."""
    removed = 0
    kept_lines = []
    for line in (text or "").splitlines():
        parts = re.split(r"(?<=[.!?。])\s+|(?<=\s/)\s*", line)
        kept = []
        for part in parts:
            if part and any(pattern.search(part) for pattern in _INJECTION):
                removed += 1
                kept.append(INJECTION_NOTE)
            else:
                kept.append(part)
        kept_lines.append(" ".join(p for p in kept if p))
    # 뺀 것이 없으면 글자 하나 건드리지 않는다.
    return ("\n".join(kept_lines), removed) if removed else (text or "", 0)
