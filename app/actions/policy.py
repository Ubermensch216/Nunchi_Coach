"""위험 등급과 승인 규칙 (행동형 업무 비서 계획서 5.1절).

    read      R0  프로젝트 DB와 원문을 읽는다
    draft     R1  outputs/actions 아래에 새 파일을 만든다
    change    R2  이미 있는 산출물을 확정·교체하거나 업무기억을 바꾼다
    external  R3  다른 시스템에 변화를 일으킨다 (발송·결재 상신)

승인을 모델의 판단이나 화면의 단추에 맡기지 않는다. 실행기가 도구를 부르기
직전에 여기서 정한 규칙을 본다 — 화면이 승인 단계를 빠뜨려도 변경 등급 도구는
돌지 않는다.

외부 등급 도구는 등록부가 받지 않는다. 실제 기관 연결이 없는 지금, 그런 도구가
'승인만 받으면 도는' 상태로 존재하는 것 자체가 위험이다.
"""

from __future__ import annotations

import hashlib
import json

READ, DRAFT, CHANGE, EXTERNAL = "read", "draft", "change", "external"
REGISTRABLE = (READ, DRAFT, CHANGE)

RISK_LABEL = {READ: "자료 읽기", DRAFT: "새 초안 만들기", CHANGE: "확정·변경",
              EXTERNAL: "외부 전송"}

APPROVED, REJECTED = "approved", "rejected"


def needs_approval(risk: str) -> bool:
    return risk == CHANGE


def writes_files(risk: str) -> bool:
    return risk in (DRAFT, CHANGE)


def digest(value) -> str:
    """값의 지문. 열쇠 순서나 공백이 달라도 같은 값이면 같은 지문이 나온다."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scope_hash(tool: str, version: str, scope) -> str:
    """승인이 묶이는 내용의 지문. 도구·판·대상 가운데 하나라도 달라지면 달라진다."""
    return digest({"tool": tool, "version": version, "scope": scope})
