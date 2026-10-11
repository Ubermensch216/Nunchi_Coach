"""작은 시간 예산으로 그래프 영속 큐를 처리하는 앱 워커."""
from PySide6.QtCore import QCoreApplication, QObject, QThread, Qt, Signal

from ..core import diagnostics
from ..core.knowledge.lifecycle import run_jobs
from ..db import Database


class KnowledgeWorker(QObject):
    done = Signal(object)

    def __init__(self, path):
        super().__init__()
        self.path = path
        self.cancelled = False

    def run(self):
        report = None
        try:
            with Database(self.path) as db:
                report = run_jobs(db, should_stop=lambda: self.cancelled, seconds=2)
                if report.failed:
                    diagnostics.record('knowledge.jobs.failed', detail={'failed': report.failed})
        except Exception as exc:
            diagnostics.record('knowledge.jobs.error', exc)
        finally:
            self.done.emit(report)


class KnowledgeRunner(QObject):
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path, self._thread, self._worker = path, None, None
        app = QCoreApplication.instance()
        if app:
            app.aboutToQuit.connect(self.stop)

    @property
    def running(self):
        return self._thread is not None and self._thread.isRunning()

    def start(self):
        if self._thread is not None:
            return False
        self._thread = QThread()
        self._worker = KnowledgeWorker(self.path)
        self._worker.moveToThread(self._thread)
        self._worker.done.connect(self._finish)
        self._worker.done.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)
        self._worker.done.connect(self._worker.deleteLater)
        self._thread.started.connect(self._worker.run)
        self._thread.start()
        return True

    def stop(self, wait_ms=5000):
        if self._worker:
            self._worker.cancelled = True
        if self._thread:
            self._thread.quit()
            self._thread.wait(wait_ms)

    def _finish(self, report):
        thread = self._thread
        self._thread, self._worker = None, None
        if thread:
            thread.quit()
            thread.wait(5000)
            thread.deleteLater()
