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
    if stale_sources(db):
        # 마지막 폴더 조사는 실패했고, 화면의 결과는 그 전에 읽어 둔 자료로 만든 것이다.
        # '준비됨'으로 보이면 지금 폴더를 반영한 결과로 오해한다(R09).
        return Outcome("partial", "이전에 읽은 자료로 만든 결과입니다",
                       f"마지막 폴더 조사에서 원본 폴더를 읽지 못했습니다. 이전에 읽어 둔 문서 "
                       f"{work_documents:,}건으로 만든 업무 결과를 보여 드립니다. 폴더 연결을 확인한 뒤 "
                       f"다시 분석하세요.", True)
    if partial:
        return Outcome("partial", "일부 자료를 분석하지 못했습니다", "파악한 업무 결과를 볼 수 있습니다. 분석 상태에서 읽지 못한 자료와 보류된 단계를 확인하세요.", True)
    return Outcome("ready", "업무 분석 결과가 준비됐습니다", f"자료에서 업무 {task_count}개를 파악했습니다. 업무를 열어 자료와 처리 방법을 살펴보세요.", True)


# ── 처리 범위를 층별로 (규제개혁 비교연구 R09) ───────────────────────
# 'ready' 하나로는 무엇이 끝났는지 알 수 없다. 원본 접근·본문 추출·업무 구조·검색 색인·
# 사람 검토는 서로 다른 일이고, 한쪽이 실패해도 다른 쪽 결과는 남는다. 실패를 감추지도,
# 실패 하나로 전체를 못 쓰게 하지도 않는다.

@dataclass(frozen=True)
class Layer:
    name: str
    state: str          # done | partial | pending | failed
    detail: str
    updated_at: str = ""


_STATE_WORD = {"done": "완료", "partial": "일부", "pending": "보류", "failed": "실패"}


def stale_sources(db: Database) -> bool:
    """마지막 폴더 조사가 실패했는데, 그 전에 읽어 둔 문서로 결과를 보여 주고 있는가."""
    row = db.con.execute(
        "SELECT errors FROM pipeline_stage_results WHERE stage='파일 찾기'").fetchone()
    if row is None or not json.loads(row["errors"] or "[]"):
        return False
    return bool(db.con.execute(
        "SELECT 1 FROM documents d JOIN sources s ON s.id=d.source_id "
        "WHERE s.kind!='upload' AND d.parse_status IN ('ok','partial') AND d.missing_since IS NULL "
        "LIMIT 1").fetchone())


def layers(db: Database) -> list[Layer]:
    """원본 접근 → 본문 추출 → 업무 구조 → 검색 색인 → 사람 검토. 분모를 함께 적는다."""
    stages = {row["stage"]: row for row in db.con.execute("SELECT * FROM pipeline_stage_results")}

    def stage(name: str):
        return stages.get(name)

    def when(*names: str) -> str:
        return max((stages[n]["updated_at"] or "" for n in names if n in stages), default="")

    out: list[Layer] = []
    scan = stage("파일 찾기")
    if scan is None:
        out.append(Layer("원본 접근", "pending", "아직 폴더를 조사하지 않았습니다."))
    elif json.loads(scan["errors"] or "[]"):
        detail = "마지막 조사에서 원본 폴더를 읽지 못했습니다"
        if stale_sources(db):
            detail += " — 이전에 읽어 둔 자료로 결과를 보여 주는 중입니다"
        out.append(Layer("원본 접근", "failed", detail + ".", when("파일 찾기")))
    else:
        out.append(Layer("원본 접근", "done", scan["note"] or "폴더를 조사했습니다.", when("파일 찾기")))

    states = dict(db.con.execute(
        "SELECT d.parse_status, COUNT(*) FROM documents d JOIN sources s ON s.id=d.source_id "
        "WHERE s.kind!='upload' AND d.missing_since IS NULL GROUP BY d.parse_status").fetchall())
    total = sum(states.values())
    read, part = states.get("ok", 0), states.get("partial", 0)
    protected = states.get("encrypted", 0)
    unread = total - read - part - protected
    extraction = "done" if total and not part and not unread and not protected else (
        "partial" if read or part else "pending")
    out.append(Layer("본문 추출", extraction,
                     f"문서 {total:,}건 중 본문 읽음 {read:,} · 일부만 읽음 {part:,} · "
                     f"암호·보호(DRM) {protected:,} · 읽지 못함·건너뜀 {unread:,}. "
                     f"읽지 못한 문서에 대해 '자료에 없음'이라고 말하지 않습니다.",
                     when("내용 읽기", "그림·스캔본 읽기")))

    structure = [stage(n) for n in ("업무 파악", "문서 갈래 확인", "일정 파악", "처리 순서 파악", "업무 복원")]
    if not any(structure):
        out.append(Layer("업무 구조", "pending", "아직 업무를 파악하지 않았습니다."))
    else:
        done = all(row is not None and row["state"] == "completed" for row in structure)
        tasks = len(db.tasks())
        out.append(Layer("업무 구조", "done" if done else "partial",
                         f"업무 {tasks}개 · 규칙으로 읽은 항목·단계" + ("" if done else " — 일부 단계가 끝나지 않았습니다"),
                         when("업무 파악", "문서 갈래 확인", "일정 파악", "처리 순서 파악", "업무 복원")))

    index = [stage(n) for n in ("의미 색인", "질문 준비")]
    if any(row is not None and row["state"] != "completed" for row in index):
        note = next((row["note"] for row in index if row is not None and row["state"] != "completed"), "")
        out.append(Layer("검색 색인", "pending",
                         f"의미 검색이 준비되지 않았습니다({note}). 파일명·본문 글자 검색과 규칙으로 읽은 결과는 쓸 수 있습니다.",
                         when("의미 색인", "질문 준비")))
    elif any(index):
        out.append(Layer("검색 색인", "done", "의미 검색과 질문 준비를 마쳤습니다.", when("의미 색인", "질문 준비")))
    else:
        out.append(Layer("검색 색인", "pending", "아직 색인하지 않았습니다."))

    reviewed = db.con.execute(
        "SELECT COUNT(*) FROM task_items WHERE excluded=0 AND human_state IN ('predecessor','successor')").fetchone()[0]
    items = db.con.execute("SELECT COUNT(*) FROM task_items WHERE excluded=0").fetchone()[0]
    out.append(Layer("사람 검토", "done" if items and reviewed == items else ("partial" if reviewed else "pending"),
                     f"업무 항목 {items:,}건 중 담당자가 확인한 것 {reviewed:,}건. 확인 전 항목은 자료에서 읽은 후보입니다."))
    return out


def layer_line(layer: Layer) -> str:
    stamp = f" · 마지막 갱신 {layer.updated_at}" if layer.updated_at else ""
    return f"{layer.name} — {_STATE_WORD.get(layer.state, layer.state)}: {layer.detail}{stamp}"
