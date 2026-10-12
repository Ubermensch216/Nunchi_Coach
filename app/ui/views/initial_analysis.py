"""최초 폴더 분석의 진행·중단·오류·결과 안내 화면."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QProgressBar, QPushButton, QVBoxLayout, QWidget

from ...core import initial_analysis
from .. import stages
from ..widgets import Card, Page, muted_label, view_title


class InitialAnalysisView(QWidget):
    resume = Signal()
    pause = Signal()
    open_results = Signal()
    show_status = Signal()
    change_sources = Signal()

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._running = self._failed = False
        self._stopping = False
        self.setObjectName("Canvas")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer = QVBoxLayout(self)
        self.page = Page(max_width=760)
        outer.addWidget(self.page)
        body = self.page.column
        self.title = view_title("")
        body.addWidget(self.title)
        self.message = muted_label("")
        body.addWidget(self.message)
        card = Card()
        self.phase = muted_label("자료를 읽기 → 업무 나누기 → 일정과 처리 방법 정리 → 업무 화면 준비")
        card.body.addWidget(self.phase)
        self.progress = QProgressBar()
        self.progress.setObjectName("Thinking")
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        card.body.addWidget(self.progress)
        self.summary = muted_label("")
        card.body.addWidget(self.summary)
        controls = QHBoxLayout()
        self.action = QPushButton()
        self.action.setObjectName("Primary")
        self.action.clicked.connect(self._act)
        controls.addWidget(self.action)
        self.status = QPushButton("분석 상태·실패 자료 확인")
        self.status.clicked.connect(self.show_status.emit)
        controls.addWidget(self.status)
        self.sources = QPushButton("자료 폴더 확인")
        self.sources.clicked.connect(self.change_sources.emit)
        controls.addWidget(self.sources)
        card.body.addLayout(controls)
        body.addWidget(card)
        body.addWidget(muted_label("원본은 읽기만 합니다. 분석 중에도 노트북을 사용할 수 있습니다."))
        body.addStretch(1)
        self.refresh()

    def set_run(self, running, failed=False):
        self._running, self._failed = running, failed
        self._stopping = False
        if running:
            self.phase.setText("자료에서 업무를 파악할 준비를 하고 있습니다")
            self.progress.setRange(0, 0)
        self.refresh()

    def show_progress(self, stage, done, total):
        self.phase.setText(stages.running(stage))
        self.progress.setRange(0, total if total > 0 else 0)
        if total > 0:
            self.progress.setValue(min(done, total))
        self.summary.setText(f"현재 단계 {done:,} / {total:,}건" if total > 0 else f"현재 단계 {done:,}건 처리")

    def refresh(self):
        self.result = initial_analysis.outcome(self.db, running=self._running, failed=self._failed)
        self.title.setText(self.result.title)
        self.message.setText(self.result.message)
        if self._running:
            action = "중단하는 중…" if self._stopping else "일시중지"
        elif self.result.can_open:
            action = "업무 분석 결과 보기"
        elif self.result.state == "paused":
            action = "이어서 분석하기"
        else:
            action = "업무 분석 시작" if self.result.state == "waiting" else "다시 분석하기"
        self.action.setText(action)
        self.action.setEnabled(not self._stopping)
        self.sources.setEnabled(not self._running)
        self.progress.setVisible(self._running)
        if not self._running:
            counts = self.db.counts()
            self.summary.setText(f"등록 파일 {counts['total']:,}건 · 읽은 문서 {counts['parsed']:,}건 · 파악한 업무 {counts['tasks']:,}개")
            self.phase.setText("분석이 끝났습니다" if self.result.can_open else "분석 상태를 확인하고 이어서 진행하세요")

    def _act(self):
        if self._running:
            self._stopping = True
            self.action.setText("중단하는 중…")
            self.action.setEnabled(False)
            self.pause.emit()
        elif self.result.can_open:
            self.open_results.emit()
        else:
            self.resume.emit()
