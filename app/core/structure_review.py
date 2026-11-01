"""업무 체계 확인 (S4) — 집중 분석 전에 사람이 업무 목록을 한 번 보는 자리.

막지 않는다. 확인하지 않아도 분석은 제안대로 끝까지 돈다. 확인하면서 이름을 고치거나
합치거나 '업무 아님'으로 내린 업무는 기존 교정 기능(사람의 결정)으로 남고, 다음 분석이
바뀐 업무만 다시 본다(S5의 지문 비교).

    pending(db)   아직 확인도 미루기도 하지 않았고 볼 업무가 있다
    rows(db)      확인할 업무 목록 — 이름, 어디서 세웠나, 문서 수
    confirm(db)   이대로 확정
    later(db)     나중에 확인
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import bundles

STATE_META = "structure_review"
CONFIRMED, LATER = "confirmed", "later"

# 업무를 세운 단서. 화면에 출처로 적는다.
CHARTER, OFFICIAL, FOLDER, CONTENT, PERSON = "분장", "공문", "폴더", "본문", "사람"


@dataclass(frozen=True)
class Row:
    task_id: int
    name: str
    basis: str
    doc_count: int
    bundle: bool          # 참고 묶음·낱장 — 일이 아니다


def state(db) -> str:
    return db.get_meta(STATE_META) or ""


def pending(db) -> bool:
    return state(db) not in (CONFIRMED, LATER) and bool(rows(db))


def _charter_names(db) -> set[str]:
    raw = db.get_meta("charter_comparison") or ""
    try:
        matched = json.loads(raw).get("matched", []) if raw else []
    except ValueError:
        return set()
    return {item["name"] if isinstance(item, dict) else item for item in matched}


def _basis(db, task, charter_names: set[str]) -> str:
    if task["origin"] == "user":
        return PERSON
    evidence = [row[0] or "" for row in db.con.execute(
        "SELECT evidence FROM task_docs WHERE task_id = ?", (task["id"],))]
    if any(e.startswith("온나라 공문") for e in evidence):
        return OFFICIAL
    if task["name"] in charter_names or "(업무분장:" in (task["description"] or ""):
        return CHARTER
    if any(e == "폴더 위치" for e in evidence):
        return FOLDER
    return CONTENT


def rows(db) -> list[Row]:
    """확인할 업무. 자료가 많은 순이고, 묶음은 맨 뒤다."""
    charter_names = _charter_names(db)
    out = []
    for task in db.con.execute(
            "SELECT t.id, t.name, t.origin, t.description, COUNT(d.id) AS docs FROM tasks t "
            "LEFT JOIN task_docs td ON td.task_id = t.id "
            "LEFT JOIN documents d ON d.id = td.doc_id AND d.missing_since IS NULL "
            "WHERE t.not_a_task = 0 AND t.merged_into IS NULL GROUP BY t.id"):
        out.append(Row(task["id"], task["name"], _basis(db, task, charter_names),
                       task["docs"], bool(bundles.kind(db, task["id"]))))
    return sorted(out, key=lambda row: (row.bundle, -row.doc_count, row.name))


def confirm(db) -> None:
    db.set_meta(STATE_META, CONFIRMED)
    db.audit("structure.confirm", result=str(len(rows(db))))


def later(db) -> None:
    db.set_meta(STATE_META, LATER)
    db.audit("structure.later")
