"""최초 업무 분석의 결과 진입 조건. 처리 종료와 업무 결과 준비를 구분한다."""

from dataclasses import dataclass
import json

from ..db import Database


@dataclass(frozen=True)
class Outcome:
    state: str
    title: str
    message: str
    can_open: bool = False


def pending(db: Database) -> bool:
    marker = db.get_meta("initial_analysis_state")
    if marker:
        return marker != "ready"
    # 기존 프로젝트는 보관된 화면으로 들어간다. 새 최초 분석은 시작 전에 표시한다.
    return bool(db.work_sources()) and not db.con.execute(
        "SELECT 1 FROM documents d JOIN sources s ON s.id=d.source_id "
        "WHERE s.kind!='upload' LIMIT 1").fetchone()


def outcome(db: Database, *, running: bool = False, failed: bool = False) -> Outcome:
    state = db.get_meta("analysis_run_state")
    if running:
        return Outcome("running", "자료에서 업무를 파악하고 있습니다", "분석 결과가 저장되면 업무와 연간 일정을 보여 드립니다.")
    if failed or state == "failed":
        return Outcome("failed", "분석 중 오류가 발생했습니다", "저장된 처리 결과는 남아 있습니다. 분석 상태에서 원인을 확인하고 다시 시도하세요.")
    if state in ("running", "cancelled"):
        return Outcome("paused", "분석이 중단되었습니다", "저장된 지점부터 이어서 분석할 수 있습니다.")
    if state != "completed":
        return Outcome("waiting", "업무 분석을 시작할 준비가 됐습니다", "연결한 폴더를 읽어 업무·일정·처리 방법을 정리합니다.")
    counts = db.counts()
    task_count = len(db.tasks())
    work_documents = db.con.execute(
        "SELECT COUNT(*) FROM documents d JOIN sources s ON s.id=d.source_id "
        "WHERE s.kind!='upload' AND d.parse_status!='skipped' AND d.missing_since IS NULL").fetchone()[0]
    if not work_documents:
        return Outcome("empty", "분석할 문서를 찾지 못했습니다", "폴더 위치와 접근 권한을 확인하거나 다른 자료 폴더를 연결하세요.")
    discover = db.con.execute("SELECT state FROM pipeline_stage_results WHERE stage='업무 파악'").fetchone()
    if not discover or discover["state"] != "completed":
        return Outcome("unavailable", "업무 파악을 마치지 못했습니다", "분석 상태에서 보류·실패 원인과 로컬 AI 연결을 확인하세요. 읽은 자료는 보존됩니다.")
    if not task_count:
        return Outcome("no_tasks", "자료에서 업무를 파악하지 못했습니다", "맡은 업무가 없다는 뜻은 아닙니다. 업무별 폴더나 계획·요청·결과 문서를 보완해 다시 분석하세요.")
    reports = db.con.execute("SELECT state,errors FROM pipeline_stage_results").fetchall()
    partial = counts["parse_failed"] or any(r["state"] != "completed" or json.loads(r["errors"]) for r in reports)
    if partial:
        return Outcome("partial", "일부 자료를 분석하지 못했습니다", "파악한 업무 결과를 볼 수 있습니다. 분석 상태에서 읽지 못한 자료와 보류된 단계를 확인하세요.", True)
    return Outcome("ready", "업무 분석 결과가 준비됐습니다", f"자료에서 업무 {task_count}개를 파악했습니다. 업무를 열어 자료와 처리 방법을 살펴보세요.", True)
