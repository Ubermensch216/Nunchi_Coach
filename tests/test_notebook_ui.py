import os
import hashlib
import json

os.environ.setdefault("QT_QPA_PLATFORM","offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication,QLabel

from app.core import notebook
from app.db import Database
from app.ui import theme
from app.ui.views.notebooks import NotebooksView


def test_source_selector_persists_scope_and_restores_notebook(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet())
    from app.jobs.ask_task import WarmupRunner
    monkeypatch.setattr(WarmupRunner,"start",lambda self:False)
    db=Database(tmp_path / "project.db");db.init()
    source=db.add_source(tmp_path / "sources")
    path=tmp_path / "sources" / "요청.txt";path.parent.mkdir();path.write_text("제출기한: 9월 30일",encoding="utf-8")
    doc=db.upsert_document(source,{"path":str(path),"filename":path.name,"ext":".txt","hash":hashlib.sha256(path.read_bytes()).hexdigest(),"parse_status":"ok"})
    db.replace_document_chunks(doc,[(1,"1문단","제출기한: 9월 30일")])
    book=notebook.create(db,"첫 업무 자료");notebook.add_sources(db,book,[doc])
    view=NotebooksView(db)
    try:
        assert view.notebook_id==book
        assert view.ask_view.send.isEnabled()
        class Offline:
            from types import SimpleNamespace
            def health(self):return self.SimpleNamespace(embedding_ready=False)
        _,answer=notebook.query(db,book,"제출기한",Offline())
        view.ask_view._render_answer(answer)
        assert not view.ask_view.reader_tabs.isVisible()
        view._update_notes()
        assert view.messages.count()==1
        view.source_list.item(0).setCheckState(Qt.CheckState.Unchecked)
        assert notebook.selected_snapshot(db,book)=={}
        assert not view.ask_view.send.isEnabled()
        assert view.messages.count()==0
        view.refresh()
        assert view.source_list.item(0).checkState()==Qt.CheckState.Unchecked
        notebook.archive(db,book);view.refresh()
        assert not view.ask_view.send.isEnabled()
        notebook.archive(db,book,False);view.refresh()
        assert view.notebook_id==book
    finally:
        view._runner.stop();view._warmup.stop();view.close();view.deleteLater();app.processEvents();db.close()
