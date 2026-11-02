"""상태 상세 — 상단 바를 누르면 뜬다.

단계별 진행 상황과 실패 목록을 보여준다. "일시중지"는 안전 지점에서
멈추고, "이어서 실행"은 문서 상태로 남은 일을 다시 찾아 이어간다 —
별도 재개 큐가 아니라 데이터 자체가 진행 상태이므로 멈췄다 다시 눌러도
같은 결과를 낸다(ING-006).
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import stages, theme
from .widgets import Card, UnknownBlock, clear_layout, muted_label, section_title
from ..core import coverage, initial_analysis
from ..jobs.pipeline import StageReport
import json

STAGE_ORDER = [
    "파일 찾기", "중복 확인", "내용 읽기", "그림·스캔본 읽기", "시점 확인", "본문 읽기", "의미 색인",
    "질문 준비", "업무 파악", "문서 갈래 확인", "일정 파악", "처리 순서 파악", "업무 복원",
]


class StatusDialog(QDialog):
    def __init__(self, window, parent: QWidget | None = None):
        super().__init__(parent)
        self._window = window
        self.setWindowTitle("분석 상태")
        self.setMinimumSize(520, 560)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(theme.SP_LG, theme.SP_LG, theme.SP_LG, theme.SP_LG)
        outer.setSpacing(theme.SP_MD)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        self.column = QVBoxLayout(body)
        self.column.setSpacing(theme.SP_MD)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        self.toggle = QPushButton()
        self.toggle.setObjectName("Primary")
        self.toggle.clicked.connect(self._toggle_pipeline)
        buttons.addWidget(self.toggle)
        buttons.addStretch(1)
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        outer.addLayout(buttons)

        self.refresh()

    def refresh(self) -> None:
        clear_layout(self.column)
        reports = self._window.stage_reports
        db = getattr(self._window, "db", None)
        if db is not None:
            reports = dict(reports)
            for row in db.con.execute("SELECT * FROM pipeline_stage_results"):
                reports.setdefault(row["stage"], StageReport(
                    row["stage"], row["done"], row["total"], row["note"],
                    json.loads(row["errors"]), row["state"]))
            summary = Card()
            summary.body.addWidget(section_title("분석 범위 요약"))
            for layer in initial_analysis.layers(db):
                line = initial_analysis.layer_line(layer)
                summary.body.addWidget(UnknownBlock(line) if layer.state == "failed" else muted_label(line))
            self.column.addWidget(summary)

            view = coverage.snapshot(db)
            card = Card()
            card.body.addWidget(section_title("파일 처리 범위"))
            states = view["parse_states"]
            card.body.addWidget(muted_label(
                f"등록 {view['registered_files']:,}건 · 원본 없음 {view['missing_files']:,}건\n"
                f"본문 추출 성공 {states.get('ok', 0):,}건 · 부분 추출 {states.get('partial', 0):,}건\n"
                f"추출 범위를 계측한 문서 {view['extraction_scope_measured_documents']:,}건 · "
                f"누락이 확인된 문서 {view['extraction_known_omission_documents']:,}건\n"
                f"의미 색인이 준비된 문서 {view['fully_indexed_documents']:,}건\n"
                f"업무 배정 {view['assigned_documents']:,}건 · 미배정 {view['unassigned_documents']:,}건"))
            card.body.addWidget(muted_label(
                "본문 추출 성공은 내용 전체를 빠짐없이 읽었다는 뜻이 아닙니다.", small=True))
            for scan in view["scans"]:
                if scan["run_id"] is None:
                    card.body.addWidget(muted_label("이전 조사의 제외·오류 내역이 없습니다. 다음 조사부터 기록합니다.", small=True))
                    continue
                card.body.addWidget(muted_label(
                    f"자료원 {scan['source_id']} · 관찰한 파일 {scan['observed_files']:,}건 · "
                    f"제외 {scan['excluded_files']:,}건 · 접근 오류 폴더 {scan['unreadable_directories']:,}개",
                    small=True))
                if scan["uninspected_directory_contents"]:
                    card.body.addWidget(muted_label("제외·접근 불능 폴더 안의 파일 수는 확인하지 못했습니다.", small=True))
                details = db.con.execute(
                    "SELECT path,state,reason FROM scan_entries WHERE run_id=? "
                    "AND state IN ('excluded','error') ORDER BY state,path LIMIT 20", (scan["run_id"],))
                from pathlib import Path
                reasons = {"temporary_file": "임시 파일", "user_exclusion": "사용자 제외 설정",
                           "managed_output": "앱 생성물", "link_not_followed": "연결 경로",
                           "default_directory_exclusion": "기본 제외 폴더", "stat_failed": "파일 접근 실패",
                           "directory_unreadable": "폴더 접근 실패", "hash_recheck_failed": "변경 재검사 실패"}
                for item in details:
                    card.body.addWidget(muted_label(
                        f"{Path(item['path']).name} · {reasons.get(item['reason'], item['reason'])}", small=True))
            if view["holds"]:
                card.body.addWidget(UnknownBlock(
                    f"{len(view['holds'])}개 문서 묶음을 같은 업무로 판단하지 못했습니다. 미배정 자료를 확인해 주세요."))
            self.column.addWidget(card)

        stages = Card()
        stages.body.addWidget(section_title("단계별 진행"))
        seen = set()
        for name in STAGE_ORDER:
            report = reports.get(name)
            seen.add(name)
            stages.body.addWidget(muted_label(_stage_line(name, report)))
        for name, report in reports.items():
            if name not in seen:
                stages.body.addWidget(muted_label(_stage_line(name, report)))
        self.column.addWidget(stages)

        errors = [
            (stage, message)
            for stage, report in reports.items()
            for message in report.errors
        ]
        if errors:
            failures = Card()
            failures.body.addWidget(section_title(f"실패 목록 ({len(errors)}건)"))
            for stage, message in errors[:50]:
                failures.body.addWidget(UnknownBlock(f"[{stage}] {message}"))
            if len(errors) > 50:
                failures.body.addWidget(muted_label(f"… 외 {len(errors) - 50:,}건", small=True))
            self.column.addWidget(failures)

        running = self._window.runner.running
        self.toggle.setText("일시중지" if running else "이어서 실행")
        self.toggle.setEnabled(True)

    def _toggle_pipeline(self) -> None:
        if self._window.runner.running:
            self._window.runner.stop(wait_ms=0)
        else:
            self._window._start_pipeline()
        self.refresh()


def _stage_line(name: str, report) -> str:
    """상세 화면에서만 기술 이름을 함께 적는다 (계획서 §24).

    문제가 났을 때 무엇을 뒤져야 하는지는 결국 기술 이름으로 물어야 한다.
    다만 그 이름이 필요한 자리는 여기 하나뿐이다.
    """
    label = stages.technical(name)
    if report is None:
        return f"○ {label}   아직 시작하지 않았습니다"
    state = getattr(report, "state", "")
    if state in ("partial", "cancelled", "pending", "running", "failed") or report.errors:
        status = {"partial": "일부 실패", "cancelled": "중단됨", "pending": "대기 중",
                  "running": "진행 중", "failed": "실패"}.get(state, "일부 실패")
        return f"△ {label}   {status} · {report.done:,} / {report.total:,} · {report.note}"
    if report.total <= 0:
        return f"· {label}   {report.note or '해당 없음'}"
    if report.done >= report.total:
        text = f"✓ {label}   {stages.done(name)} ({report.total:,}건)"
    else:
        text = f"⣾ {label}   {stages.running(name)} · {report.done:,} / {report.total:,}"
    if report.note:
        text += f"   · {report.note}"
    return text
