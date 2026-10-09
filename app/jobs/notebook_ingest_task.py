"""노트북에 올린 파일을 뒤에서 읽는다.

읽기·그림 판독·임베딩은 파일 하나에 몇 초에서 몇 분이 걸린다. 화면을 막지
않도록 워커 스레드에서 돌고, 파일 하나가 끝날 때마다 알려 목록에 바로 나타나게
한다. AskRunner(ask_task.py)와 같은 모양이다 — 한 번에 한 묶음만 돌린다.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QThread, Qt, Signal

from ..ai.client import OllamaClient
from ..ai.settings import project_client
from ..core import diagnostics, notebook_ingest, notebook_tasks, onnara_import
from ..db import Database


class IngestWorker(QObject):
    progress = Signal(int, int, str, str)   # 몇 번째, 전체, 파일 이름, 지금 하는 일
    file_done = Signal(object)              # notebook_ingest.Ingested
    done = Signal(object)                   # list[Ingested]

    def __init__(self, db_path: Path | str, notebook_id: int, paths: list[str], doc_ids: list[int],
                 collection: str = "", tasks: list[int] | None = None):
        super().__init__()
        self._db_path = Path(db_path)
        self._notebook_id = notebook_id
        self._paths = list(paths)
        self._doc_ids = list(doc_ids)
        self._collection = collection      # 온나라 수집본 폴더
        self._tasks = list(tasks or [])    # 정리본을 만들어 넣을 업무
        self.cancelled = False
        self._client = None

    def cancel(self) -> None:
        self.cancelled = True
        if self._client is not None:
            self._client.cancel()

    def run(self) -> None:
        results = []
        db = Database(self._db_path)
        try:
            self._client = client = project_client(db, OllamaClient)
            jobs = ([("task", key) for key in self._tasks] + [("file", path) for path in self._paths]
                    + [("doc", key) for key in self._doc_ids])
            briefs = set()
            for index, (kind, target) in enumerate(jobs, start=1):
                if self.cancelled:
                    break
                if kind == "file":
                    name = Path(target).name
                elif kind == "task":
                    task = db.task(target)
                    name = notebook_tasks.brief_filename(task["name"] if task else "")
                else:
                    name = db.document(target)["filename"]

                def phase(text, i=index, n=name):
                    self.progress.emit(i, len(jobs), n, text)

                try:
                    if kind == "file":
                        outcome = notebook_ingest.ingest_file(
                            db, self._notebook_id, target, client, phase, lambda: self.cancelled)
                    elif kind == "task":
                        outcome = notebook_tasks.add_brief(
                            db, self._notebook_id, target, client, phase, lambda: self.cancelled)
                        briefs.add(outcome.doc_id)
                    else:
                        outcome = notebook_ingest.reprocess(db, target, client, phase, lambda: self.cancelled)
                except ValueError as exc:  # 그사이 업무가 없어졌거나 노트북을 보관했다
                    outcome = notebook_ingest.Ingested(str(target), name, status="rejected", error=str(exc))
                except Exception as exc:   # 파일 하나가 묶음 전체를 멈추면 안 된다
                    diagnostics.record("notebook.ingest.error", exc)
                    outcome = notebook_ingest.Ingested(str(target), name, status="rejected",
                                                       error=f"{type(exc).__name__}: {exc}")
                results.append(outcome)
                self.file_done.emit(outcome)
            # 수집본은 수십 건이 한꺼번에 들어온다. 건마다 요약을 만들면 몇십 분이 걸리므로
            # 요약은 직접 올린 파일에만 만든다.
            summarized = len(results)
            if self._collection and not self.cancelled:
                try:
                    imported = onnara_import.import_collection(
                        db, self._notebook_id, self._collection, client,
                        lambda name, text: self.progress.emit(0, 0, name, text), lambda: self.cancelled)
                except ValueError as exc:
                    imported = [notebook_ingest.Ingested(self._collection, Path(self._collection).name,
                                                         status="rejected", error=str(exc))]
                results.extend(imported)
                self.file_done.emit(None)
            # 요약은 맨 뒤에 몰아서 한다. 자료는 요약 없이도 바로 쓸 수 있어야 한다.
            for index, outcome in enumerate(results[:summarized], start=1):
                if self.cancelled:
                    break
                # 정리본은 그 자체가 정리한 글이다. 다시 요약하지 않는다.
                if (outcome.doc_id is None or outcome.status not in ("ok", "partial")
                        or outcome.doc_id in briefs):
                    continue
                self.progress.emit(index, summarized, outcome.filename, notebook_ingest.PHASE_SUMMARY)
                try:
                    if notebook_ingest.summarize_source(db, self._notebook_id, outcome.doc_id, client):
                        self.file_done.emit(outcome)
                except Exception as exc:
                    diagnostics.record("notebook.summary.error", exc)
        except Exception as exc:
            diagnostics.record("notebook.ingest.error", exc)
        finally:
            db.close()
        self.done.emit(results)


class NotebookIngestRunner(QObject):
    progress = Signal(int, int, str, str)
    file_done = Signal(object)
    done = Signal(object)

    def __init__(self, db_path: Path | str, parent: QObject | None = None):
        super().__init__(parent)
        self._db_path = db_path
        self._thread: QThread | None = None
        self._worker: IngestWorker | None = None
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.stop)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, notebook_id: int, paths: list[str], doc_ids: list[int] | None = None,
              collection: str = "", tasks: list[int] | None = None) -> bool:
        if self._thread is not None or not (paths or doc_ids or collection or tasks):
            return False
        self._thread = QThread()
        self._worker = IngestWorker(self._db_path, notebook_id, paths, doc_ids or [], collection, tasks)
        self._worker.moveToThread(self._thread)
        self._worker.progress.connect(self.progress)
        self._worker.file_done.connect(self.file_done)
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

    def _finish(self, results) -> None:
        thread, worker = self._thread, self._worker
        self._thread, self._worker = None, None
        if thread is not None:
            thread.quit()
            thread.wait(5000)
            thread.deleteLater()
        del worker
        self.done.emit(results)
