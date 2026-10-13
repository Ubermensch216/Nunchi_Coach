"""남의 사례를 옮겨 실은 문서 가려내기.

전임자가 만든 내부 문서라도 본문 대부분이 **다른 부서의 사례**인 것이 있다 — 우수사례
추천 현황, 위원회 심의자료, 중점과제를 실은 실행계획. 갈래로는 내부 문서(계획·회의
자료)라서 걸러지지 않는데, 거기 적힌 사업비·예산은 사례의 것이지 이 업무의 비용이
아니다. 실제 자료에서 금액 항목 15건 가운데 14건이 이런 문서에서 나왔다.

문서 한 건을 통째로 본다. 사례를 서술하는 말(우수사례, 추진성과, 사업내용 …)이 여러 번
나오고 서로 다른 금액이 여럿 적혀 있으면 사례 모음이다. 실측에서 둘은 뚜렷이 갈렸다 —
사례 모음은 단서 13~92회·금액 12~71가지, 업무 자체의 계획서·지급 문서는 단서 0~5회·
금액 1~5가지였다.

발표 자료(pptx)도 같다. 사례를 낸 부서가 만든 것이다.
"""

from __future__ import annotations

import re

CASE_CUES = 8        # 사례를 서술하는 말이 이만큼 나오고
CASE_AMOUNTS = 6     # 서로 다른 금액이 이만큼 적혀 있으면 사례 모음이다

_CUE = re.compile(r"우수\s*사례|사례\s*명|추진\s*(?:성과|실적|배경)|사업\s*(?:내용|개요)|공적")
MONEY = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:원|천원|만원|억원|백만원|억)")
_SLIDES = (".pptx", ".ppt")


def amounts_in(text: str) -> set[str]:
    """글에 적힌 서로 다른 금액."""
    return {re.sub(r"\s+", "", found) for found in MONEY.findall(text or "")}


def is_case_collection(text: str) -> bool:
    return len(_CUE.findall(text or "")) >= CASE_CUES and len(amounts_in(text)) >= CASE_AMOUNTS


def collections(db, doc_ids) -> set[int]:
    """남의 사례를 옮겨 실은 문서. 금액·담당·문장을 이 업무의 것으로 읽지 않을 문서다."""
    ids = [int(i) for i in doc_ids]
    if not ids:
        return set()
    marks = ", ".join("?" * len(ids))
    out = {row["id"] for row in db.con.execute(
        f"SELECT id FROM documents WHERE id IN ({marks}) AND LOWER(COALESCE(ext, '')) IN (?, ?)",
        (*ids, *_SLIDES))}
    for row in db.con.execute(
            f"SELECT doc_id, body FROM document_index WHERE doc_id IN ({marks})", ids):
        if row["doc_id"] not in out and is_case_collection(row["body"]):
            out.add(row["doc_id"])
    return out
