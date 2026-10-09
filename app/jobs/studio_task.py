"""스튜디오의 초안 엮기를 뒤에서 돌린다.

서식의 항목마다 생성 호출이 한 번씩 있어 한 문서에 1분 안팎이 걸린다.
NotebookIngestRunner와 같은 모양이다 — 한 번에 한 문서만 엮는다.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QThread, Qt, Signal

from ..ai.client import OllamaClient
from ..ai.settings import project_client
from ..core import diagnostics
from ..db import Database
from ..studio import compose, outputs, templates


class StudioWorker(QObject):
    progress = Signal(int, int, str)     # 몇 번째 항목, 전체, 항목 이름
    done = Signal(object, str)           # (산출물 id 또는 None, 알릴 말)

    def __init__(self, db_path: Path | str, notebook_id: int, template_id: int, title: str, request: str):
        super().__init__()
        self._db_path = Path(db_path)
        self._args = (notebook_id, template_id, title, request)
        self.cancelled = False
        self._client = None

    def cancel(self) -> None:
        self.cancelled = True
        if self._client is not None:
            self._client.cancel()

    def run(self) -> None:
        notebook_id, template_id, title, request = self._args
        output_id, message = None, ""
        db = Database(self._db_path)
        try:
            self._client = client = project_client(db, OllamaClient)
            template = templates.get(db, template_id)
            draft = compose.compose(db, notebook_id, template, client, title, request,
                                    on_progress=self.progress.emit)
            if self.cancelled:
                message = "문서 만들기를 취소했습니다."
            else:
                output_id = outputs.save_draft(db, notebook_id, template, draft)
                message = draft.note
        except ValueError as exc:
            message = str(exc)
        except Exception as exc:   # 워커가 조용히 죽으면 진행 표시가 영원히 돈다
            diagnostics.record("studio.compose.error", exc)
            message = f"문서를 만들지 못했습니다 ({type(exc).__name__})."
        finally:
            db.close()
        self.done.emit(output_id, message)


class StudioRunner(QObject):
    progress = Signal(int, int, str)
    done = Signal(object, str)

    def __init__(self, db_path: Path | str, parent: QObject | None = None):
        super().__init__(parent)
        self._db_path = db_path
        self._thread: QThread | None = None
        self._worker: StudioWorker | None = None
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.stop)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, notebook_id: int, template_id: int, title: str = "", request: str = "") -> bool:
        if self._thread is not None:
            return False
        self._thread = QThread()
        self._worker = StudioWorker(self._db_path, notebook_id, template_id, title, request)
        self._worker.moveToThread(self._thread)
        self._worker.progress.connect(self.progress)
        self._worker.done.connect(self._finish)
        self._worker.done.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)
        self._worker.done.connect(self._worker.deleteLater)
        self._thread.started.connect(self._worker.run)
        self._thread.start()
        return True

    def cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def stop(self, wait_ms: int = 5000) -> None:
        self.cancel()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(wait_ms)

    def _finish(self, output_id, message: str) -> None:
        # 워커를 스레드가 끝날 때까지 붙든다. 여기서 마지막 참조를 놓으면 워커가 아직
        # done을 내보내는 중인데 객체가 지워져 프로세스가 통째로 죽는다(abort).
        thread, worker = self._thread, self._worker
        self._thread, self._worker = None, None
        if thread is not None:
            thread.quit()
            thread.wait(5000)
            thread.deleteLater()
        del worker
        self.done.emit(output_id, message)
