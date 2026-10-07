"""문장별 근거 검증 — 규칙 기반, LLM 호출 없음 (RAG 개선 R5, 이 계획의 핵심).

모델에게 "sources"를 스스로 밝히라고 시켜도, 그 sources가 실제로 그 문장을
뒷받침하는지는 모델이 보장하지 않는다 — v1이 겪은 "그럴듯하지만 근거 없는
답"이 문장 단위로 재발할 수 있다. 그래서 규칙으로 한 번 더 확인한다.
생성 호출을 늘리지 않는다(원칙 6 — 대량 처리는 규칙 기반으로).

검증 순서 (하나라도 걸리면 그 문장은 버린다)
  ① sources가 비어 있다 — 근거를 스스로 못 댔다.
  ② 존재하지 않는 근거 번호를 인용했다 — 컨텍스트에 없는 조각을 지어냈다.
  ③ 문장의 숫자·연도·금액이 인용한 조각 어디에도 없다 — 숫자를 지어냈다.
     (실측: "1분기 41건, 2분기 38건…"처럼 무관한 표를 그대로 옮겨 붙인
     사례가 있었다. 이 검사는 '엉뚱한 근거를 정확히 인용한' 경우까지는
     잡지 못한다 — 그건 검색 단계의 몫이다.)
  ④ 인용한 조각과 겹치는 낱말이 거의 없다 — 인용은 했지만 그 내용과
     무관한 말을 하고 있다는 신호.
  ⑦ "정보는 없으나 …"처럼 답이 없다고 하면서 다른 내용을 내놓는다.
  ⑨ 이름표를 부르며 다른 이름표의 값을 말했다. (60문항 평가: "구비서류는 실적표
     별지 제3호이다" — 그 값은 바로 앞 줄 '제출 서식'의 것이다.)
  ⑧ 표의 숫자를 말하면서 질문의 낱말을 되받았는데, 그 낱말이 인용한 문서에
     없다 — 다른 것의 값을 묻는 것의 값처럼 말했다. (60문항 평가: "1분기
     수질검사 건수는 41건이었다" — 41은 월간실적보고의 처리건수이고 그 문서에
     '수질검사'는 없다.)
  ⑥ 숫자에 붙인 금액 단위(천원·백만원 등)가 인용한 조각에 없다.
  ⑤ 표의 행과 열을 짚어 말했는데 그 칸의 값이 아니다 — 같은 표의 다른 칸을
     읽었다. (30문항 평가: "2분기 집행액은 430입니다" — 430은 3분기 칸이고
     2분기는 210이다. 숫자가 근거에 있으니 ③은 통과한다.)

주의: 이 검증은 '문장이 인용한 근거에 충실한가'만 본다. '그 근거가
질문과 애초에 관련 있는가'는 검색 단계(R3·R4)와 프롬프트(R6)의 몫이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..core import korean

from ..core import korean

_NUMBER = re.compile(r"(?<!\d)\d+(?:,\d{3})*(?:\.\d+)?%?")
_CITATION = re.compile(r"\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]")
_NEGATION = re.compile(r"아니(?:다|라|며|고|었)|않(?:다|는|았|습니다)|없(?:다|음|습|었)|불가|금지")
# 실측 기준값이 아니라 임시값이다 — R1 평가셋으로 재조정 대상(rag_report).
MIN_OVERLAP = 0.25


@dataclass(slots=True)
class VerifiedSentence:
    text: str
    sources: list[int]


@dataclass(slots=True)
class Dropped:
    text: str
    reason: str


@dataclass(slots=True)
class VerifyResult:
    kept: list[VerifiedSentence] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)

    @property
    def survived(self) -> bool:
        return bool(self.kept)


def verify_sentences(
    raw_sentences: list[Any], source_text: dict[int, str],
    support_text: dict[int, str] | None = None,
    question: str = "", names: dict[int, str] | None = None,
) -> VerifyResult:
    """모델이 낸 sentences 배열을 검증해 살아남은 것만 돌려준다.

    question과 names(근거 번호 → 문서 이름)를 주면 ⑧을 함께 본다.

    source_text는 {1-based 근거 번호: 그 조각 본문}. _format_context가
    매긴 번호와 같은 번호 체계를 써야 한다 — 다르면 멀쩡한 인용도
    '존재하지 않는 번호'로 오판한다.

    support_text는 사실 확인에만 쓰는 더 넓은 본문이다(기본값은 source_text).
    조각 하나가 스스로를 설명하지 못할 때 이웃 조각을 함께 컨텍스트에
    넣는데(rag.py의 _expand_neighbors), 그렇게 보여준 이웃을 검증에서는
    근거로 인정하지 않으면 앞뒤가 맞지 않는다. 실측으로 겪었다 — 모델이
    표 조각의 '820'을 인용하며 옆 문단의 '2023년'을 함께 쓴 정확한 문장을
    냈는데, 연도가 인용 조각에 없다는 이유로 버려졌다.
    """
    support_text = support_text or source_text
    result = VerifyResult()
    for item in raw_sentences:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            continue   # 빈 문장은 조용히 넘긴다 — 사유를 남길 것도 없다

        sources = _clean_sources(item.get("sources"))
        if not sources:
            # 근거 번호를 sources가 아니라 문장 끝에 적는 경우가 있다(30문항 평가:
            # {"text": "…문서유형은 계획서이다.[1]", "sources": []} — 옳은 답이
            # '근거 없음'으로 버려졌다). 모델이 밝힌 근거이므로 그 번호로 본다.
            # 아래의 검증은 그대로 거친다.
            sources = [int(value.strip()) for mark in _CITATION.findall(text)
                       for value in mark.split(",")]
        if not sources:
            result.dropped.append(Dropped(text, "근거를 스스로 밝히지 않았다"))
            continue

        valid_sources = [s for s in sources if s in source_text]
        if not valid_sources:
            result.dropped.append(Dropped(text, "존재하지 않는 근거 번호를 인용했다"))
            continue

        # Only structured, existing source IDs can authorize display citations.
        marks = _CITATION.findall(text)
        marked_ids = {int(value.strip()) for mark in marks for value in mark.split(",")}
        if not marked_ids.issubset(set(valid_sources)):
            result.dropped.append(Dropped(text, "문장의 인용번호가 구조화 근거와 다르다"))
            continue
        text = " ".join(_CITATION.sub("", text).split())
        if not text:
            continue

        # 조각마다 한 줄로 둔다 — 표의 칸 대조가 조각을 넘어 표를 잇지 않게.
        cited_text = "\n".join(
            support_text.get(s, source_text[s]) for s in valid_sources
        )

        missing = _unsupported_numbers(text, cited_text)
        if missing:
            result.dropped.append(
                Dropped(text, f"인용한 근거에 없는 숫자를 썼다: {', '.join(missing)}")
            )
            continue

        if _HEDGE.search(text):
            result.dropped.append(Dropped(text, "답이 없다고 하면서 다른 내용을 내놓았다"))
            continue

        units = _unsupported_units(text, cited_text)
        if units:
            result.dropped.append(
                Dropped(text, f"인용한 근거에 없는 금액 단위를 썼다: {', '.join(units)}"))
            continue

        wrong_cell = _wrong_table_cell(text, cited_text)
        if wrong_cell:
            result.dropped.append(
                Dropped(text, f"표에서 짚은 칸의 값이 아니다: {', '.join(wrong_cell)}"))
            continue

        swapped = _swapped_label(text, cited_text)
        if swapped:
            result.dropped.append(Dropped(text, f"다른 이름표의 값을 말했다: {swapped}"))
            continue

        strangers = _foreign_subject(text, question, cited_text + "\n" + "\n".join(
            (names or {}).get(s, "") for s in valid_sources))
        if strangers:
            result.dropped.append(Dropped(
                text, f"표의 값을 인용한 문서에 없는 것의 값처럼 말했다: {', '.join(strangers)}"))
            continue

        if not _overlaps_enough(text, cited_text):
            result.dropped.append(Dropped(text, "인용한 근거와 겹치는 말이 거의 없다"))
            continue

        if _contradicts_polarity(text, cited_text):
            result.dropped.append(Dropped(text, "인용한 근거의 부정 표현과 반대되는 문장이다"))
            continue

        result.kept.append(VerifiedSentence(text=text, sources=valid_sources))
    return result


def _clean_sources(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, bool):   # bool은 int의 서브클래스라 따로 걸러야 한다
            continue
        if isinstance(item, int):
            out.append(item)
    return out


def _unsupported_numbers(text: str, cited_text: str) -> list[str]:
    """문장의 숫자 중 근거에 없는 것. 문장부호는 떼고 본다.

    쉼표·마침표를 숫자 일부로 받는 이유는 '1,234'·'3.14' 때문이다. 그런데
    그대로 두면 "820, 2분기 0"에서 '820,'을 통째로 숫자로 잡아 근거에 없다고
    판정한다 — 정상 문장이 계속 버려졌다. 뒤에 붙은 문장부호만 떼어 낸다.
    """
    def canonical(number):
        from decimal import Decimal
        suffix = "%" if number.endswith("%") else ""
        return (Decimal(number.rstrip("%").replace(",", "")), suffix)
    supported = {canonical(n) for n in _NUMBER.findall(cited_text)}
    return sorted({n for n in _NUMBER.findall(text) if canonical(n) not in supported})


_LABELLED = re.compile(r"([가-힣A-Za-z][가-힣A-Za-z ·]{0,14}?)\s*(?:\([^)]{1,12}\))?\s*[:：]\s*(.+)")


def _swapped_label(text: str, cited_text: str) -> str:
    """문장이 부른 이름표의 값은 없고, 부르지 않은 이름표의 값이 통째로 들어 있으면 그 이름표.

    증거창이 이름표 줄의 끝을 ' /'로 남기므로 (이름표, 값)을 다시 읽을 수 있다.
    짧은 값(네 글자 미만)과 여러 이름표가 함께 가진 값은 따지지 않는다.
    """
    squeezed = re.sub(r"\s+", "", text)
    values: dict[str, set[str]] = {}
    for line in cited_text.splitlines():
        for segment in line.split(" /"):
            if " | " in segment:
                continue
            match = _LABELLED.fullmatch(segment.strip())
            if match:
                values.setdefault(re.sub(r"\s+", "", match.group(1)), set()).add(
                    re.sub(r"\s+", "", match.group(2)).rstrip("."))
    named = [label for label in values if len(label) >= 2 and label in squeezed]
    if not named or any(value in squeezed for label in named for value in values[label]):
        return ""
    own = {value for label in named for value in values[label]}
    for label, held in values.items():
        if label in named:
            continue
        if any(len(value) >= 4 and value in squeezed and value not in own for value in held):
            return label
    return ""


# 질문의 낱말 가운데 '무엇의 값인가'를 가리지 않는 말. 되받아도 따지지 않는다.
_NOT_A_SUBJECT = re.compile(r"^(?:작년|지난해|전년도?|올해|금년|내년|얼마|몇|어느|어떤|무슨|언제|어디)")


def _foreign_subject(text: str, question: str, about: str) -> list[str]:
    """문장이 되받은 질문의 낱말 가운데 인용한 문서에 없는 것. 표의 숫자를 말한 문장만 본다.

    "시범사업의 4분기 집행액은 180입니다"는 행(집행액)과 열(4분기)이 맞아 칸
    대조를 지나간다. 그러나 그 표는 예산관리의 것이고 그 문서 어디에도
    '시범사업'은 없다. 질문이 물은 것과 표가 말하는 것이 다르면, 표의 값이
    옳아도 답은 틀렸다.
    """
    if not question:
        return []
    cells = {cell.replace(",", "") for _headers, rows in _tables(about)
             for row in rows for cell in row[1:]}
    said = {number.rstrip("%").replace(",", "") for number in _NUMBER.findall(text)}
    if not said & cells:
        return []
    out = []
    # '몇 건이야'의 '건이야'는 답의 '41건이다'와 겹친다 — 묻는 말은 빼고 본다.
    for word in korean.tokens(re.sub(r"몇\s*[가-힣]+", " ", question)):
        if re.search(r"\d", word) or _NOT_A_SUBJECT.match(word):
            continue        # 숫자는 ③이 본다
        if korean.contains(text, word) and not korean.contains(about, word):
            out.append(korean.strip_punctuation(word))
    return out


# ── 표의 칸 대조 ────────────────────────────────────────────────────
# 증거창(search/evidence.py)이 표를 한 줄로 펴면서 남기는 꼴은 둘이다. 행은 ' /'로 끝난다.
#   칸 꼴   "구분 | 1분기 | 2분기 / 집행액 | 0 | 210 /"                  (xlsx·docx·csv)
#   행 꼴   "첫 행: 구분 | 1분기 | 2분기 1열: 집행액 | 2열: 0 | 3열: 210 /"  (hwpx·odt)
_ROW_FORM = re.compile(r"(?:첫 행|머리글):\s*(.+?)\s+(1열:.+?)\s/")
_ROW_CELL = re.compile(r"(\d+)열:\s*(.*?)(?=\s\|\s\d+열:|$)")


def _tables(cited_text: str) -> list[tuple[list[str], list[list[str]]]]:
    """인용한 글에서 (머리글, 행들)을 읽는다. 머리글이 있는 표만 본다.

    근거 조각마다 한 줄이다. 줄을 넘어 표를 잇지 않는다 — 다른 조각의 표와
    섞이면 열이 어긋난다.
    """
    tables: list[tuple[list[str], list[list[str]]]] = []
    for line in cited_text.splitlines():
        for match in _ROW_FORM.finditer(line):
            headers = [part.strip() for part in match.group(1).split(" | ")]
            cells = {int(n): v.strip() for n, v in _ROW_CELL.findall(match.group(2))}
            tables.append((headers, [[cells.get(i, "") for i in range(1, len(headers) + 1)]]))
        block: list[list[str]] = []
        for segment in [*_ROW_FORM.sub(" ", line).split(" /"), ""]:
            if " | " in segment and "열:" not in segment:
                block.append([cell.strip() for cell in segment.split(" | ")])
                continue
            if len(block) >= 2:
                tables.append((block[0], block[1:]))
            block = []
    return tables


def _wrong_table_cell(text: str, cited_text: str) -> list[str]:
    """문장이 표의 행 이름과 열 이름을 함께 짚었을 때, 그 칸에 없는 표 안의 숫자.

    행이나 열 가운데 하나만 말한 문장은 건드리지 않는다 — 어느 칸을 말하는지
    정할 수 없으면 판정하지 않는다. 여러 행·열을 말했으면 그 교차 칸 전부를
    허용한다. 표에 아예 없는 숫자(연도 등)는 ③의 몫이다.
    """
    squeezed = re.sub(r"\s+", "", text)
    allowed: set = set()
    in_tables: set = set()
    labels: set[str] = set()
    for headers, rows in _tables(cited_text):
        columns = [i for i, name in enumerate(headers)
                   if i > 0 and len(name) >= 2 and re.sub(r"\s+", "", name) in squeezed]
        named = [row for row in rows
                 if row and len(row[0]) >= 2 and re.sub(r"\s+", "", row[0]) in squeezed]
        if not columns or not named:
            continue
        labels.update(re.sub(r"\s+", "", headers[i]) for i in columns)
        labels.update(re.sub(r"\s+", "", row[0]) for row in named)
        for row in rows:
            for cell in row[1:]:
                in_tables.update(_canonical(n) for n in _NUMBER.findall(cell))
        for row in named:
            for i in columns:
                if i < len(row):
                    allowed.update(_canonical(n) for n in _NUMBER.findall(row[i]))
    if not labels:
        return []
    # '2분기'의 2처럼 행·열 이름에 든 숫자는 값이 아니다.
    for label in sorted(labels, key=len, reverse=True):
        squeezed = squeezed.replace(label, " ")
    return sorted({n for n in _NUMBER.findall(squeezed)
                   if _canonical(n) in in_tables and _canonical(n) not in allowed})


def _canonical(number: str):
    from decimal import Decimal
    suffix = "%" if number.endswith("%") else ""
    return (Decimal(number.rstrip("%").replace(",", "")), suffix)


# "A에 대한 정보는 없으나, B는 …" — 프롬프트가 금지한 '대신 답하기'다. 묻는 것은
# 없다고 하면서 곁가지를 답처럼 내놓는다(30문항 평가: "수량에 대한 구체적인 정보는
# 없으나, … 관련 문서들이 존재합니다"가 근거 8건을 달고 답으로 나갔다).
_HEDGE = re.compile(r"(?:정보|내용|자료|언급|기록|명시)[은는이가도]?\s*"
                    r"(?:없으나|없지만|없으며|없고|확인되지 않으나|확인되지 않지만|"
                    r"나와 있지 않으나|나와 있지 않지만|포함되어 있지 않으나|포함되어 있지 않지만)")

# 금액 단위. '원' 하나는 어디에나 있어 따지지 않는다.
_MONEY = re.compile(r"\d\s*(천만원|백만원|십만원|만원|천원|억원|조원)")


def _unsupported_units(text: str, cited_text: str) -> list[str]:
    """숫자에 붙인 금액 단위 가운데 근거에 없는 것.

    30문항 평가에서 단위가 없는 표의 210에 다른 문서의 '백만원'을 붙여 답했다.
    단위가 틀리면 값이 천 배, 백만 배로 달라진다.
    """
    return sorted({unit for unit in _MONEY.findall(text) if unit not in cited_text})


def _contradicts_polarity(text: str, cited_text: str) -> bool:
    """거의 같은 문장을 긍정/부정으로 뒤집은 경우만 보수적으로 거른다.

    일반적인 의미 검증기는 아니다. 다른 문장의 '없다' 때문에 정상 근거를
    버리지 않도록 문장별로 비교하고, 같은 극성의 근거가 있으면 인정한다.
    """
    candidates = [part for part in re.split(r"(?<!\d)[.!?]\s*|\n+", cited_text)
                  if part.strip() and korean.overlap_ratio(text, part) >= 0.8]
    if not candidates:
        return False
    negative = bool(_NEGATION.search(text))
    return all(bool(_NEGATION.search(part)) != negative for part in candidates)


def _overlaps_enough(text: str, cited_text: str) -> bool:
    """인용한 근거와 말이 겹치는가. 조사·어미가 붙은 형태도 같은 말로 본다.

    낱말 완전일치로 재면 "예산요구액은"이 근거의 "예산요구액"과 다른 말이
    되어 정상 문장이 버려진다 — 실측으로 겪었다(core/korean.py 참고).
    """
    return korean.overlap_ratio(text, cited_text) >= MIN_OVERLAP
