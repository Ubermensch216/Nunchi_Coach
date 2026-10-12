"""최초 분석의 결과 진입과 실패·중단·재개. 실제 모델 없이 상태 전환을 검증한다."""

import os
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from app.core import initial_analysis
from app.db import Database
from app.jobs.pipeline import Pipeline
from app.ui import shell


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path / "project.db")
    db.init()
    db.add_source(tmp_path / "자료")
    db.set_meta("initial_analysis_state", "pending")
    yield db
    db.close()


def saved_result(db, *, status="ok", discovery="completed", tasks=True):
    source = db.work_sources()[0]["id"]
    db.upsert_document(source, {"path": str(db.path.parent / "계획.hwpx"), "filename": "계획.hwpx",
                               "ext": ".hwpx", "parse_status": status})
    if tasks:
        db.con.execute("INSERT OR IGNORE INTO tasks(id,name) VALUES (1,'환경정책')")
    db.con.execute("INSERT OR REPLACE INTO pipeline_stage_results(stage,state,done,total,note,errors) "
                   "VALUES ('업무 파악',?,1,1,'','[]')", (discovery,))
    db.set_meta("analysis_run_state", "completed")


@pytest.mark.parametrize("run_state, expected", [("running", "paused"), ("cancelled", "paused"), ("failed", "failed")])
def test_saved_result_is_not_completion_after_interruption_or_failure(db, run_state, expected):
    saved_result(db)
    db.set_meta("analysis_run_state", run_state)
    result = initial_analysis.outcome(db)
    assert result.state == expected and not result.can_open


def test_empty_folder_unknown_tasks_and_ai_hold_have_distinct_outcomes(db):
    db.set_meta("analysis_run_state", "completed")
    assert initial_analysis.outcome(db).state == "empty"
    saved_result(db, tasks=False)
    assert initial_analysis.outcome(db).state == "no_tasks"
    db.con.execute("UPDATE pipeline_stage_results SET state='pending' WHERE stage='업무 파악'")
    assert initial_analysis.outcome(db).state == "unavailable"


def test_partial_result_can_be_opened_but_core_discovery_failure_cannot(db):
    saved_result(db, status="empty")
    assert initial_analysis.outcome(db).state == "partial"
    assert initial_analysis.outcome(db).can_open
    db.con.execute("UPDATE pipeline_stage_results SET state='partial' WHERE stage='업무 파악'")
    assert not initial_analysis.outcome(db).can_open


def test_notebook_uploads_do_not_make_an_empty_work_folder_look_analysed(db):
    db.con.execute("DELETE FROM meta WHERE key='initial_analysis_state'")
    upload = db.add_source(db.path.parent / "uploads", kind="upload")
    db.upsert_document(upload, {"path": str(db.path.parent / "note.txt"), "filename": "note.txt",
                               "ext": ".txt", "parse_status": "ok"})
    assert initial_analysis.pending(db)
    db.set_meta("analysis_run_state", "completed")
    assert initial_analysis.outcome(db).state == "empty"


def test_existing_results_keep_their_home_during_additional_analysis(db):
    saved_result(db)
    db.con.execute("DELETE FROM meta WHERE key='initial_analysis_state'")
    db.set_meta("analysis_run_state", "running")
    assert not initial_analysis.pending(db)


class Runner(QObject):
    progress = Signal(str, int, int, str)
    stage_done = Signal(object)
    finished = Signal()
    failed = Signal(str)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.running = False
        self.starts = 0

    def start(self):
        self.running = True
        self.starts += 1

    def stop(self, wait_ms=5000):
        self.running = False


@pytest.fixture
def window(qapp, db, monkeypatch):
    from app.jobs.ask_task import _WarmupWorker
    monkeypatch.setattr(_WarmupWorker, "run", lambda self: self.finished.emit())
    monkeypatch.setattr(shell, "PipelineRunner", Runner)
    monkeypatch.setattr(shell.MainWindow, "_resume_knowledge", lambda self: None)
    window = shell.MainWindow(db)
    yield window
    window.shutdown()
    window.close()
    window.deleteLater()
    qapp.processEvents()


def finish(window):
    window.runner.running = False
    window.runner.finished.emit()


def test_initial_analysis_hides_changing_candidates_and_opens_home_once_saved(window, db):
    assert window.runner.starts == 1
    assert window.stack.currentWidget() is window.views["initial_analysis"]
    saved_result(db)
    window.go("tasks")
    assert window.stack.currentWidget() is window.views["initial_analysis"]
    finish(window)
    assert window.stack.currentWidget() is window.views["tasks"]
    assert db.get_meta("initial_analysis_state") == "ready"


def test_completion_does_not_take_the_user_away_from_the_notebook(window, db):
    window.go("notebooks")
    saved_result(db)
    finish(window)
    assert window.stack.currentWidget() is window.views["notebooks"]
    window.go("tasks")
    assert window.stack.currentWidget() is window.views["initial_analysis"]
    window.views["initial_analysis"].action.click()
    assert window.stack.currentWidget() is window.views["tasks"]


def test_partial_files_require_an_explicit_result_open(window, db):
    saved_result(db, status="empty")
    finish(window)
    view = window.views["initial_analysis"]
    assert window.stack.currentWidget() is view
    assert view.result.state == "partial" and view.result.can_open
    view.action.click()
    assert db.get_meta("initial_analysis_state") == "ready"


def test_failed_then_finished_signals_do_not_clear_the_error(window, db):
    saved_result(db)
    # 실패 callback 자체도 보존한다. 상태 저장까지 실패한 경우를 포함한다.
    window.runner.failed.emit("테스트 오류")
    finish(window)
    view = window.views["initial_analysis"]
    assert view.result.state == "failed" and not view.result.can_open
    assert window.stack.currentWidget() is view
    assert "오류" in window.topbar.status.text()


@pytest.mark.parametrize("state", ["cancelled", "failed", "completed"])
def test_reopening_a_stopped_or_unsuccessful_first_analysis_does_not_restart(window, db, state):
    window.runner.running = False
    starts = window.runner.starts
    db.set_meta("analysis_run_state", state)
    window._resume_if_pending()
    assert window.runner.starts == starts


def test_progress_is_for_the_current_stage_not_a_made_up_total(window):
    view = window.views["initial_analysis"]
    window.runner.progress.emit("파일 찾기", 12, 0, "")
    assert view.progress.maximum() == 0 and "12건" in view.summary.text()
    window.runner.progress.emit("내용 읽기", 3, 10, "")
    assert view.progress.maximum() == 10 and view.progress.value() == 3


def test_pause_keeps_the_button_disabled_until_the_worker_finishes(window, db):
    view = window.views["initial_analysis"]
    view.action.click()
    view.refresh()
    assert not view.action.isEnabled() and view.action.text() == "중단하는 중…"
    db.set_meta("analysis_run_state", "cancelled")
    finish(window)
    assert view.action.isEnabled() and view.action.text() == "이어서 분석하기"


@pytest.mark.parametrize("mode", ["completed", "cancelled", "failed"])
def test_pipeline_persists_its_terminal_state_before_finished(db, qapp, monkeypatch, mode):
    worker = Pipeline(db.path)
    for stage in ("_scan", "_hash", "_parse", "_pictures", "_date", "_embed", "_chunks", "_discover", "_roles", "_cycles", "_steps", "_restore"):
        monkeypatch.setattr(worker, stage, lambda _db: None)
    from app.core.knowledge import lifecycle
    monkeypatch.setattr(lifecycle, "run_jobs", lambda *args, **kwargs: None)
    if mode == "cancelled":
        worker.stop()
    elif mode == "failed":
        def fail(_db):
            raise ValueError("test")
        monkeypatch.setattr(worker, "_scan", fail)
    observed = []
    worker.finished.connect(lambda: observed.append(db.get_meta("analysis_run_state")))
    worker.run()
    assert observed == [mode]
