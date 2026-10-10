"""등록된 도구와 계획 틀, 그리고 화면·시험이 부르는 시작점.

도구 모듈은 불러오는 순간 등록부에 자기를 등록한다. 이 모듈을 불러오면 쓸 수 있는
도구와 계획 틀이 전부 갖춰진다.
"""

from __future__ import annotations

from datetime import date

from ..core import cases
from .plans import PLANS, Plan, PlanStep
from .registry import REGISTRY
from .runner import Runner
from .tools import checklist, checks, compare, discover, form   # noqa: F401  (등록)

RECURRING_PREP = "recurring_prep"

_ALL = ("discover", "classify", "compare", "form", "checklist")

if RECURRING_PREP not in PLANS.keys():
    PLANS.register(Plan(RECURRING_PREP, "1", "반복 업무 준비", (
        PlanStep("discover", "discover"),
        PlanStep("classify", "classify", needs=("discover",)),
        PlanStep("compare", "compare", needs=("discover", "classify")),
        PlanStep("form", "form", needs=("discover", "classify")),
        PlanStep("checklist", "checklist", needs=("discover", "classify", "compare", "form")),
        PlanStep("verify", "verify", needs=_ALL),
        PlanStep("register", "register_checklist", needs=("checklist", "verify")),
    )))


def prepare(db, task_id: int, target_year: int | None = None, today: date | None = None,
            recommendation_id: int | None = None, **runner_options) -> tuple[Runner, int]:
    """‘준비 작업 실행’ — 수행 건을 열고 실행을 만든다. 돌리는 것은 부르는 쪽이 한다.

    같은 업무·연도·자료이면 있던 수행 건과 실행을 돌려준다.
    """
    today = today or date.today()
    year = target_year or today.year
    case_id = cases.open_case(db, task_id, year)
    runner = Runner(db, REGISTRY, PLANS, today=today, **runner_options)
    return runner, runner.start(RECURRING_PREP, task_id, year, case_id=case_id,
                                recommendation_id=recommendation_id)
