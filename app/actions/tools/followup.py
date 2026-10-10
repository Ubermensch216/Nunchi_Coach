"""후속 실행 도구들이 함께 쓰는 것 — 수행 건에서 업무·올해 기준·만들어 둔 서식을 찾는다.

후속 실행(요청문 초안·회신 취합)은 준비 작업이 끝난 수행 건 위에서 돈다. 준비 작업의
산출물(서식)이 없거나 낡았으면 사용자가 할 일이 있으므로 멈춘다(Blocked).
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import openpyxl

from ...core import yearcompare as yc
from ..registry import Blocked
from . import common, discover


@dataclass
class Inputs:
    case: object
    name: str
    found: dict                    # discover의 출력
    doc_ids: list[int]
    mat: yc.Materials
    target: common.Target


def case_inputs(context) -> Inputs:
    db = context.db
    case = db.case(context.case_id) if context.case_id else None
    if case is None:
        raise Blocked("이 작업을 할 수행 건이 없습니다. 업무에서 ‘준비 작업’으로 시작해 주세요.")
    found = discover.run_discover(context, {"task_id": case["task_id"]})
    doc_ids = [item["id"] for item in found["included"]]
    mat = yc.materials(db, doc_ids)
    target = common.judge_target(db, case["id"], doc_ids, case["period_year"], mat)
    return Inputs(case, common.task_name(db, case["task_id"]), found, doc_ids, mat, target)


def form_artifact(db, case_id: int):
    """수행 건의 가장 최근 취합 서식. 없거나 낡았으면 사용자의 조치가 필요하다."""
    rows = [row for row in db.artifacts(case_id=case_id) if row["kind"] == "collection_form"]
    if not rows:
        raise Blocked("만들어 둔 취합 서식이 없습니다. 먼저 ‘준비 작업’을 실행해 서식을 만들어 주세요.")
    usable = [row for row in rows if row["state"] != "stale"]
    if not usable:
        raise Blocked("취합 서식이 낡았습니다 (올해 기준이 달라졌습니다). ‘준비 작업’을 다시 실행해 서식을 새로 만들어 주세요.")
    return usable[-1]


def type_names(form_row) -> set[str]:
    """서식의 02_평가지표 시트에 적힌 지표(구분) 이름. 읽지 못하면 빈 집합이다."""
    try:
        with open(form_row["path"], "rb") as handle:
            workbook = openpyxl.load_workbook(io.BytesIO(handle.read()), read_only=True)
        sheet = workbook["02_평가지표"]
        return {str(row[0]).strip() for row in sheet.iter_rows(min_row=2, values_only=True)
                if row and row[0] and not str(row[0]).startswith("(")}
    except (OSError, KeyError, ValueError):
        return set()
