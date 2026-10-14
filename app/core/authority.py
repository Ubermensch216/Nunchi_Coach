"""문서의 권위 — 이 문서가 업무의 의무·값을 말할 자격이 있는가 (규제개혁 비교연구 R03·R07).

문서 갈래(docrole)는 '이 문서가 업무에서 무엇이었나'를 말한다. 여기서는 그와 다른 축을
본다 — **누가 썼고, 공식 기준이 될 수 있는가.** 실제 자료에서 두 가지가 업무 항목을
오염시켰다.

  · AI가 쓴 참고 보고서. '[ChatGPT-DeepResearch]2026년 … 평가 전략'의 지표 설명이 결재된
    2025년 추진계획을 제치고 계획 업무의 '목적'이 됐다. 파일 연도가 더 최근이라 이겼다.
  · 정기간행물·연구 자료. 'KIPA 규제동향 Vol.50' 77쪽의 "목적은 경제안보 분야의 정보 유출
    방지이다"가 규제개혁신문고 업무의 목적이 되고, 95쪽의 "…이내에 금융감독원에 보고한다"가
    제출처가 됐다.

이런 문서는 아이디어와 배경의 출처로는 쓸모가 있다. 지우지 않고 검색·열람에는 남긴다.
다만 기한·제출처·목적·절차 같은 **업무의 값**을 읽어 내는 근거로는 쓰지 않는다.

이름에 드러난 표시만 본다. 본문 문체로 AI 생성을 짐작하지 않는다 — 확정적 낙인을 찍지
않는다는 원칙이다(개선계획서 4.2).

서식의 빈칸('0000과', '☎000-000-0000', '○○○')은 값이 아니다. 채워 넣을 자리다.
"""

from __future__ import annotations

import re
from pathlib import PurePath

AI_GENERATED = "ai_generated"
PUBLICATION = "publication"

LABEL = {AI_GENERATED: "AI 생성 참고자료", PUBLICATION: "발간물·연구자료"}

# 생성형 AI 도구의 이름. 파일·폴더 이름에 붙여 두는 관행을 그대로 읽는다.
_AI = re.compile(
    r"chat\s*-?\s*gpt|deep\s*-?\s*research|(?<![a-z])gpt[-\s]?\d|gemini|perplexity|copilot"
    r"|claude|뤼튼|wrtn|클로바\s*x|AI\s*생성|생성형\s*AI\s*(?:작성|초안)", re.IGNORECASE)
# 정기간행물·학술·연구 자료. 업무 문서가 이런 이름을 갖는 일은 드물다.
_PUBLICATION = re.compile(
    r"vol\.?\s*\d+|(?:봄|여름|가을|겨울)\s*호|통권\s*\d+|제\s*\d+\s*권\s*제?\s*\d+\s*호"
    r"|이슈\s*분석|이슈\s*페이퍼|이슈\s*브리프|정책\s*브리프|소고|학술|논문|저널|journal"
    r"|(?:규제|관심|정책|입법|산업)\s*동향", re.IGNORECASE)


def marker(filename: str, path: str = "") -> str | None:
    """이 문서가 업무 값의 근거가 될 수 없는 참고물인가 — AI_GENERATED, PUBLICATION, None.

    파일 이름을 먼저, 그다음 파일이 든 폴더 이름을 본다. 'AI 기본법 관련 …' 같은 주제어의
    'AI'는 도구 이름이 아니므로 걸리지 않는다.
    """
    name = filename or ""
    folder = str(PurePath(path).parent) if path else ""
    for text in (name, folder):
        if _AI.search(text):
            return AI_GENERATED
    if _PUBLICATION.search(name):
        return PUBLICATION
    return None


def reference_only(db, doc_ids) -> dict[int, str]:
    """업무의 값을 읽지 않을 참고물 {문서: 사유}."""
    ids = [int(i) for i in doc_ids]
    if not ids:
        return {}
    marks = ", ".join("?" * len(ids))
    out = {}
    for row in db.con.execute(
            f"SELECT id, filename, path FROM documents WHERE id IN ({marks})", ids):
        found = marker(row["filename"] or "", row["path"] or "")
        if found:
            out[row["id"]] = found
    return out


# ── 서식의 빈칸 ─────────────────────────────────────────────────────
# 숫자 0이나 동그라미·영문 O를 잇달아 적은 자리: '0000과', 'OO과', '○○○ 주무관', 'XX팀'
_FILLER = re.compile(r"(?<![\d가-힣A-Za-z])(?:0{3,}|[OoＯ○◯ㅇ△□XxＸ×*]{2,})(?=[가-힣]{0,3}(?:$|[\s,)/·]))")
# 국번과 번호가 모두 0인 전화번호: '000-000-0000', '044-000-0000', '☎ 00-0000-0000'
_ZERO_PHONE = re.compile(r"(?<!\d)\d{2,3}\s*[-)]\s*0{3,4}\s*-\s*0{4}(?!\d)")
_EXAMPLE_NAME = re.compile(r"홍길동|김철수|아무개|(?<![\w.])(?:abc|xxx|example)@")
# 빈칸을 빼고 나서도 값다운 것이 남는가. 기관 꼴의 일반명사만 남으면 빈칸이다.
_GENERIC = re.compile(
    r"시\s*[·ㆍ]\s*도|시\s*[·ㆍ]\s*군\s*[·ㆍ]\s*구|구\s*[·ㆍ]\s*군|기관명?|부서명?|담당자?|주무관|"
    r"[☎()\[\]/·ㆍ,:\-\s]|과|팀|실|계|국|전화|연락처|내선")


def is_placeholder(value: str) -> bool:
    """서식의 빈칸을 값으로 읽은 것인가 ('시·도/시·군·구 0000과', '(☎000-000-0000)')."""
    text = value or ""
    if _ZERO_PHONE.search(text) or _EXAMPLE_NAME.search(text):
        return True
    if not _FILLER.search(text):
        return False
    rest = _GENERIC.sub("", _FILLER.sub("", text))
    return len(rest) < 2
