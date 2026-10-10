"""계획 틀 — 어떤 도구를 어떤 순서로 부르는가.

계획은 코드에 적혀 있다. 생성 모델이 세우지 않는다 — 같은 입력에는 같은 계획이
나와야 시험할 수 있고, 문서 속의 글이 단계를 더하거나 바꿀 수 없어야 한다.

단계는 앞 단계의 결과를 이름으로 받는다(needs). 받아야 할 단계가 끝나지 않았으면
그 단계는 건너뛴다. 근거가 모자랄 때의 갈림은 도구가 결과로 말하고, 다음 도구가
그 결과를 보고 정한다 — 계획 틀에 조건문을 두지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


def _pass_through(params: dict, outputs: dict) -> dict:
    return {**params, "inputs": outputs}


@dataclass(frozen=True)
class PlanStep:
    key: str                              # 이 계획 안에서의 이름
    tool: str
    needs: tuple[str, ...] = ()
    # (실행 인자, 앞 단계 결과) -> 도구 인자. 기본은 실행 인자에 앞 결과를 붙여 넘긴다.
    bind: Callable[[dict, dict], dict] = _pass_through


@dataclass(frozen=True)
class Plan:
    key: str
    version: str
    title: str
    steps: tuple[PlanStep, ...]

    def __post_init__(self):
        seen: set[str] = set()
        for step in self.steps:
            if step.key in seen:
                raise ValueError(f"계획 '{self.key}'에 같은 이름의 단계가 있습니다: {step.key}")
            missing = [need for need in step.needs if need not in seen]
            if missing:
                raise ValueError(
                    f"단계 '{step.key}'가 받는 단계가 앞에 없습니다: {', '.join(missing)}")
            seen.add(step.key)


class Plans:
    def __init__(self) -> None:
        self._plans: dict[str, Plan] = {}

    def register(self, plan: Plan) -> Plan:
        if plan.key in self._plans:
            raise ValueError(f"같은 이름의 계획 틀이 이미 있습니다: {plan.key}")
        self._plans[plan.key] = plan
        return plan

    def get(self, key: str) -> Plan:
        try:
            return self._plans[key]
        except KeyError:
            raise ValueError(f"없는 계획 틀입니다: {key}") from None

    def keys(self) -> list[str]:
        return sorted(self._plans)


PLANS = Plans()
