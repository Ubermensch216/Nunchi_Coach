"""글에서 법령 인용을 읽어 낸다 — "지방계약법 제9조제1항" → (법령명, 조, 항).

규칙으로만 한다. '방법 제3조'·'사내 규정 제5조'처럼 법령처럼 생겼지만 아닌
말을 걸러 내는 것이 절반이다. 걸러 내지 못하면 엉뚱한 낱말을 법령 이름으로
바깥에 조회하게 된다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 흔히 쓰는 줄임말 → 정식 이름. 공공기관 실무에서 자주 나오는 것 위주.
ALIASES = {
    "헌법": "대한민국헌법",
    "근기법": "근로기준법",
    "산안법": "산업안전보건법",
    "중처법": "중대재해 처벌 등에 관한 법률",
    "중대재해처벌법": "중대재해 처벌 등에 관한 법률",
    "산재법": "산업재해보상보험법",
    "지공법": "지방공무원법",
    "국공법": "국가공무원법",
    "개보법": "개인정보 보호법",
    "개인정보보호법": "개인정보 보호법",
    "정통망법": "정보통신망 이용촉진 및 정보보호 등에 관한 법률",
    "정보통신망법": "정보통신망 이용촉진 및 정보보호 등에 관한 법률",
    "청탁금지법": "부정청탁 및 금품등 수수의 금지에 관한 법률",
    "김영란법": "부정청탁 및 금품등 수수의 금지에 관한 법률",
    "이해충돌방지법": "공직자의 이해충돌 방지법",
    "국가계약법": "국가를 당사자로 하는 계약에 관한 법률",
    "지방계약법": "지방자치단체를 당사자로 하는 계약에 관한 법률",
    "정보공개법": "공공기관의 정보공개에 관한 법률",
    "공공기록물법": "공공기록물 관리에 관한 법률",
    "기록물관리법": "공공기록물 관리에 관한 법률",
    "지방재정법": "지방재정법",
    "지방회계법": "지방회계법",
    "지자법": "지방자치법",
    "행정절차법": "행정절차법",
    "민원처리법": "민원 처리에 관한 법률",
    "전자정부법": "전자정부법",
    "국기법": "국세기본법",
    "부가세법": "부가가치세법",
    "하도급법": "하도급거래 공정화에 관한 법률",
    "공거법": "독점규제 및 공정거래에 관한 법률",
    "민소법": "민사소송법",
    "형소법": "형사소송법",
    "건보법": "국민건강보험법",
    "고보법": "고용보험법",
    "주임법": "주택임대차보호법",
    "국계법": "국토의 계획 및 이용에 관한 법률",
    "감염병법": "감염병의 예방 및 관리에 관한 법률",
}

_SUFFIX = re.compile(r"(?:헌법|법률|법|령|규칙|규정|조례|고시|예규|훈령|지침)$")
# 법령처럼 끝나지만 법령이 아닌 흔한 말.
_NOT_LAW_TAIL = re.compile(
    r"(?:방|수|편|비|용|요|어|기|묘|화|작|식|사용|운영|진단|치료|조리|요리|평가|분석|학습|훈련|"
    r"표현|행동|사고|문|제조|관리|검사|작성|처리|계산|산출|활용|이용|해결)법$")
_NOT_LAW_RULE = re.compile(r"(?:사내|회사|내부|자체|운영|모임|개인|팀|부서|업무|게임|놀이|회의)\s*(?:규칙|규정|지침)$")
_CIRCLED = {c: i for i, c in enumerate("①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳", start=1)}

_ARTICLE = (
    r"\s*제\s*0*(?P<article>\d{1,4})\s*조(?:\s*의\s*0*(?P<branch>\d{1,2}))?"
    r"(?:\s*제?\s*0*(?P<paragraph>\d{1,3})\s*항|\s*(?P<circled>[①-⑳]))?"
    r"(?:\s*제?\s*0*(?P<item>\d{1,3})\s*호)?")
# 꺾쇠로 싼 이름은 그대로 믿는다. 싸지 않은 이름은 앞말이 딸려 오므로 tail_name이 다시 고른다.
_CITATION = re.compile(
    r"(?:[「『](?P<quoted>[^」』]{2,60})[」』]"
    r"|(?P<name>[가-힣A-Za-z0-9·ㆍ ]{0,80}?(?:헌법|법률|법|령|규칙|규정|조례|고시|예규|훈령|지침)))" + _ARTICLE)
_BRACKETED = re.compile(r"[「『](?P<name>[^」』]{2,60})[」』]")
_BARE_NAME = re.compile(
    r"(?P<name>[가-힣A-Za-z0-9·ㆍ ]{0,80}?(?:법률|법|조례)(?: 시행령| 시행규칙)?)"
    r"(?=(?:에서|에는|에도|에|의|을|를|은|는|이|가|과|와|도|상|으로|로)?(?:[\s,.?!)」』]|$))")
_KNOWN = sorted({name.replace(" ", ""): name for name in [*ALIASES, *ALIASES.values()]}.items(),
                key=lambda item: -len(item[0]))
# "…에 관한 법률"처럼 여러 낱말로 된 이름의 안쪽에 오는 말. 이 말로 끝나는 낱말은 이름의 일부로 본다.
_NAME_LINK = ("관한", "위한", "대한", "따른", "의한", "하는", "있는", "받는", "되는", "및", "등")
# 이름 안에 오지 않는 낱말 끝. 여기서 이름이 시작된다고 본다 — "이 건은 / 지방계약법".
_CLAUSE_END = re.compile(r"(?:은|는|이|가|데|면|서|고|며|요|다|까|도|만|인|,)$")
_LONG_TAILS = ("법률", "특별법", "특례법", "규정", "규칙", "조례")
MAX_NAME_WORDS = 12


def tail_name(text: str) -> str:
    """앞말이 딸려 온 글의 끝에서 법령 이름만 골라낸다. 못 고르면 빈 문자열.

    **짧게 고르는 쪽으로 틀린다.** 이름을 덜 잡으면 검색이 정식 이름을 찾아
    주지만("주거환경정비법" → "도시 및 주거환경정비법"), 더 잡으면 법령과 무관한
    앞말("김 주무관 건인데 …")이 바깥 서버로 나간다.
    """
    words = " ".join(str(text or "").split()).split(" ")
    if not words or not words[-1]:
        return ""
    joined = "".join(words)
    for compact, name in _KNOWN:                       # ① 아는 이름·줄임말
        if joined.endswith(compact):
            return ALIASES.get(name, name)
    last = words[-1]
    if last in ("시행령", "시행규칙") and len(words) > 1:  # ② "○○법 시행령"
        base = tail_name(" ".join(words[:-1]))
        return f"{base} {last}" if base else ""
    if last in _LONG_TAILS and len(words) > 1 and words[-2] in _NAME_LINK:
        picked = [last]                                # ③ "…에 관한 법률"
        for word in reversed(words[:-1]):
            if len(picked) >= MAX_NAME_WORDS or (_CLAUSE_END.search(word) and word not in _NAME_LINK):
                break
            picked.append(word)
        return " ".join(reversed(picked)) if len(picked) > 2 else ""
    return last                                         # ④ 붙여 쓴 한 낱말 — "도로교통법"


def normalize_name(value: str) -> str:
    text = " ".join(str(value or "").split()).strip("「」『』\"'`“”‘’ ")
    return ALIASES.get(text.replace(" ", ""), ALIASES.get(text, text))


# 이름 없이 종류만 가리키는 말 — "규정 제5조", "지침 제2조". 어느 법령인지 알 수 없다.
_KIND_ONLY = {"법", "법률", "령", "시행령", "시행규칙", "규칙", "규정", "조례", "고시", "예규", "훈령", "지침"}


def is_lawish(name: str) -> bool:
    text = normalize_name(name)
    if len(text) < 2 or text.isdigit() or text in _KIND_ONLY:
        return False
    if text in ALIASES.values():
        return True
    if _NOT_LAW_TAIL.search(text) or _NOT_LAW_RULE.search(text):
        return False
    return bool(_SUFFIX.search(text))


@dataclass(frozen=True, slots=True)
class LawRef:
    law_name: str
    article: int | None = None
    branch: int | None = None        # 제N조의M
    paragraph: int | None = None
    item: int | None = None

    @property
    def article_label(self) -> str:
        if self.article is None:
            return ""
        return f"제{self.article}조" + (f"의{self.branch}" if self.branch else "")

    @property
    def label(self) -> str:
        parts = [f"「{self.law_name}」", self.article_label]
        if self.paragraph:
            parts.append(f"제{self.paragraph}항")
        if self.item:
            parts.append(f"제{self.item}호")
        return " ".join(part for part in parts if part)

    @property
    def jo_code(self) -> str:
        """law.go.kr의 조문 번호 표기 — 조 4자리 + 가지 2자리."""
        if self.article is None:
            return ""
        return f"{self.article:04d}{self.branch or 0:02d}"


def extract(text: str) -> list[LawRef]:
    """글에 적힌 조문 인용. 같은 조문은 한 번만 돌려준다."""
    source = " ".join(str(text or "").split())
    found: list[LawRef] = []
    for match in _CITATION.finditer(source):
        name = normalize_name(match["quoted"]) if match["quoted"] else tail_name(match["name"])
        if not is_lawish(name):
            continue
        paragraph = int(match["paragraph"]) if match["paragraph"] else _CIRCLED.get(match["circled"] or "")
        ref = LawRef(name, int(match["article"]), int(match["branch"]) if match["branch"] else None,
                     paragraph, int(match["item"]) if match["item"] else None)
        if ref not in found:
            found.append(ref)
    return found


def names(text: str) -> list[str]:
    """조문 번호 없이 이름만 나온 법령. 조문 인용에 든 이름도 포함한다."""
    source = " ".join(str(text or "").split())
    candidates = [normalize_name(m["name"]) for m in _BRACKETED.finditer(source)]
    candidates += [ref.law_name for ref in extract(source)]
    candidates += [tail_name(m["name"]) for m in _BARE_NAME.finditer(source)]
    found: list[str] = []
    for name in candidates:
        if is_lawish(name) and name not in found:
            found.append(name)
    return found
