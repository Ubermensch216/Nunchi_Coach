"""일이 아닌 묶음 — 참고 자료 묶음과 낱장 묶음.

업무 목록에는 처리할 일이 아닌 묶음이 섞여 있다. 실제 자료에서 법령·지침만 모아 둔
폴더가 업무로 올라와 '문서 처리' 단계 셋과 "2026년 자료가 없습니다" 공백이 붙었고,
어느 업무 폴더에도 들지 않은 낱장 열 건(구·군 담당자 명단, 분기 제출 건수, 회의 자료)은
미분류로 남아 어디에도 보이지 않았다.

이런 묶음은 목록에 그대로 두되 일처럼 다루지 않는다: 처리 순서·반복 주기·공백을
만들지 않는다. 사람이 업무로 확정하거나 문서를 옮기면 보통 업무와 같다.
"""

from __future__ import annotations

from . import docrole

REFERENCE = "reference"     # 법령·규정·받은 자료뿐이고 우리가 만든 문서가 없다
LOOSE = "loose"             # 업무 폴더 밖의 낱장을 모은 것
LOOSE_NAME = "수시 조사·제출 자료"
LOOSE_EVIDENCE = "업무 폴더 밖의 낱장"
LOOSE_MIN = 3               # 낱장이 이만큼은 돼야 묶음으로 올린다
_REFERENCE_ROLES = frozenset({docrole.LEGAL, docrole.REFERENCE})

NOTE = {
    REFERENCE: "참고 자료 묶음입니다. 처리할 일이 아니라 다른 업무의 근거로 볼 자료입니다.",
    LOOSE: ("어느 업무 폴더에도 들지 않은 낱장을 모은 것입니다. 수시로 받은 조사·제출 요청, 명단, "
            "회의 자료가 섞여 있습니다. 순서나 주기가 있는 한 가지 일이 아닙니다."),
}


def kind(db, task_id: int) -> str | None:
    """이 업무가 일이 아닌 묶음이면 그 종류. 사람이 확정한 업무는 묶음으로 보지 않는다."""
    task = db.con.execute("SELECT origin, status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None or task["origin"] != "ai" or task["status"] != "proposed":
        return None
    rows = db.con.execute(
        "SELECT td.evidence, r.role FROM task_docs td JOIN documents d ON d.id = td.doc_id "
        "LEFT JOIN doc_roles r ON r.doc_id = td.doc_id "
        "WHERE td.task_id = ? AND d.missing_since IS NULL", (task_id,)).fetchall()
    if not rows:
        return None
    if all(row["evidence"] == LOOSE_EVIDENCE for row in rows):
        return LOOSE
    roles = [row["role"] for row in rows]
    # 갈래가 아직 정해지지 않은 문서가 있으면 판단하지 않는다.
    if all(roles) and all(role in _REFERENCE_ROLES for role in roles):
        return REFERENCE
    return None
