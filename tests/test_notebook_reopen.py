from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from app.core import notebook, notebook_ingest
from app.db import Database
from app.ingest.parsers.base import version_of
from app.ui.views.notebooks import NotebooksView


@pytest.fixture
def uploaded(tmp_path, monkeypatch):
    app=QApplication.instance() or QApplication([])
    from app.jobs.ask_task import WarmupRunner
    monkeypatch.setattr(WarmupRunner,'start',lambda self:False)
    db=Database(tmp_path/'project'/'project.db')
    db.init()
    book=notebook.create(db,'업로드 자료')
    source=tmp_path/'original.txt'
    source.write_text('2026. 9. 28. 기존 업무 계획',encoding='utf-8')
    result=notebook_ingest.ingest_file(db,book,source)
    db.con.execute("UPDATE notebook_sources SET summary='기존 요약',topics='[\"계획\"]' WHERE notebook_id=?",(book,))
    view=NotebooksView(db)
    yield SimpleNamespace(app=app,db=db,book=book,doc=result.doc_id,view=view)
    view._ingest.stop();view._warmup.stop();view._runner.stop();view._studio.stop()
    view.close();view.deleteLater();app.processEvents();db.close()


@pytest.mark.parametrize('state',['current','old_parser','pending'])
def test_open_and_reopen_never_launch_ingestion_or_change_saved_document(uploaded,monkeypatch,state):
    u=uploaded
    if state=='old_parser': u.db.update_document(u.doc,parser_version='obsolete')
    if state=='pending': u.db.update_document(u.doc,parse_status='pending')
    rows=lambda:[tuple(r) for r in u.db.con.execute('SELECT * FROM document_sections WHERE doc_id=?',(u.doc,))]
    before=rows()
    doc_before=dict(u.db.document(u.doc))
    starts=[]
    monkeypatch.setattr(u.view._ingest,'start',lambda *a,**k:starts.append((a,k)))
    for _ in range(3):
        u.view.open_notebook(u.book)
        u.view.refresh()
        sources=u.view.workspace.sources
        assert sources.progress_box.isHidden()
        assert sources.resume_button.isHidden()==(state=='current')
        if state=='pending': assert '처리 대기' in sources.source_list.item(0).text()
        u.view.show_home()
    assert starts==[]
    assert rows()==before and dict(u.db.document(u.doc))==doc_before
    assert notebook.sources(u.db,u.book)[0]['summary']=='기존 요약'


def test_explicit_resume_only_enqueues_this_notebooks_pending_documents(uploaded,monkeypatch):
    u=uploaded
    other=notebook.create(u.db,'다른 업무노트')
    path=u.db.path.parent/'another.txt'
    path.write_text('다른 내용',encoding='utf-8')
    other_doc=notebook_ingest.ingest_file(u.db,other,path).doc_id
    u.db.update_document(u.doc,parse_status='pending')
    u.db.update_document(other_doc,parse_status='pending')
    u.view.open_notebook(u.book)
    starts=[]
    def start(*args,**kwargs):
        starts.append((args,kwargs));return True
    monkeypatch.setattr(u.view._ingest,'start',start)
    sources=u.view.workspace.sources
    assert not starts
    sources.resume_button.click()
    assert starts==[((u.book,[],[u.doc]),{})]
    assert not sources.progress_box.isHidden()
    assert not sources.resume_button.isEnabled()
    sources._on_ingest_done([notebook_ingest.Ingested('', 'original.txt',u.doc,status='ok')])
    assert '처리했습니다' in sources._report and '추가했습니다' not in sources._report


def test_archived_notebook_does_not_offer_or_start_processing(uploaded,monkeypatch):
    u=uploaded
    u.db.update_document(u.doc,parser_version='obsolete')
    notebook.archive(u.db,u.book)
    starts=[]
    monkeypatch.setattr(u.view._ingest,'start',lambda *a,**k:starts.append(a))
    u.view.open_notebook(u.book)
    sources=u.view.workspace.sources
    assert sources.resume_button.isHidden()
    sources._resume_uploads()
    assert starts==[]


def test_stale_queue_entry_reuses_current_parse_without_any_expensive_work(uploaded,monkeypatch):
    u=uploaded
    before=[tuple(r) for r in u.db.sections(u.doc)]
    def unexpected(*a,**k): raise AssertionError('completed document must not be processed again')
    monkeypatch.setattr(notebook_ingest,'read_document',unexpected)
    monkeypatch.setattr(notebook_ingest,'write_chunks',unexpected)
    monkeypatch.setattr(notebook_ingest,'write_date',unexpected)
    phases=[]
    result=notebook_ingest.reprocess(u.db,u.doc,on_phase=phases.append)
    assert result.status=='reused' and not phases
    assert [tuple(r) for r in u.db.sections(u.doc)]==before


def test_explicit_parser_update_runs_once_then_becomes_reusable(uploaded,monkeypatch):
    u=uploaded
    u.db.update_document(u.doc,parser_version='obsolete')
    assert notebook_ingest.outdated_uploads(u.db,u.book)==[u.doc]
    real=notebook_ingest.read_document
    calls=[]
    def read(*a,**k): calls.append(True);return real(*a,**k)
    monkeypatch.setattr(notebook_ingest,'read_document',read)
    first=notebook_ingest.reprocess(u.db,u.doc)
    second=notebook_ingest.reprocess(u.db,u.doc)
    assert first.status=='ok' and second.status=='reused' and len(calls)==1
    assert u.db.document(u.doc)['parser_version']==version_of('text')
    assert notebook_ingest.outdated_uploads(u.db,u.book)==[]


def test_worker_does_not_summarize_an_already_completed_queue_entry(uploaded,monkeypatch):
    from app.jobs import notebook_ingest_task
    u=uploaded
    monkeypatch.setattr(notebook_ingest_task,'project_client',lambda *a:None)
    def unexpected(*a,**k): raise AssertionError('existing summary must be reused')
    monkeypatch.setattr(notebook_ingest,'summarize_source',unexpected)
    worker=notebook_ingest_task.IngestWorker(u.db.path,u.book,[],[u.doc])
    results=[]
    worker.done.connect(results.extend)
    worker.run()
    assert len(results)==1 and results[0].status=='reused'
    assert notebook.sources(u.db,u.book)[0]['summary']=='기존 요약'
