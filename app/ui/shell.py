"""애플리케이션 셸 — 업무·챙길 일·업무노트·인수인계 메뉴와 분석 상태.

일정·자료는 선택 업무 안에서 보며, 챙길 일은 진행·제안 확인,
인수인계는 매뉴얼 작성과 기록 보관·이동을 맡는다.
미분류 자료 점검은 보조 문서 화면에서 연다.

묻는 일은 업무노트가 한다. 예전의 '질문' 메뉴는 자료 전체에 한 번 묻고 끝나는
화면이었다 — 지금은 눈치코치가 파악한 업무와 그 문서를 업무노트의 자료로 넣고
(core/notebook_tasks.py) 거기서 묻는다. 묻는 자리가 둘이면 어디서 물어야 할지부터
물어야 한다.

분석 진행률은 메뉴가 아니라 상단 바에 상주한다. 상태는 화면이 아니다.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import handover
from ..core import initial_analysis
from ..db import Database
from ..jobs import PipelineRunner
from ..jobs.pipeline import STAGE_DISCOVER
from ..jobs.knowledge_task import KnowledgeRunner
from . import icons, motion, stages, theme
from .views.calendar import CalendarView
from .views.documents import DocumentsView
from .views.onboarding import OnboardingView
from .views.initial_analysis import InitialAnalysisView
from .views.tasks import TasksView
from .views.today import TodayView
from .views.notebooks import NotebooksView
from .views.handover import HandoverView
from .widgets.nav_button import NavButton

# 메뉴마다 그 메뉴가 답하는 질문을 함께 적는다. 이름만 있는 메뉴는
# 처음 쓰는 사람에게 "눌러 봐야 아는 것"이지만, 질문이 붙으면 누르기 전에
# 안다 — 인수인계 도구에서 첫 5분이 그 차이로 갈린다.
NAV = [
    ("tasks", "tasks", "업무", "내 업무는 무엇인가"),
    ("today", "tasks", "챙길 일", "새 제안·진행 중인 작업·확인할 단계를 살핀다"),
    ("notebooks", "ask", "업무노트", "업무노트를 골라 업무와 자료를 분석하고, 정리해 남긴다"),
    ("handover", "tasks", "인수인계", "후임자용 매뉴얼 작성과 업무 기록 보관·이동"),
]
NAV_SHORTCUTS = {'tasks': 1, 'notebooks': 2, 'today': 3, 'handover': 4}

# 가로 띠에서는 메뉴가 글자만으로 선다. 아이콘 체계(icons.py)는 설정 창처럼
# 세로 목록이 있는 곳에서 쓴다.


class AnalysisStatus(QPushButton):
    """단계 문구와 그 단계의 진행률을 같은 클릭 영역에 담는다."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__("자료원이 없습니다", parent)
        self.setObjectName("AnalysisStatus")
        self.setFixedHeight(40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("눌러서 단계별 진행과 실패 목록을 봅니다")
        self.progress = QProgressBar(self)
        self.progress.setObjectName("TopProgress")
        self.progress.setTextVisible(False)
        self.progress.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.progress.hide()
        self.animation = QPropertyAnimation(self.progress, b"value", self)
        self.animation.setDuration(320)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._stage = None
        self._target = None

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.progress.setGeometry(10, self.height() - 9, max(0, self.width() - 20), 4)

    def _active(self, active: bool) -> None:
        if self.property("active") != active:
            self.setProperty("active", active)
            self.style().unpolish(self)
            self.style().polish(self)
            self.updateGeometry()

    def show_progress(self, done: int, total: int, note: str = "") -> None:
        stage = note or "분석 중"
        self._active(True)
        self.progress.show()
        if total <= 0:
            self.animation.stop()
            self.progress.setRange(0, 0)
            self._target = None
            self.setText(f"{stage} · {done:,}건" if done else stage)
        else:
            target = round(max(0, min(done, total)) / total * 1000)
            reset = self._stage != stage or self.progress.maximum() == 0
            if reset or target != self._target:
                self.animation.stop()
                self.progress.setRange(0, 1000)
                if reset or target < self.progress.value() or motion.is_reduced():
                    self.progress.setValue(target)
                else:
                    self.animation.setStartValue(max(0, self.progress.value()))
                    self.animation.setEndValue(target)
                    self.animation.start()
            self._target = target
            self.setText(f"{stage} · {done:,} / {total:,}")
        self._stage = stage
        self.setAccessibleName(self.text())

    def show_idle(self, text: str) -> None:
        self.animation.stop()
        self.progress.hide()
        self._active(False)
        self._stage = self._target = None
        self.setText(text)
        self.setAccessibleName(text)


class TopBar(QWidget):
    """흰 띠 하나에 길과 상태를 담는다.

    왼쪽부터 제품·프로젝트 → 업무·업무노트 메뉴 → 오른쪽에 지금 상태. 세로 사이드바 대신
    가로 띠를 쓰는 이유는 본문이다 — 업무노트가 '출처 | 대화 | 스튜디오' 세 판을
    쓰는데 왼쪽에 메뉴 기둥까지 서면 원문을 대조하기 어려운 폭이 된다.
    메뉴는 하루에 몇 번 누르고 본문은 내내 읽는다.

    띠는 조용해야 한다. 오른쪽 상태 문구들은 모두 회색 작은 글자로 물러서
    있고, 눈에 띄는 것은 고른 메뉴 하나뿐이다.
    """

    settings_requested = Signal()
    status_clicked = Signal()
    project_requested = Signal()
    navigated = Signal(str)
    handover_clicked = Signal()
    home_requested = Signal()

    def __init__(self, project_name: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("TopBar")
        self.setFixedHeight(theme.TOPBAR_H)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        row = QHBoxLayout(self)
        row.setContentsMargins(theme.SP_XL, 0, theme.SP_LG, 0)
        row.setSpacing(theme.SP_SM)

        # 창 아이콘과 같은 연결 고리 표지를 쓴다.
        art = icons.brand(theme.NAVBAR_TEXT, theme.TOPBAR_H - 24)
        if art is not None:
            self.brand_mark = QLabel()
            self.brand_mark.setPixmap(art)
            self.brand_mark.setCursor(Qt.CursorShape.PointingHandCursor)
            self.brand_mark.setToolTip("업무 홈으로 이동")
            self.brand_mark.mousePressEvent = lambda _event: self.home_requested.emit()
            row.addWidget(self.brand_mark)
        else:
            self.brand_mark = None

        self.brand_title = QPushButton("눈치코치")
        self.brand_title.setObjectName("AppName")
        self.brand_title.setCursor(Qt.CursorShape.PointingHandCursor)
        self.brand_title.setToolTip("업무 홈으로 이동")
        self.brand_title.clicked.connect(self.home_requested.emit)
        row.addWidget(self.brand_title)
        row.addSpacing(theme.SP_MD)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        for key, _icon, label, question in NAV:
            button = NavButton(label)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            # 메뉴가 답하는 질문은 툴팁에 둔다 — 처음 쓰는 사람에게 이 넷이
            # 각각 무엇에 답하는지가 이 제품의 뼈대다.
            button.setToolTip(f"{label} — {question}  (Ctrl+{NAV_SHORTCUTS[key]})")
            button.clicked.connect(lambda _=False, k=key: self.navigated.emit(k))
            self.group.addButton(button)
            row.addWidget(button)
            self._buttons[key] = button

        row.addStretch(1)

        # 인수인계 진행도. 어느 화면에 있든 "얼마나 남았나"가 보여야 확인
        # 작업이 끝이 있는 일로 느껴진다.
        self.handover = QPushButton("")
        self.handover.setObjectName("TopHandover")
        self.handover.setCursor(Qt.CursorShape.PointingHandCursor)
        self.handover.clicked.connect(self.handover_clicked.emit)
        self.handover.hide()
        row.addWidget(self.handover)

        row.addWidget(_bar_divider())

        # 어떤 인수인계를 보고 있는지 항상 적어 둔다. 프로젝트가 여럿이 되면
        # 화면만 봐서는 구별되지 않는다. 누르면 다른 인수인계로 갈아탄다.
        self.project = QPushButton(f"{project_name or '프로젝트'}  ▾")
        self.project.setObjectName("TopLink")
        self.project.setCursor(Qt.CursorShape.PointingHandCursor)
        self.project.setToolTip("지금 열려 있는 인수인계 — 눌러서 다른 인수인계로 바꿉니다")
        self.project.clicked.connect(self.project_requested.emit)
        row.addWidget(self.project)

        # 이 도구가 지키는 약속 — 원본을 건드리지 않는다는 것이 이 제품
        # 채택의 조건이다. 늘 보이되 소리치지 않는다.
        self.promise = QLabel("읽기 전용")
        self.promise.setObjectName("TopPromise")
        self.promise.setToolTip("원본을 수정·이동·삭제·이름 변경하지 않습니다")
        row.addWidget(self.promise)

        self.status = AnalysisStatus()
        self.progress = self.status.progress
        self.status.clicked.connect(self.status_clicked.emit)
        row.addWidget(self.status)

        settings = QPushButton()
        settings.setObjectName("TopLink")
        settings.setIcon(icons.icon("settings", theme.NAVBAR_MUTED, 18))
        settings.setIconSize(QSize(18, 18))
        settings.setFixedWidth(34)
        settings.setCursor(Qt.CursorShape.PointingHandCursor)
        settings.setToolTip("설정")
        settings.setAccessibleName("설정")
        settings.clicked.connect(self.settings_requested.emit)
        row.addWidget(settings)

    # ── 길 ──────────────────────────────────────────────────────────
    def select(self, key: str) -> None:
        button = self._buttons.get(key)
        if button:
            button.setChecked(True)

    def set_count(self, key: str, count: int, detail: str = '') -> None:
        """메뉴 제목을 유지하고 건수는 독립 배지로 표시한다."""
        button = self._buttons.get(key)
        if button is not None:
            button.set_count(count)
            label, question = next((label, question) for k, _icon, label, question in NAV if k == key)
            button.setToolTip(f'{label} — {question}  (Ctrl+{NAV_SHORTCUTS[key]})' + ('\n' + detail if detail else ''))
            button.setAccessibleDescription(detail)

    def setEnabledNav(self, enabled: bool) -> None:
        # 업무노트는 직접 업로드, 인수인계는 저장본 가져오기로 자료 폴더 없이도 시작할 수 있다.
        for key, button in self._buttons.items():
            button.setEnabled(enabled or key in ("notebooks", "handover"))

    # ── 상태 ────────────────────────────────────────────────────────
    def show_progress(self, done: int, total: int, note: str = "") -> None:
        self.status.show_progress(done, total, note)

    def show_idle(self, text: str) -> None:
        self.status.show_idle(text)

    def show_promise(self, law_on: bool) -> None:
        """원본을 건드리지 않는다는 약속 옆에, PC 밖과 통신하는 기능이 켜져 있는지 적는다."""
        if law_on:
            self.promise.setText("읽기 전용 · 법령 조회 켜짐")
            self.promise.setToolTip(
                "원본을 수정·이동·삭제·이름 변경하지 않습니다.\n"
                "현행 법령 조회가 켜져 있습니다 — 업무노트 대화에서 ‘법령’을 고른 질문에 한해 "
                "law.go.kr로 법령 이름·조문 번호·그 질문 문장을 보냅니다. 자료 본문은 보내지 않습니다.")
        else:
            self.promise.setText("읽기 전용")
            self.promise.setToolTip("원본을 수정·이동·삭제·이름 변경하지 않습니다")

    def show_handover(self, progress) -> None:
        """진행도를 갱신한다. 잴 것이 없으면 아예 숨긴다 —
        분석이 끝나기 전의 0%는 '아무것도 안 했다'는 잘못된 질책이다."""
        if not progress.measured:
            self.handover.hide()
            return
        self.handover.show()
        self.handover.setText(f"인수인계 확인 {progress.percent}%")
        lines = [area.sentence() for area in progress.measured]
        nxt = progress.next_step()
        if nxt is not None:
            lines.append(f"다음에 확인할 것 · {nxt.sentence()}")
        self.handover.setToolTip(chr(10).join(lines))


def _bar_divider() -> QFrame:
    line = QFrame()
    line.setObjectName("TopDivider")
    line.setFixedWidth(1)
    line.setFixedHeight(16)
    return line


class MainWindow(QMainWindow):
    # 다른 인수인계를 열어 달라 — 창을 새로 만드는 일은 main.py가 한다.
    switch_requested = Signal()

    def __init__(self, db: Database, project_name: str = ""):
        super().__init__()
        self.db = db
        self.project_name = project_name or db.get_meta("project_name") or ""
        title = "눈치코치 — 업무 인수인계 도구"
        self.setWindowTitle(f"{self.project_name} — {title}" if self.project_name else title)
        self.setMinimumSize(*theme.WINDOW_MIN)

        root = QWidget()
        root.setObjectName("Content")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.topbar = TopBar(self.project_name)
        self.topbar.home_requested.connect(self._go_home)
        self.topbar.settings_requested.connect(self._open_settings)
        self.topbar.status_clicked.connect(self._open_status)
        self.topbar.project_requested.connect(self.switch_requested.emit)
        self.topbar.navigated.connect(self.go)
        # 진행도를 누르면 확인할 것이 있는 자리로 데려간다. 숫자만 보여 주고
        # 어디로 가야 하는지 말하지 않으면 그 숫자는 채근일 뿐이다.
        self.topbar.handover_clicked.connect(lambda: self.go("handover"))
        outer.addWidget(self.topbar)
        self.stage_reports: dict[str, object] = {}
        self._pipeline_error = False
        self._status_dialog = None

        self.stack = QStackedWidget()
        self.views: dict[str, QWidget] = {
            "onboarding": OnboardingView(self.db),
            "initial_analysis": InitialAnalysisView(self.db),
            "today": TodayView(self.db),
            "tasks": TasksView(self.db),
            "handover": HandoverView(self.db, self.project_name),
            "notebooks": NotebooksView(self.db),
            "calendar": CalendarView(self.db),
            "documents": DocumentsView(self.db),
        }
        for view in self.views.values():
            self.stack.addWidget(view)
        outer.addWidget(self.stack, 1)

        self.setCentralWidget(root)

        self.views["onboarding"].started.connect(self._on_scan_started)
        for key in ("tasks", "calendar"):
            view = self.views[key]
            if hasattr(view, "go_documents"):
                view.go_documents.connect(self._open_task_documents if key == "tasks" else lambda: self.go("documents"))

        # When(일정)과 What(업무)은 서로 되돌아간다 — 일정에서 업무를 열고,
        # 업무 상세에서 일정으로 넘어간다.
        self.views["tasks"].go_calendar.connect(self.views["tasks"]._show_schedule)
        self.views["tasks"].open_notebook.connect(self._open_notebook_for_task)
        self.views["notebooks"].go_task.connect(self._open_task_from_calendar)
        self.views["today"].workbox_opener = self.views["tasks"]._open_workbox
        self.views["today"].open_task.connect(self._open_task_from_calendar)
        self.views["today"].changed.connect(self._sync_today)
        self.views['today'].count_changed.connect(self._update_attention_count)
        self.views["today"].go_tasks.connect(lambda: self.go("tasks"))
        self.views['handover'].changed.connect(self._sync_handover)
        self.views['handover'].changed.connect(self._sync_today)
        # 업무 화면에서 확인·교정이 일어나면 상단 띠의 진행도가 그 자리에서 오른다.
        self.views["tasks"].state_changed.connect(self._sync_handover)
        self.views["calendar"].open_task.connect(self._open_task_from_calendar)
        self.views["calendar"].go_tasks.connect(lambda: self.go("tasks"))

        # 처리는 백그라운드에서 돈다. UI는 완료를 기다리지 않고 이미 처리된
        # 결과부터 보여준다.
        self.runner = PipelineRunner(self.db.path, self)
        self.runner.progress.connect(self._on_progress)
        self.runner.stage_done.connect(self._on_stage_done)
        self.runner.finished.connect(self._on_pipeline_finished)
        self.runner.failed.connect(self._on_pipeline_failed)
        initial = self.views["initial_analysis"]
        initial.resume.connect(self._start_pipeline)
        initial.pause.connect(lambda: self.runner.stop(wait_ms=0))
        initial.open_results.connect(self._open_initial_results)
        initial.show_status.connect(self._open_status)
        initial.change_sources.connect(self._open_settings)
        initial.review_structure.connect(self._open_structure_review)
        self.views["tasks"].review_structure.connect(self._open_structure_review)
        self._knowledge = KnowledgeRunner(self.db.path, self)
        self._knowledge_tick = QTimer(self)
        self._knowledge_tick.setInterval(2000)
        self._knowledge_tick.timeout.connect(self._resume_knowledge)
        self._knowledge_tick.timeout.connect(self._ask_about_arrivals)
        self._asking_arrival = False
        self._knowledge_tick.start()

        self._tick = QTimer(self)
        self._tick.setInterval(1500)
        self._tick.timeout.connect(self._refresh_current)

        # 날짜가 바뀌면 지금 챙길 일도 달라진다. 켜 둔 채로 자정을 넘겨도 놓치지 않게 한 시간마다 본다.
        self._daily = QTimer(self)
        self._daily.setInterval(3_600_000)
        self._daily.timeout.connect(self._scan_if_new_day)

        self._install_shortcuts()
        self._scan_proactive()
        self.refresh()
        self._resume_if_pending()
        self._daily.start()

    # ── 내비게이션 ──────────────────────────────────────────────────
    def _resume_knowledge(self):
        from ..core.knowledge.lifecycle import pending
        if not self.runner.running and not self._knowledge.running and pending(self.db):
            self._knowledge.start()

    def _go_home(self) -> None:
        self.go("tasks")
        tasks_view = self.views.get("tasks")
        if self.stack.currentWidget() is tasks_view:
            tasks_view.back()

    def _open_task_documents(self) -> None:
        tasks = self.views["tasks"]
        if tasks._task_id is not None:
            tasks._switch("docs")
            return
        documents = self.views["documents"]
        documents.unclassified_only.blockSignals(True)
        documents.unclassified_only.setChecked(True)
        documents.unclassified_only.blockSignals(False)
        self.go("documents")

    def go(self, key: str) -> None:
        if key == "tasks" and initial_analysis.pending(self.db):
            key = "initial_analysis"
        view = self.views.get(key)
        if view is None:
            return
        self.stack.setCurrentWidget(view)
        self.topbar.select("tasks" if key in ("initial_analysis", "calendar", "documents") else key)
        if key == "tasks":
            view.open_workspace()
        elif key == "notebooks":
            view.show_home()
            view.refresh()
        elif hasattr(view, "refresh"):
            view.refresh()
        self._sync_handover()

    def _install_shortcuts(self) -> None:
        for key, _icon, _label, _question in NAV:
            shortcut = QShortcut(QKeySequence(f"Ctrl+{NAV_SHORTCUTS[key]}"), self)
            shortcut.activated.connect(lambda k=key: self.go(k))

    # ── 상태 ────────────────────────────────────────────────────────
    def refresh(self) -> None:
        """자료원이 없으면 시작 마법사만 보여준다. 빈 메뉴를 누르게 하지 않는다."""
        from ..law import service as law_service
        self.topbar.show_promise(law_service.available(self.db))
        has_source = bool(self.db.work_sources())
        self.topbar.setEnabledNav(has_source)
        if not has_source:
            if self.stack.currentWidget() not in (self.views['notebooks'], self.views['handover']):
                self.stack.setCurrentWidget(self.views["onboarding"])
            self.topbar.show_idle("자료 폴더를 연결하세요")
            return

        self._sync_handover()
        if not self.runner.running:
            self._show_idle_status()
        if self.stack.currentWidget() is self.views["onboarding"]:
            self.go("tasks")

    def _show_idle_status(self) -> None:
        if initial_analysis.pending(self.db):
            self.topbar.show_idle(initial_analysis.outcome(self.db, failed=self._pipeline_error).title)
            return
        counts = self.db.counts()
        parts = [f"파일 {counts['total']:,}건"]
        if counts["parse_failed"]:
            parts.append(f"읽지 못함 {counts['parse_failed']:,}")
        if counts["tasks"]:
            parts.append(f"업무 {counts['tasks']}개")
        self.topbar.show_idle(" · ".join(parts))

    # ── 백그라운드 처리 ─────────────────────────────────────────────
    def _resume_if_pending(self) -> None:
        """앱을 껐다 켜도 중단 지점부터 이어서 처리한다 (ING-006).

        남은 일은 별도 큐가 아니라 문서 상태로 판단하므로, 강제 종료 뒤에도
        같은 질의가 그대로 집어낸다.
        """
        # 준비 작업을 하다 프로그램이 죽은 흔적은 ‘일부만 완료’로 되돌린다(이어서 할 수 있다).
        self.db.recover_actions()
        if not self.db.work_sources():
            return
        if initial_analysis.pending(self.db) and self.db.get_meta("analysis_run_state") in ("cancelled", "failed", "completed"):
            # 명시적으로 멈춘 최초 분석이나 실패 결과를 앱 재실행으로 지우지 않는다.
            return
        from ..ai.settings import model_names
        from ..core import docrole
        from ..ingest.parsers.base import outdated_clause
        embed_model = model_names(self.db)[1]
        pending = self.db.con.execute(
            "SELECT COUNT(*) AS n FROM documents d "
            "JOIN sources s ON s.id = d.source_id AND s.kind != 'upload' "
            "LEFT JOIN doc_embeddings e ON e.doc_id = d.id AND e.model = ? "
            "WHERE d.missing_since IS NULL AND d.parse_status != 'skipped' "
            "  AND (d.hash IS NULL OR d.parse_status = 'pending' "
            "       OR d.eff_date IS NULL "
            "       OR (d.parse_status IN ('ok','partial') AND "
            "           (e.doc_id IS NULL OR " + outdated_clause("d") + ")))",
            (embed_model,),
        ).fetchone()["n"]
        counts = self.db.counts()
        # 문서 처리는 끝났는데 업무를 아직 못 찾았거나, 업무는 찾았는데
        # 주기를 아직 못 살핀 경우도 이어서 해야 한다.
        needs_discovery = counts["embedded"] > 0 and counts["in_task"] == 0
        needs_cycles = counts["tasks"] > 0 and self.db.get_meta("cycles_checked") is None
        needs_steps = counts["tasks"] > 0 and self.db.get_meta("steps_checked") is None
        needs_items = counts["tasks"] > 0 and self.db.get_meta("items_checked") is None
        # 질문 준비는 플래그가 아니라 실제 상태로 판단한다. 플래그만 보면
        # 한 번 세운 뒤에는 임베딩이 빠진 조각을 영영 다시 시도하지 않는다.
        # 위 pending 질의가 이미 쓰는 방식이고, 이쪽만 달랐다.
        needs_chunks = (
            counts["documents"] > 0
            and (self.db.get_meta("chunks_checked") is None
                 or self.db.unembedded_chunk_count(embed_model) > 0
                 or self.db.unembedded_child_count(embed_model) > 0)
        )
        # 역할을 아직 정하지 않은 문서(스키마를 올린 옛 프로젝트 포함).
        needs_roles = bool(docrole.pending(self.db, limit=1))
        if (initial_analysis.pending(self.db) or pending or needs_discovery or needs_cycles or needs_steps or needs_items
                or needs_chunks or needs_roles):
            self._start_pipeline()

    def _start_pipeline(self) -> None:
        if self.runner.running:
            return
        if initial_analysis.pending(self.db):
            self.db.set_meta("initial_analysis_state", "pending")
        self._pipeline_error = False
        self.stage_reports.clear()
        self.views["initial_analysis"].set_run(True)
        self.runner.start()
        self._tick.start()
        self.topbar.show_progress(0, 1, "시작하는 중")

    def _on_scan_started(self) -> None:
        if not self.db.tasks() and self.db.get_meta("initial_analysis_state") != "ready":
            self.db.set_meta("initial_analysis_state", "pending")
        self.refresh()
        self._start_pipeline()

    def _on_progress(self, stage: str, done: int, total: int, note: str) -> None:
        # 상시 화면에는 일감 이름이 아니라 지금 무엇이 일어나는지를 적는다
        # (계획서 §24). 기술 이름은 상태 상세에만 둔다.
        self.topbar.show_progress(done, total, stages.running(stage))
        self.views["initial_analysis"].show_progress(stage, done, total)

    # ── 선제 감지 ───────────────────────────────────────────────────
    def _scan_proactive(self) -> None:
        """과거 주기와 새 문서에서 지금 챙길 일을 찾는다. 모델을 부르지 않는 짧은 계산이다."""
        from ..core import diagnostics
        from ..proactive import recommend

        try:
            recommend.scan(self.db)
        except Exception as exc:        # 감지가 실패해도 화면은 그대로 쓸 수 있어야 한다
            diagnostics.record("proactive.error", exc)
        self._sync_today()

    def _scan_if_new_day(self) -> None:
        from datetime import date

        if self.db.get_meta("proactive_scanned") != date.today().isoformat():
            self._scan_proactive()

    def _sync_today(self) -> None:
        view = self.views["today"]
        view.refresh()

    def _update_attention_count(self, count) -> None:
        self.topbar.set_count('today', count, self.views['today'].data.attention_summary)

    def _open_structure_review(self) -> None:
        """업무 체계 확인(S4). 고친 업무는 다음 분석이 다시 본다 — 돌고 있으면 끝난 뒤에 한 번 더."""
        from .views.structure_review import StructureReviewDialog

        dialog = StructureReviewDialog(self.db, self)
        dialog.handover_folder_requested.connect(self._make_handover_folder)
        dialog.exec()
        if dialog.changed:
            if self.runner.running:
                self._rerun_after_review = True
            else:
                self._start_pipeline()
        self._refresh_current()

    def _make_handover_folder(self) -> None:
        self.go("handover")
        self.views["handover"]._export_folder()

    def _on_pipeline_finished(self) -> None:
        self._tick.stop()
        if getattr(self, "_rerun_after_review", False):
            self._rerun_after_review = False
            QTimer.singleShot(0, self._start_pipeline)
        self._scan_proactive()
        initial = self.views["initial_analysis"]
        initial.set_run(False, self._pipeline_error)
        if initial_analysis.pending(self.db):
            # 완료를 기다리며 보고 있던 화면만 전환한다. 업무노트의 선택·초점은 유지한다.
            if initial.result.state == "ready" and self.stack.currentWidget() is initial:
                self._open_initial_results()
            self.topbar.show_idle(initial.result.title)
        elif not self._pipeline_error:
            self._show_idle_status()
        self._refresh_current()

    def _on_pipeline_failed(self, message: str) -> None:
        self._pipeline_error = True
        self._tick.stop()
        self.topbar.show_idle(f"⚠ 처리 중 오류: {message}")
        self.views["initial_analysis"].set_run(False, True)

    def _open_initial_results(self) -> None:
        result = initial_analysis.outcome(self.db, running=self.runner.running, failed=self._pipeline_error)
        if not result.can_open:
            return
        self.db.set_meta("initial_analysis_state", "ready")
        self._go_home()

    def _refresh_current(self) -> None:
        view = self.stack.currentWidget()
        if hasattr(view, "refresh"):
            view.refresh()

    def _sync_handover(self) -> None:
        """상단 띠의 진행도를 지금 상태로 맞춘다.

        업무를 확인하거나 문서를 읽음 표시하면 그 자리에서 올라가야 한다.
        화면을 옮겨야만 갱신되면, 사용자는 자기가 한 일이 반영됐는지 확인하러
        메뉴를 한 번씩 눌러 보게 된다.
        """
        self.topbar.show_handover(handover.summarize(self.db.handover_counts()))

    def _open_status(self) -> None:
        from .status_dialog import StatusDialog

        dialog = StatusDialog(self, self)
        self._status_dialog = dialog
        # 열려 있는 동안 진행 상황이 계속 갱신되도록 tick에 물린다.
        self._tick.timeout.connect(dialog.refresh)
        try:
            dialog.exec()
        finally:
            self._tick.timeout.disconnect(dialog.refresh)
            self._status_dialog = None

    def _on_stage_done(self, report) -> None:
        self.stage_reports[report.stage] = report
        if report.stage == STAGE_DISCOVER:
            # 업무를 찾았으면 분석이 끝나기 전에도 업무 체계를 확인할 수 있다(S4).
            self.views["initial_analysis"].refresh()

    def _open_task_from_calendar(self, task_id: int) -> None:
        self.go("tasks")
        self.views["tasks"].open_task(task_id)

    def _open_notebook_for_task(self, task_id: int) -> None:
        """업무 상세에서 '업무노트에서 묻기'를 눌렀을 때 해당 업무의 업무노트를 열거나 생성 (개편계획서 §7)."""
        from PySide6.QtWidgets import QMessageBox

        from ..core import notebook_tasks
        try:
            opened = notebook_tasks.open_for_task(self.db, task_id)
        except ValueError:   # 그사이 업무가 없어졌거나 합쳐졌다
            return
        if opened.warnings:
            QMessageBox.warning(self, "업무노트에 일부만 넣었습니다", chr(10).join(opened.warnings))
        self.go("notebooks")
        self.views["notebooks"].open_notebook(opened.notebook_id)

    def _open_settings(self) -> None:
        from .views.settings import SettingsDialog

        dialog = SettingsDialog(self.db, self)
        dialog.sources_changed.connect(self._on_sources_changed)
        dialog.models_changed.connect(self._on_sources_changed)
        dialog.exec()
        # 법령 조회를 켜고 껐을 수 있다. 상단 띠의 표시와 업무노트 입력칸을 다시 맞춘다.
        from ..law import service as law_service
        self.topbar.show_promise(law_service.available(self.db))
        self._refresh_current()

    def _ask_about_arrivals(self) -> None:
        """onNara.sAIde가 알려 온 꾸러미를 자료 폴더로 등록할지 묻는다.

        알림은 브리지의 서버 스레드에 도착한다. 화면은 여기(주 스레드)에서 읽어 와 묻는다.
        알림만으로는 아무것도 등록하지 않는다 — 사용자가 고른 뒤에만 가져온다.
        """
        from ..bridge import service as bridge_service

        bridge = bridge_service.current()
        if bridge is None or self._asking_arrival or not self.isVisible():
            return
        self._asking_arrival = True
        try:
            for arrival in bridge.take_arrivals():
                self.import_arrival(arrival, self._confirm_arrival(arrival))
        finally:
            self._asking_arrival = False

    def _confirm_arrival(self, arrival: dict) -> bool:
        from PySide6.QtWidgets import QMessageBox

        from ..core import onnara_import

        try:
            summary = onnara_import.describe_package(arrival["path"])
        except ValueError as exc:
            summary = str(exc)
        answer = QMessageBox.question(
            self, "onNara.sAIde 꾸러미",
            f"onNara.sAIde에서 꾸러미를 내보냈습니다.\n\n{summary}\n{arrival['path']}\n\n"
            f"이 인수인계({self.project_name})의 자료 폴더로 등록할까요? 등록하면 다른 자료와 함께 분석합니다. "
            "지금 등록하지 않아도 설정 › 자료 폴더 › 온나라 자료 가져오기에서 이 폴더를 고를 수 있습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def import_arrival(self, arrival: dict, accepted: bool) -> None:
        from PySide6.QtWidgets import QMessageBox

        from ..bridge import service as bridge_service
        from ..core import onnara_import

        bridge = bridge_service.current()
        if not accepted:
            if bridge is not None:
                bridge.settle_arrival(arrival["run_id"], "dismissed")
            return
        try:
            onnara_import.add_to_project(self.db, arrival["path"])
        except (ValueError, OSError) as exc:
            if bridge is not None:
                bridge.settle_arrival(arrival["run_id"], "failed")
            QMessageBox.warning(self, "onNara.sAIde 꾸러미", str(exc))
            return
        if bridge is not None:
            bridge.settle_arrival(arrival["run_id"], "imported")
        self.db.audit("bridge.package.import", arrival["run_id"])
        self._on_sources_changed()

    def _on_sources_changed(self) -> None:
        """자료 폴더를 더하거나 바꾸거나 뺐다 — 화면과 분석을 다시 맞춘다.

        폴더만 바꿔 놓고 분석을 그대로 두면, 사용자는 새 폴더를 지정했는데도
        옛 목록을 보며 "안 먹혔나" 하게 된다. 파이프라인은 남은 일을 문서
        상태로 찾으므로 그냥 다시 켜면 새 폴더부터 훑는다(ING-006).
        """
        self.refresh()
        if self.db.work_sources():
            self._start_pipeline()

    def restyle(self) -> None:
        """테마·글자 크기가 바뀐 뒤 창 전체를 새 색으로 다시 그린다.

        스타일시트만으로 끝나지 않는 것이 있다. 일정 격자·업무 카드는 칸마다
        색을 직접 칠하므로 화면을 다시 조립해야 한다.
        """
        # 지금 보이는 화면만 다시 짓는다. 나머지는 그 화면으로 갈 때
        # go()가 어차피 refresh를 부르므로 미리 만들 이유가 없다.
        self._refresh_current()
        self.update()

    def shutdown(self) -> bool:
        """백그라운드 스레드를 안전하게 세운다.

        실행 중인 QThread가 파괴되면 프로세스가 죽는다. 창이 닫힐 때뿐 아니라
        테스트처럼 closeEvent 없이 정리되는 경로에서도 불러야 한다.
        """
        self._tick.stop()
        self._knowledge_tick.stop()
        tasks = self.views.get("tasks")
        if tasks is not None:
            tasks.stop()
        runners = [self.runner, self._knowledge]
        if tasks is not None:
            runners.extend(dialog._job for dialog in tasks._workboxes.values())
        for view in self.views.values():
            for name in ("_runner", "_summaries", "_warmup", "_ingest", "_studio"):
                runner = getattr(view, name, None)
                if runner is not None:
                    runners.append(runner)
        for runner in runners:
            runner.stop()
        return not any(runner.running for runner in runners)

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt 규약
        if not self.shutdown():
            event.ignore()
            self.topbar.show_idle("진행 중인 작업을 마친 뒤 안전하게 닫습니다…")
            QTimer.singleShot(250, self.close)
            return
        super().closeEvent(event)
