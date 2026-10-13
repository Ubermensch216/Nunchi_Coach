"""실행기를 뒤에서 돌린다.

StudioRunner와 같은 모양이다 — 한 번에 한 실행만 돌린다. 워커는 자기 스레드에서
DB 연결을 따로 연다(연결이 스레드를 넘나들지 않는다).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, Signal

from ..actions.runner import FAILED, Runner
from ..actions.registry import ToolError
from ..core import diagnostics
from ..db import Database


class ActionWorker(QObject):
    progress = Signal(int, int, int, str)   # 실행 id, 몇 번째 단계, 전체, 단계 이름
    done = Signal(int, str, str)            # 실행 id, 실행 상태, 알릴 말

    def __init__(self, db_path: Path | str, run_id: int, runner_factory=Runner):
        super().__init__()
        self._db_path = Path(db_path)
        self._run_id = run_id
        self._factory = runner_factory
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def run(self) -> None:
        state, message = FAILED, ""
        db = Database(self._db_path)
        try:
            runner = self._factory(
                db, should_stop=lambda: self.cancelled,
                on_step=lambda ordinal, total, label: self.progress.emit(
                    self._run_id, ordinal, total, label))
            state = runner.execute(self._run_id)
        except (ToolError, ValueError) as exc:
            message = str(exc)
        except Exception as exc:   # 워커가 조용히 죽으면 진행 표시가 영원히 돈다
            diagnostics.record("action.error", exc)
            message = f"실행하지 못했습니다 ({type(exc).__name__})."
        finally:
            db.close()
        self.done.emit(self._run_id, state, message)


class ActionRunner(QObject):
    progress = Signal(int, int, int, str)
    done = Signal(int, str, str)

    def __init__(self, db_path: Path | str, parent: QObject | None = None, runner_factory=Runner):
        super().__init__(parent)
        self._db_path = db_path
        self._factory = runner_factory
        self._thread: QThread | None = None
        self._worker: ActionWorker | None = None
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.stop)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, run_id: int) -> bool:
        if self._thread is not None:
            return False
        self._thread = QThread()
        self._worker = ActionWorker(self._db_path, run_id, self._factory)
        self._worker.moveToThread(self._thread)
        self._worker.progress.connect(self.progress)
        self._worker.done.connect(self._finish)
        self._worker.done.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)
        self._thread.started.connect(self._worker.run)
        self._thread.start()
        return True

    def cancel(self) -> None:
        """지금 도는 단계가 끝나면 멈춘다. 끝낸 단계와 산출물은 남는다."""
        if self._worker is not None:
            self._worker.cancel()

    def stop(self, wait_ms: int = 5000) -> None:
        self.cancel()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(wait_ms)

    def _finish(self, run_id: int, state: str, message: str) -> None:
        # 워커를 스레드가 끝날 때까지 붙든다(StudioRunner._finish와 같은 이유).
        thread, worker = self._thread, self._worker
        self._thread, self._worker = None, None
        if thread is not None:
            thread.quit()
            thread.wait(5000)
            thread.deleteLater()
        del worker
        self.done.emit(run_id, state, message)
