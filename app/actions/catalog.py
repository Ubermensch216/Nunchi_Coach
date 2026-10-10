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
from .tools import checklist, checks, compare, discover, form, intake, request   # noqa: F401  (등록)

RECURRING_PREP = "recurring_prep"
COLLECTION_REQUEST = "collection_request"
COLLECTION_INTAKE = "collection_intake"

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

if COLLECTION_REQUEST not in PLANS.keys():
    PLANS.register(Plan(COLLECTION_REQUEST, "1", "자료 제출 요청 준비", (
        PlanStep("request_draft", "request_draft"),
        PlanStep("verify", "verify_run", needs=("request_draft",)),
        PlanStep("finalize", "finalize_form", needs=("request_draft", "verify")),
    )))

if COLLECTION_INTAKE not in PLANS.keys():
    PLANS.register(Plan(COLLECTION_INTAKE, "1", "회신 취합", (
        PlanStep("read_replies", "read_replies"),
        PlanStep("check_evidence", "check_evidence", needs=("read_replies",)),
        PlanStep("report", "collection_report", needs=("read_replies", "check_evidence")),
        PlanStep("verify", "verify_run", needs=("read_replies", "check_evidence", "report")),
    )))


def start_plan(db, plan_key: str, case_id: int, params: dict | None = None, extra_inputs: list[str] = (),
               today: date | None = None, recommendation_id: int | None = None, **runner_options):
    """수행 건 위에서 후속 계획 틀을 시작한다. (실행기, 실행 id). 돌리는 것은 부르는 쪽이 한다.

    그 업무에 묶인 문서의 판에 extra_inputs(예: 회신 파일, 만들어 둔 서식)를 얹어 실행의 지문을
    만든다. 같은 자료로 다시 누르면 같은 실행이고, 자료가 달라지면 새 실행이다.
    """
    case = db.case(case_id)
    if case is None:
        raise ValueError("없는 수행 건입니다.")
    runner = Runner(db, REGISTRY, PLANS, today=today or date.today(), **runner_options)
    return runner, runner.start(plan_key, case["task_id"], case["period_year"], params=params, case_id=case_id,
                                recommendation_id=recommendation_id, extra_inputs=extra_inputs)


def prepare_request(db, case_id: int, **options):
    """서식 확정과 요청문 초안 — 서식이 달라지면 새 실행이 되도록 서식의 지문을 딛는 자료로 삼는다."""
    forms = [row["sha256"] for row in db.artifacts(case_id=case_id) if row["kind"] == "collection_form"]
    return start_plan(db, COLLECTION_REQUEST, case_id, extra_inputs=forms, **options)


def prepare_intake(db, case_id: int, folder, **options):
    """회신 취합. 회신 엑셀의 내용과 그 밖 파일(증빙 후보)의 이름·크기가 같으면 같은 실행이다."""
    from pathlib import Path

    from . import replies

    root = Path(folder)
    listed = replies.list_files(root) if root.is_dir() else []
    shas = [replies._sha256(path) for path in listed[:replies.MAX_FILES]]
    others = sorted(f"{path.relative_to(root).as_posix()}:{path.stat().st_size}" for path in root.rglob("*")
                    if path.is_file() and path not in listed) if root.is_dir() else []
    return start_plan(db, COLLECTION_INTAKE, case_id, params={"folder": str(root)},
                      extra_inputs=shas + others + [row["sha256"] for row in db.artifacts(case_id=case_id)
                                              if row["kind"] == "collection_form"], **options)
