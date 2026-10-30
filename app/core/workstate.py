"""업무 복원·수행의 상태 어휘 (구축계획서 §5.2).

한 항목에 대해 서로 다른 네 가지를 묻는다. 한 열에 섞으면 '문서에는
적혀 있지만 아무도 확인하지 않은 과거 기한'을 표현할 수 없다.

    근거 상태    자료가 무엇을 말하는가       stated / inferred / conflict / missing
    적용 시점    지금 대상에도 맞는가         current / past / unknown
    사람 확인    누가 확인했는가              none / predecessor / successor / recheck
    수행 상태    후임자가 어디까지 했는가     not_started / in_progress / done / waiting

사람 확인이 없어도(none) 근거를 보고 수행 안내를 쓸 수 있다. 확인을 기다리는
상태를 따로 두지 않는 이유다 — 전임자가 없다는 것이 일을 멈출 사유가 아니다.

화면에 보일 때는 제품 전체가 쓰는 4단계(core/status.py)로 접는다. 새 신뢰
체계를 하나 더 배우게 하지 않는다.
"""

from __future__ import annotations

from . import status

# 근거 상태
STATED = "stated"          # 문서에 명시되어 있다
INFERRED = "inferred"      # 자료에서 추정했다 (파일 순서·날짜 등)
CONFLICT = "conflict"      # 자료끼리 다르게 말한다
MISSING = "missing"        # 자료에서 찾지 못했다
BASIS = (STATED, INFERRED, CONFLICT, MISSING)

# 적용 시점
CURRENT = "current"        # 현재 대상에 적용됨을 확인했다
PAST = "past"              # 과거 사례다
UNKNOWN = "unknown"        # 지금도 적용되는지 알 수 없다
APPLICABILITY = (CURRENT, PAST, UNKNOWN)

# 사람 확인
NONE = "none"
PREDECESSOR = "predecessor"
SUCCESSOR = "successor"
RECHECK = "recheck"        # 확인한 뒤 자료가 달라졌다
HUMAN = (NONE, PREDECESSOR, SUCCESSOR, RECHECK)
CONFIRMED_BY = (PREDECESSOR, SUCCESSOR)

# 수행 상태
NOT_STARTED = "not_started"
IN_PROGRESS = "in_progress"
DONE = "done"
WAITING = "waiting"        # 이 단계만 특정 확인을 기다린다
RUN = (NOT_STARTED, IN_PROGRESS, DONE, WAITING)

# 절차 유형
PROCEDURE_STATED = "stated"        # 문서에 적힌 절차
PROCEDURE_PRECEDENT = "precedent"  # 과거에 이렇게 처리한 사례
PROCEDURE_SUGGESTED = "suggested"  # 시스템이 제안한 준비 행동
PROCEDURE_TOLD = "told"            # 문서에는 없고 사람이 알려 준 단계
PROCEDURE = (PROCEDURE_STATED, PROCEDURE_PRECEDENT, PROCEDURE_SUGGESTED, PROCEDURE_TOLD)

# 시작 방식
MODE_MATERIALS = "materials"
MODE_WITH_PREDECESSOR = "with_predecessor"
MODES = (MODE_MATERIALS, MODE_WITH_PREDECESSOR)

# 스냅샷 종류
SNAPSHOT_RESTORATION = "restoration"   # 자료에서 자동으로 복원한 업무 복원본
SNAPSHOT_HANDOVER = "handover"         # 전임자가 검토해 발행한 인계본
SNAPSHOT_KINDS = (SNAPSHOT_RESTORATION, SNAPSHOT_HANDOVER)
SNAPSHOT_LABEL = {SNAPSHOT_RESTORATION: "자료 기반 업무 복원본",
                  SNAPSHOT_HANDOVER: "전임자 검토 인계본"}

# 검토 행동
REVIEW_ACTIONS = ("confirm", "edit", "supplement", "exclude")
GAP_KINDS = ("missing", "conflict", "stale", "changed",
             "unanswered_official", "unregistered_final", "missing_attachment")

_BASIS_LABEL = {STATED: "문서에 명시", INFERRED: "자료에서 추정",
                CONFLICT: "자료가 서로 다름", MISSING: "자료 없음"}
_APPLICABILITY_LABEL = {CURRENT: "현재 대상에 적용", PAST: "과거 사례",
                        UNKNOWN: "적용 여부 미상"}
_HUMAN_LABEL = {NONE: "사람 확인 없음", PREDECESSOR: "전임자 확인",
                SUCCESSOR: "후임자 확인", RECHECK: "재확인 필요"}
_RUN_LABEL = {NOT_STARTED: "시작 전", IN_PROGRESS: "진행 중", DONE: "완료",
              WAITING: "확인 대기"}
_PROCEDURE_LABEL = {PROCEDURE_STATED: "문서에 적힌 절차",
                    PROCEDURE_PRECEDENT: "과거 수행 사례",
                    PROCEDURE_SUGGESTED: "시스템 제안",
                    PROCEDURE_TOLD: "사람이 알려 준 단계"}
ROLE_LABEL = {PREDECESSOR: "전임자", SUCCESSOR: "후임자"}


def basis_label(value: str) -> str:
    return _BASIS_LABEL.get(value, _BASIS_LABEL[MISSING])


def applicability_label(value: str) -> str:
    return _APPLICABILITY_LABEL.get(value, _APPLICABILITY_LABEL[UNKNOWN])


def human_label(value: str) -> str:
    return _HUMAN_LABEL.get(value, _HUMAN_LABEL[NONE])


def run_label(value: str) -> str:
    return _RUN_LABEL.get(value, _RUN_LABEL[NOT_STARTED])


def procedure_label(value: str) -> str:
    return _PROCEDURE_LABEL.get(value, _PROCEDURE_LABEL[PROCEDURE_PRECEDENT])


def require(value: str, allowed: tuple[str, ...], name: str) -> str:
    """알 수 없는 상태값을 조용히 저장하지 않는다 — 화면이 해석하지 못한다."""
    if value not in allowed:
        raise ValueError(f"알 수 없는 {name}: {value!r}")
    return value


def display_state(basis_state: str, human_state: str = NONE) -> str:
    """네 축 중 신뢰에 관한 둘을 제품 공통의 4단계로 접는다.

    사람이 확인했으면 근거가 추정이어도 확인된 지식이다. 다만 확인 뒤에
    자료가 달라졌으면(recheck) 그 확인을 그대로 믿으라고 하지 않는다.
    """
    if human_state in CONFIRMED_BY:
        return status.CONFIRMED
    if basis_state == MISSING:
        return status.UNKNOWN
    if human_state == RECHECK or basis_state == CONFLICT:
        return status.WEAK
    return status.INFERRED


def describe(basis_state: str, applicability: str, human_state: str) -> str:
    """'문서에 명시 · 과거 사례 · 사람 확인 없음' — 세 축을 숨기지 않고 나란히."""
    return " · ".join((basis_label(basis_state), applicability_label(applicability),
                       human_label(human_state)))
