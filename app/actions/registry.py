"""도구 등록부 — 실행기가 부를 수 있는 것의 전부.

도구 하나는 (손잡이, 인자) → 결과다. 결과는 다음 단계로 넘길 식별자와 수치의
사전이고 DB에 그대로 적힌다. 본문을 담지 않는다.

여기 없는 것은 실행되지 않는다. 계획 틀은 도구를 이름으로만 부르고, 문서의
내용이 그 이름을 바꿀 길은 없다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from . import policy


class ToolError(RuntimeError):
    """도구가 일을 끝내지 못했다. 메시지는 사용자에게 그대로 보여도 되는 한글이다."""


class Blocked(ToolError):
    """사용자의 조치가 있어야 이어갈 수 있다 (접근 권한, 빠진 자료 등).

    다시 시도해 봐야 같은 결과이므로 재시도 횟수를 쓰지 않는다.
    """


@dataclass(frozen=True)
class Tool:
    name: str
    version: str
    risk: str
    run: Callable[..., dict]              # (context, params) -> 결과
    label: str = ""                       # 화면에 보일 이름
    needs_model: bool = False
    # 승인이 묶일 대상. (db, params) -> 지문을 낼 값. 없으면 인자 전체가 대상이다.
    scope: Callable[..., object] | None = None


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.risk == policy.EXTERNAL:
            raise ValueError(
                f"외부 전송 등급의 도구는 등록할 수 없습니다: {tool.name}. "
                "실제 기관 연결이 검증되기 전에는 발송·결재 도구를 두지 않습니다.")
        if tool.risk not in policy.REGISTRABLE:
            raise ValueError(f"위험 등급을 알 수 없는 도구입니다: {tool.name} ({tool.risk!r})")
        if not tool.name or not tool.version:
            raise ValueError("도구에는 이름과 판이 있어야 합니다.")
        if tool.name in self._tools:
            raise ValueError(f"같은 이름의 도구가 이미 있습니다: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolError(f"등록되지 않은 도구입니다: {name}") from None

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)


REGISTRY = Registry()
