"""질문을 검색에 맞게 다듬는다 — 바꿔 쓴 질의와, 후속 질문의 독립 질문화.

둘 다 **검색에 넣을 문장만** 만든다. 답을 만들지 않고, 근거가 되지도 않는다.
모델이 실패하면 조용히 원래 질문으로 돌아간다 — 다듬기는 편의이지 관문이
아니다.

업무노트 대화에서만 쓴다. 질문 화면(단발 질의)의 검색은 동결한 평가 세트로
맞춰 둔 것이라 건드리지 않는다.
"""

from __future__ import annotations

import re

from ..ai.prompts_loader import render
from ..ai.schemas import EXPAND_SCHEMA, REWRITE_SCHEMA

MAX_VARIANTS = 3
MAX_QUESTION_CHARS = 300
HISTORY_TURNS = 3
# 앞선 대화 없이는 뜻이 서지 않는 말. 이런 말이 없으면 독립 질문으로 본다 —
# 멀쩡한 질문까지 모델이 고쳐 쓰게 두면 묻는 것이 미묘하게 바뀐다.
_LEANS_ON_CONTEXT = re.compile(
    r"그것|그게|그건|그걸|그거|그때|그곳|거기|그럼|그러면|그렇다면|그중|그 ?밖에|"
    r"이것|이게|이건|위 |위의|앞서|방금|아까|"
    r"(?:이|그|저|해당|같은) ?(?:업무|사업|문서|자료|계약|건|내용|기한|금액|부서|담당자?)")
_CONTINUES = re.compile(r"^(?:그리고|또|또한|그런데|근데|그래서|추가로|더 )")


def needs_context(question: str) -> bool:
    """앞선 질문을 봐야 뜻이 통하는 후속 질문인가(규칙으로만 판정한다)."""
    text = question.strip()
    return bool(_LEANS_ON_CONTEXT.search(text) or _CONTINUES.search(text))


def _clean(value, original: str) -> str:
    text = " ".join(str(value or "").split()).strip("\"'`")
    if not text or len(text) > MAX_QUESTION_CHARS:
        return ""
    return "" if text == " ".join(original.split()) else text


def variants(client, question: str, limit: int = MAX_VARIANTS) -> list[str]:
    """같은 것을 찾는 다른 표현들. 실패하면 빈 목록."""
    if len(question) > MAX_QUESTION_CHARS or limit <= 0:
        return []
    prompt, _version = render("expand_query", question=question, limit=str(limit))
    data, error, _raw = client.generate_json(prompt, EXPAND_SCHEMA, num_predict=200)
    if error or not isinstance(data, dict):
        return []
    found: list[str] = []
    for value in data.get("variants") or []:
        text = _clean(value, question)
        if text and text not in found:
            found.append(text)
    return found[:limit]


def standalone(client, question: str, history: list[str]) -> str:
    """후속 질문을 혼자서도 뜻이 통하는 질문으로. 고칠 수 없으면 빈 문자열."""
    earlier = [q for q in history if q.strip()][-HISTORY_TURNS:]
    if not earlier:
        return ""
    listing = "\n".join(f"{i}. {q}" for i, q in enumerate(earlier, start=1))
    prompt, _version = render("rewrite_question", history=listing, question=question)
    data, error, _raw = client.generate_json(prompt, REWRITE_SCHEMA, num_predict=160)
    if error or not isinstance(data, dict):
        return ""
    return _clean(data.get("question"), question)
