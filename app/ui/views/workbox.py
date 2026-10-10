"""준비 작업 창 — 한 업무의 올해 수행 건을 만들고, 실행·승인·산출물을 한 곳에서 본다.

업무 화면의 ‘지금 할 일’ 탭에서 연다. 하는 일은 이렇다.

    ① 연도를 고르고 ‘준비 작업 실행’을 누른다 (수행 건이 만들어진다)
    ② 관련 자료 찾기 → 비교 → 취합 서식 → 체크리스트 초안 → 검증이 차례로 돈다
    ③ 체크 항목으로 올리는 단계에서 멈추고 승인을 묻는다
    ④ 만든 파일을 열고, 체크 항목을 진행한다

지키는 것
  · 승인을 묻는 것은 화면이지만 **승인 없이 돌지 않게 막는 것은 실행기**(actions/runner.py)다.
    이 창이 승인 단계를 빠뜨려도 변경 단계는 돌지 않는다.
  · 올해 공식 기준이 확인되지 않았으면 그렇게 크게 보인다. 초안을 공식 서식처럼 보이게 하지 않는다.
  · 발송·결재는 이 창에 없다. 만든 파일을 사람이 쓴다.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QVBoxLayout, QWidget,
)

from ...actions import catalog
from ...actions.registry import REGISTRY
from ...actions.runner import (COMPLETED, FAILED, PARTIAL, RUN_LABEL, STEP_LABEL, WAITING, CANCELLED,
                               Runner)
from ...core import cases
from ...jobs.action_task import ActionRunner
from .. import theme
from ..widgets import Card, clear_layout, muted_label, open_original, section_title

_MARK = {"done": "✓", "failed": "✕", "blocked": "!", "skipped": "–", "running": "…",
         "waiting_for_approval": "?", "pending": "○"}
SOURCE_LABEL = {"official": "공식", "stated": "문서에 적힘", "precedent": "과거 사례",
                "suggested": "추천", "user": "직접 추가"}
MODE_LABEL = {"preliminary": "초안 · 사전조사용", "official": "공식 기준 반영"}
CONTINUE_STATES = (PARTIAL, FAILED, WAITING)


class WorkboxDialog(QDialog):
    def __init__(self, parent: QWidget | None, db, task_id: int, today: date | None = None,
                 runner_factory=Runner, run_id: int | None = None, year: int | None = None) -> None:
        super().__init__(parent)
        self.db, self.task_id = db, task_id
        self.today = today or date.today()
        self._factory = runner_factory
        self._run_id: int | None = None
        self._busy = False
        self.setWindowTitle("준비 작업")
        self.setMinimumSize(640, 620)
        self._job = ActionRunner(db.path, self, runner_factory=runner_factory)
        self._job.progress.connect(self._on_progress)
        self._job.done.connect(self._on_done)

        outer = QVBoxLayout(self)
        outer.setSpacing(theme.SP_MD)
        task = db.task(task_id)
        outer.addWidget(section_title(f"{task['name']} — 올해 준비 작업"))
        outer.addWidget(muted_label(
            "과거 자료를 찾아 해별로 비교하고, 부서별 실적 취합 서식과 체크리스트 초안을 만듭니다. "
            "만든 파일은 프로젝트의 출력 폴더에만 저장되고 원본 자료는 바뀌지 않습니다. "
            "공문 발송이나 결재 상신은 하지 않습니다.", small=True))

        start = QHBoxLayout()
        start.addWidget(QLabel("대상 연도"))
        self.year = QSpinBox()
        self.year.setRange(2000, 2100)
        self.year.setValue(year or self.today.year)
        self.year.setAccessibleName("대상 연도")
        start.addWidget(self.year)
        self.run_button = QPushButton("준비 작업 실행")
        self.run_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.run_button.clicked.connect(lambda _=False: self.start())
        start.addWidget(self.run_button)
        self.cancel_button = QPushButton("멈추기")
        self.cancel_button.setObjectName("Quiet")
        self.cancel_button.clicked.connect(lambda _=False: self._job.cancel())
        self.cancel_button.setVisible(False)
        start.addWidget(self.cancel_button)
        start.addStretch(1)
        outer.addLayout(start)
        self.status = muted_label("", small=True)
        outer.addWidget(self.status)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        holder = QWidget()
        self.column = QVBoxLayout(holder)
        self.column.setSpacing(theme.SP_MD)
        self.scroll.setWidget(holder)
        outer.addWidget(self.scroll, 1)

        close = QPushButton("닫기")
        close.setObjectName("Quiet")
        close.clicked.connect(self.accept)
        outer.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        self.refresh()
        if run_id is not None:          # 제안을 받아들여 열렸다 — 만들어 둔 실행을 곧바로 돌린다
            self._run_id = run_id
            QTimer.singleShot(0, lambda: self._run(run_id))

    # ── 실행 ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._busy:
            return
        _, run_id = catalog.prepare(self.db, self.task_id, self.year.value(), today=self.today)
        self._run_id = run_id
        self._run(run_id)

    def _run(self, run_id: int) -> None:
        self._busy = True
        self.run_button.setEnabled(False)
        self.cancel_button.setVisible(True)
        self.status.setText("실행하는 중…")
        if not self._job.start(run_id):
            self._finish_ui()
            return
        self.refresh()

    def _on_progress(self, run_id: int, ordinal: int, total: int, label: str) -> None:
        self.status.setText(f"{ordinal + 1} / {total}  {label}…")

    def _on_done(self, run_id: int, state: str, message: str) -> None:
        self._finish_ui()
        self.status.setText(message or RUN_LABEL.get(state, state))
        self.refresh()

    def _finish_ui(self) -> None:
        self._busy = False
        self.run_button.setEnabled(True)
        self.cancel_button.setVisible(False)

    def _decide(self, step_id: int, approved: bool) -> None:
        Runner(self.db, today=self.today).decide(step_id, approved)
        self._run(self._current_run()["id"])

    def _current_run(self):
        runs = self.db.runs(task_id=self.task_id)
        if self._run_id is not None:
            chosen = self.db.run(self._run_id)
            if chosen is not None:
                return chosen
        return runs[0] if runs else None

    # ── 그리기 ──────────────────────────────────────────────────────
    def refresh(self) -> None:
        clear_layout(self.column)
        run = self._current_run()
        if run is None:
            self.column.addWidget(muted_label(
                "아직 실행한 준비 작업이 없습니다. 연도를 확인하고 ‘준비 작업 실행’을 누르세요."))
            self.column.addStretch(1)
            return
        self._run_id = run["id"]
        case = self.db.case(run["case_id"]) if run["case_id"] else None
        self.column.addWidget(self._case_card(run, case))
        pending = Runner(self.db, today=self.today).pending_approval(run["id"])
        if pending is not None and not self._busy:
            self.column.addWidget(self._approval_card(pending))
        self.column.addWidget(self._steps_card(run))
        artifacts = self.db.artifacts(run_id=run["id"])
        if artifacts:
            self.column.addWidget(self._artifacts_card(artifacts))
        if case is not None and self.db.checklist(case["id"]):
            self.column.addWidget(self._checklist_card(case["id"]))
        if run["state"] in CONTINUE_STATES and not self._busy and pending is None:
            again = QPushButton("이어서 하기")
            again.clicked.connect(lambda _=False: self._run(run["id"]))
            self.column.addWidget(again, 0, Qt.AlignmentFlag.AlignLeft)
        self.column.addStretch(1)

    def _case_card(self, run, case) -> QWidget:
        card = Card(tone="attention" if case is not None and case["basis_state"] == "unconfirmed" else "")
        title = cases.title(case) if case is not None else "수행 건 없음"
        card.body.addWidget(section_title(title))
        card.body.addWidget(muted_label(f"실행 상태: {RUN_LABEL.get(run['state'], run['state'])}"))
        if case is not None:
            card.body.addWidget(muted_label(
                f"올해 기준: {cases.BASIS_LABEL[case['basis_state']]} · {cases.due_text(case)}"))
        modes = {row["mode"] for row in self.db.artifacts(run_id=run["id"])}
        if "preliminary" in modes:
            warn = QLabel("올해 공식 평가기준이 확인되지 않았습니다. 지금 만든 서식과 체크리스트는 "
                          "과거 자료를 참고한 사전조사용 초안입니다.")
            warn.setWordWrap(True)
            card.body.addWidget(warn)
        return card

    def _steps_card(self, run) -> QWidget:
        card = Card()
        card.body.addWidget(section_title("진행"))
        for step in self.db.run_steps(run["id"]):
            tool = REGISTRY.get(step["tool"]) if step["tool"] in REGISTRY else None
            label = (tool.label if tool and tool.label else step["tool"])
            line = f"{_MARK.get(step['state'], '○')}  {label} — {STEP_LABEL.get(step['state'], step['state'])}"
            card.body.addWidget(QLabel(line))
            if step["error"] and step["state"] in ("failed", "blocked", "skipped"):
                card.body.addWidget(muted_label("    " + step["error"], small=True))
        return card

    def _approval_card(self, pending: dict) -> QWidget:
        card = Card(tone="primary")
        card.body.addWidget(section_title("승인이 필요합니다"))
        count = pending["scope"].get("items", 0) if isinstance(pending["scope"], dict) else 0
        card.body.addWidget(muted_label(
            f"‘{pending['label']}’ — 체크리스트 초안의 항목 {count}개를 이 수행 건에 올립니다. "
            "이미 직접 고치거나 진행한 항목은 덮어쓰지 않습니다. 승인하지 않아도 만든 파일은 그대로 남습니다."))
        buttons = QHBoxLayout()
        approve = QPushButton("승인하고 올리기")
        approve.clicked.connect(lambda _=False: self._decide(pending["step_id"], True))
        reject = QPushButton("올리지 않기")
        reject.setObjectName("Quiet")
        reject.clicked.connect(lambda _=False: self._decide(pending["step_id"], False))
        buttons.addWidget(approve)
        buttons.addWidget(reject)
        buttons.addStretch(1)
        card.body.addLayout(buttons)
        return card

    def _artifacts_card(self, artifacts) -> QWidget:
        card = Card()
        card.body.addWidget(section_title("만든 파일"))
        for row in artifacts:
            line = QHBoxLayout()
            text = QLabel(f"{row['title']}  ·  {row['format'].upper()}  ·  {MODE_LABEL.get(row['mode'], row['mode'])}")
            text.setWordWrap(True)
            line.addWidget(text, 1)
            open_button = QPushButton("열기")
            open_button.setObjectName("Link")
            open_button.setCursor(Qt.CursorShape.PointingHandCursor)
            open_button.clicked.connect(lambda _=False, path=row["path"]: open_original(self, self.db, path))
            line.addWidget(open_button)
            card.body.addLayout(line)
        card.body.addWidget(muted_label("파일은 프로젝트의 출력 폴더에 있습니다. 고쳐 쓰려면 복사해서 쓰세요.", small=True))
        return card

    def _checklist_card(self, case_id: int) -> QWidget:
        card = Card()
        card.body.addWidget(section_title("체크리스트"))
        phase = None
        for row in self.db.checklist(case_id):
            if row["phase"] != phase:
                phase = row["phase"]
                card.body.addWidget(muted_label(phase, small=True))
            box = QCheckBox(f"{row['title']}   [{SOURCE_LABEL.get(row['source_type'], row['source_type'])}]")
            box.setChecked(row["state"] == "done")
            box.setToolTip(row["description"])
            box.toggled.connect(lambda on, item=row["id"]: self._check(item, on))
            card.body.addWidget(box)
        return card

    def _check(self, item_id: int, on: bool) -> None:
        self.db.set_check_state(item_id, "done" if on else "todo")

    def done(self, result: int) -> None:
        self._job.stop()
        super().done(result)
