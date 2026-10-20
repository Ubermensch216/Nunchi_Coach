import os
import hashlib
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM","offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication,QLabel

from app.core import notebook
from app.db import Database
from app.ui import theme
from app.ui.views.notebooks import NotebooksView


class Offline:
    def health(self):return SimpleNamespace(embedding_ready=False)


@pytest.fixture
def workspace(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet())
    from app.jobs.ask_task import WarmupRunner
    monkeypatch.setattr(WarmupRunner,"start",lambda self:False)
    db=Database(tmp_path / "project.db");db.init()
    source=db.add_source(tmp_path / "sources")
    (tmp_path / "sources").mkdir()
    docs=[]
    for name,text in (("요청.txt","제출기한: 9월 30일"),("참고.txt","담당 부서: 총무과")):
        path=tmp_path / "sources" / name;path.write_text(text,encoding="utf-8")
        doc=db.upsert_document(source,{"path":str(path),"filename":name,"ext":".txt","hash":hashlib.sha256(path.read_bytes()).hexdigest(),"parse_status":"ok"})
        db.replace_document_chunks(doc,[(1,"1문단",text)])
        db.con.execute("INSERT INTO document_sections(doc_id,kind,ordinal,locator,text) VALUES (?,'paragraph',1,'1문단',?)",(doc,text))
        docs.append(doc)
    book=notebook.create(db,"첫 업무 자료");notebook.add_sources(db,book,docs[:1])
    view=NotebooksView(db)
    yield SimpleNamespace(app=app,db=db,view=view,book=book,docs=docs)
    view._runner.stop();view._warmup.stop();view._ingest.stop();view._studio.stop();view.close();view.deleteLater();app.processEvents();db.close()


def test_home_lists_notebooks_as_cards_and_opens_the_workspace(workspace):
    view,db=workspace.view,workspace.db
    assert view.stack.currentWidget() is view.home and view.notebook_id is None
    assert [card.notebook_id for card in view.home.cards]==[workspace.book]
    assert any(label.text()=="자료 1건" or label.text().endswith("· 자료 1건") for label in view.home.findChildren(QLabel))
    view.home.cards[0].opened.emit(workspace.book)
    assert view.stack.currentWidget() is view.workspace and view.notebook_id==workspace.book
    assert db.con.execute("SELECT last_opened_at FROM notebooks WHERE id=?",(workspace.book,)).fetchone()[0]
    assert view.workspace.home_button.text() == ""
    assert not view.workspace.home_button.icon().isNull()
    assert view.workspace.home_button.accessibleName() == "업무노트 홈으로 이동"
    view.workspace.home_button.click()
    assert view.stack.currentWidget() is view.home and view.notebook_id is None


def test_home_search_archive_filter_and_list_mode(workspace):
    view,db=workspace.view,workspace.db
    other=notebook.create(db,"예산 검토");notebook.archive(db,other)
    view.home.refresh()
    assert [card.notebook_id for card in view.home.cards]==[workspace.book]
    view.home.archived.setChecked(True)
    assert {card.notebook_id for card in view.home.cards}=={workspace.book,other}
    view.home.search.setText("예산")
    assert [card.notebook_id for card in view.home.cards]==[other]
    view.home.search.setText("")
    view.home.mode_button.click()
    assert not view.home.cards and db.get_meta("notebook_home_mode")=="list"


def test_duplicate_copies_sources_only_and_delete_removes_the_notebook(workspace):
    db,book=workspace.db,workspace.book
    notebook.query(db,book,"제출기한",Offline())
    copy=notebook.duplicate(db,book)
    assert [r["doc_id"] for r in notebook.sources(db,copy)]==workspace.docs[:1]
    assert not notebook.messages(db,copy)
    notebook.delete(db,book)
    assert db.con.execute("SELECT COUNT(*) FROM notebook_messages").fetchone()[0]==0
    assert db.document(workspace.docs[0]) is not None
    with pytest.raises(ValueError):notebook.require(db,book,active=False)


def test_source_selection_gates_the_chat_and_hides_answers_from_another_selection(workspace):
    view,db,book=workspace.view,workspace.db,workspace.book
    view.open_notebook(book)
    chat,sources=view.workspace.chat,view.workspace.sources
    assert chat.send.isEnabled() and chat.scope.text()=="자료 1건"
    notebook.query(db,book,"제출기한",Offline())
    chat.refresh()
    assert any(label.text()=="자료 검색 결과" for label in chat.findChildren(QLabel))
    sources.source_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert notebook.selected_snapshot(db,book)=={}
    assert not chat.send.isEnabled()
    assert not any(label.text()=="자료 검색 결과" for label in chat.findChildren(QLabel))
    assert any("답을 가렸습니다" in label.text() for label in chat.findChildren(QLabel))
    view.refresh()
    assert sources.source_list.item(0).checkState()==Qt.CheckState.Unchecked
    sources.all_box.click()
    assert chat.send.isEnabled()
    assert any(label.text()=="자료 검색 결과" for label in chat.findChildren(QLabel))


def test_citation_opens_the_original_in_the_sources_panel(workspace):
    view,db,book=workspace.view,workspace.db,workspace.book
    view.open_notebook(book)
    _,answer=notebook.query(db,book,"제출기한",Offline())
    view.workspace.chat.refresh()
    view.workspace.chat.citation_requested.emit(answer.citations[0])
    sources=view.workspace.sources
    assert sources.stack.currentIndex()==1 and sources.reader_doc_id==workspace.docs[0]
    assert any(label.objectName()=="ParaCited" for label in sources.findChildren(QLabel))
    # 선택에서 뺀 자료의 인용은 원문을 펴지 않는다.
    notebook.select(db,book,workspace.docs[0],False)
    sources.show_citation(answer.citations[0])
    assert sources.reader_doc_id is None


def test_archived_notebook_is_read_only_and_restores(workspace):
    view,db,book=workspace.view,workspace.db,workspace.book
    view.open_notebook(book)
    notebook.archive(db,book);view.refresh()
    chat=view.workspace.chat
    assert not chat.send.isEnabled() and not view.workspace.banner.isHidden()
    assert not view.workspace.sources.add_button.isEnabled()
    notebook.archive(db,book,False);view.refresh()
    assert chat.send.isEnabled() and view.workspace.banner.isHidden()


def test_panels_fold_and_the_layout_is_remembered_per_notebook(workspace):
    view,db,book=workspace.view,workspace.db,workspace.book
    view.open_notebook(book)
    view.workspace.studio.collapse_requested.emit()
    assert view.workspace.studio.isHidden() and not view.workspace.toggles["studio"].isChecked()
    assert notebook.layout(db,book)["studio"] is False
    view.show_home();view.open_notebook(book)
    assert view.workspace.studio.isHidden() and not view.workspace.sources.isHidden()
    view.workspace.toggles["studio"].click()
    assert not view.workspace.studio.isHidden()


def test_memo_is_editable_but_cannot_be_promoted(workspace):
    view,db,book=workspace.view,workspace.db,workspace.book
    view.open_notebook(book)
    note=notebook.add_memo(db,book,"확인할 것","총무과에 기한 재확인")
    studio=view.workspace.studio;studio.select_latest()
    assert studio.editor.toPlainText()=="총무과에 기한 재확인" and not studio.editor.isReadOnly()
    notebook.edit_note(db,note,"총무과에 기한 재확인 완료","확인함")
    assert db.con.execute("SELECT state FROM notebook_notes WHERE id=?",(note,)).fetchone()[0]=="memo"
    with pytest.raises(ValueError):notebook.promote(db,note,"후임자","전체")


def test_uploading_a_file_from_the_sources_panel_makes_it_askable(workspace,tmp_path,monkeypatch):
    import time
    from app.jobs import notebook_ingest_task
    class NoModel:
        embed_model="none"
        def health(self):return SimpleNamespace(embedding_ready=False,generation_ready=False,message="offline")
        def _supports(self,_):return False
        def cancel(self):pass
    monkeypatch.setattr(notebook_ingest_task,"project_client",lambda db,factory=None:NoModel())
    view,db,app=workspace.view,workspace.db,workspace.app
    empty=notebook.create(db,"빈 업무노트")
    view.open_notebook(empty)
    sources,chat=view.workspace.sources,view.workspace.chat
    assert not chat.send.isEnabled()
    upload=tmp_path / "내려받은 공문.txt";upload.write_text("회신기한: 10월 30일",encoding="utf-8")
    broken=tmp_path / "깨진.docx";broken.write_text("문서 아님",encoding="utf-8")
    sources.upload([str(upload),str(broken)])
    assert not sources.progress_box.isHidden() and not sources.add_button.isEnabled()
    deadline=time.monotonic()+20
    while sources._ingest.running or not sources.progress_box.isHidden():
        app.processEvents()
        assert time.monotonic()<deadline
    assert sources.source_list.count()==2
    assert chat.send.isEnabled() and chat.scope.text()=="자료 1건"
    assert "1건을 추가했습니다" in sources.hint.text() and "깨진.docx" in sources.hint.text()
    labels=[sources.source_list.item(i).text() for i in range(2)]
    assert any("읽지 못함" in label for label in labels)
    assert upload.read_text(encoding="utf-8")=="회신기한: 10월 30일"
    assert db.work_sources()==[db.sources()[0]]


def test_studio_makes_a_document_from_a_template_and_exports_it(workspace,tmp_path,monkeypatch):
    import time
    from app.jobs import studio_task
    from app.studio import outputs, templates
    class Writer:
        cancelled=False
        def health(self):return SimpleNamespace(embedding_ready=False,generation_ready=True)
        def cancel(self):pass
        def generate_json(self,prompt,schema,num_predict=400,temperature=0.0):
            hit="지금 쓸 항목: 2. 기한과 일정" in prompt
            return {"sentences":[{"text":"제출기한: 9월 30일","sources":[1]}] if hit else []},None,""
    monkeypatch.setattr(studio_task,"project_client",lambda db,factory=None:Writer())
    view,db,book,app=workspace.view,workspace.db,workspace.book,workspace.app
    view.open_notebook(book)
    studio=view.workspace.studio
    assert len(studio.tile_buttons)==len(templates.BUILTIN) and all(b.isEnabled() for b in studio.tile_buttons)
    briefing=next(t for t in templates.all_templates(db) if t.key=="briefing")
    studio.compose(briefing.id,"첫 업무 브리핑")
    assert not studio.progress_box.isHidden() and not studio.tile_buttons[0].isEnabled()
    deadline=time.monotonic()+20
    while studio._studio.running or not studio.progress_box.isHidden():
        app.processEvents()
        assert time.monotonic()<deadline
    assert studio.outputs.count()==1 and studio.pages.currentIndex()==0
    body=studio.output_editor.toPlainText()
    assert body.startswith("# 첫 업무 브리핑") and "- 제출기한: 9월 30일[1]" in body and "확인 필요" in body
    assert "‘확인 필요’로 남겼습니다" in studio.output_state.text()
    # 산출물은 표기가 아니라 문서 모양으로 보인다. 표기는 [고치기]를 눌러야 나온다.
    shown=studio.output_view.toPlainText()
    assert studio.output_stack.currentIndex()==0 and "#" not in shown and "첫 업무 브리핑" in shown
    assert "○ 제출기한: 9월 30일" in shown and studio.output_save.isHidden()
    studio.output_edit.click()
    assert studio.output_stack.currentIndex()==1
    studio.output_editor.setPlainText(body.replace("# 첫 업무 브리핑","# 고친 제목"))
    studio.output_save.click()
    assert studio.output_stack.currentIndex()==0 and "고친 제목" in studio.output_view.toPlainText()
    row=outputs.outputs(db,book)[0]
    assert row["title"]=="고친 제목" and row["state"]=="edited"
    monkeypatch.setattr("app.ui.views.notebooks.studio_panel.QFileDialog.getSaveFileName",
                        lambda *a,**k:(str(tmp_path / "out" / "브리핑.hwpx"),""))
    studio._export_output("hwpx")
    assert (tmp_path / "out" / "브리핑.hwpx").is_file() and "한글에서 열어" in studio.report.text()


def test_new_notebook_opens_at_once_with_a_default_title(workspace,monkeypatch):
    view,db=workspace.view,workspace.db
    from PySide6.QtWidgets import QInputDialog
    monkeypatch.setattr(QInputDialog,"getText",lambda *a,**k:pytest.fail("이름을 묻지 않는다"))
    view.home._create()
    assert view.stack.currentWidget() is view.workspace
    assert notebook.require(db,view.notebook_id)["title"]==notebook.DEFAULT_TITLE
    assert notebook.untitled(db)==notebook.DEFAULT_TITLE+" 2"


def test_folded_panel_leaves_a_rail_that_unfolds_it(workspace):
    view=workspace.view;view.open_notebook(workspace.book)
    ws=view.workspace
    assert ws.rails["sources"].isHidden()
    ws.sources.fold.click()
    assert ws.sources.isHidden() and not ws.rails["sources"].isHidden()
    ws.rails["sources"].unfold.click()
    assert not ws.sources.isHidden() and ws.rails["sources"].isHidden()


def test_answer_renders_tables_lists_and_escapes_source_text():
    from app.ui.views.notebooks.answer_html import answer_html
    page=answer_html("**요약**입니다.[1]\n\n| 구분 | 금액 |\n|---|---|\n| A<b> | 1,200 |\n\n- 하나\n- 둘")
    assert "<table" in page and page.count("<td")==2 and 'align="right"' in page
    assert "A&lt;b&gt;" in page and "<b>요약</b>" in page and 'href="1"' in page
    assert page.count("<li")==2 and "|" not in page
    facts=answer_html("- 계획.hwpx · 2026 · 총사업비: 12,000천원 [1]\n- 최종.hwpx · 2026 · 총사업비: 13,500천원 [2]")
    assert facts.count("<tr>")==3 and "총사업비" in facts
    assert "<li" not in answer_html("9. 30.까지 제출합니다.")
    # 근거 달린 문장이 한 문단으로 이어져 오면 문장마다 끊어 세운다. 글자는 바꾸지 않는다.
    run=answer_html("핵심 주장입니다.[1] 둘째 근거입니다.[1] 셋째는 9. 30.까지입니다.[2][3] 넷째입니다.[3]")
    assert run.count("<p")==1 and run.count("<li")==3 and "셋째는 9. 30.까지입니다." in run
    assert answer_html("하나입니다.[1] 둘입니다.[2]").count("<p")==2
    plain=answer_html("근거 없는 문장입니다. 이어지는 문장입니다.[1]")
    assert plain.count("<p")==1 and "<li" not in plain


def test_withheld_answer_names_the_cause_and_offers_a_whole_material_pass(workspace):
    from PySide6.QtWidgets import QCheckBox,QPushButton
    view,db,book=workspace.view,workspace.db,workspace.book
    view.open_notebook(book)
    chat=view.workspace.chat
    assert not any(box.text()=="전체 정리" for box in chat.findChildren(QCheckBox))
    snapshot=notebook.dumps(notebook.selected_snapshot(db,book))
    for mode,code in (("rag","model_declined"),("overview","no_facts")):
        db.con.execute("INSERT INTO notebook_messages(notebook_id,question,answer,source_snapshot,citations,generation,"
                       "model,withheld,standalone_question,mode,law_citations) VALUES (?,?,?,?,'[]',?,'fake',1,'',?,'{}')",
                       (book,"예산 내용을 정리해 줘","확인 가능한 자료가 부족합니다.",snapshot,
                        notebook.dumps({"withheld":code,"mode":mode}),mode))
    chat.refresh()
    texts=[label.text() for label in chat.findChildren(QLabel)]
    assert any("확정하지 못했습니다" in text for text in texts) and not any("자료가 부족" in text for text in texts)
    again=[b for b in chat.findChildren(QPushButton) if b.text()=="자료 전체를 훑어 다시 정리"]
    assert len(again)==1                       # 이미 전체를 훑은 답에는 붙지 않는다
    started=[]
    chat._runner.start=lambda question,scope:started.append((question,scope.mode)) or True
    again[0].click()
    assert started==[("예산 내용을 정리해 줘","overview")] and chat._whole
    chat._on_progress(2,5)
    assert "5묶음 중 3번째" in chat.waiting.text()


def test_empty_chat_shows_the_guides_of_the_selected_sources_only(workspace):
    view,db,book=workspace.view,workspace.db,workspace.book
    notebook.add_sources(db,book,workspace.docs[1:])
    for doc,summary in zip(workspace.docs,("요청 문서의 안내이다.","참고 문서의 안내이다.")):
        db.con.execute("UPDATE notebook_sources SET summary=?,topics=? WHERE notebook_id=? AND doc_id=?",
                       (summary,notebook.dumps(["제출기한"]),book,doc))
    notebook.select(db,book,workspace.docs[1],False)
    view.open_notebook(book)
    chat=view.workspace.chat
    texts=[label.text() for label in chat.findChildren(QLabel)]
    assert "요청 문서의 안내이다." in texts and "참고 문서의 안내이다." not in texts
    assert any("원문이 아닙니다" in text for text in texts) and any("주제어 — 제출기한" in text for text in texts)
    notebook.select(db,book,workspace.docs[1],True)
    chat.refresh()
    assert "참고 문서의 안내이다." in [label.text() for label in chat.findChildren(QLabel)]
    notebook.query(db,book,"제출기한",Offline())
    chat.refresh()
    assert "요청 문서의 안내이다." not in [label.text() for label in chat.findChildren(QLabel)]


def test_source_item_row_icons_and_actions(workspace):
    view, db, book = workspace.view, workspace.db, workspace.book
    view.open_notebook(book)
    sources = view.workspace.sources
    assert sources.source_list.count() == 1
    item = sources.source_list.item(0)
    row = sources.source_list.itemWidget(item)
    assert row is not None

    # 원문 보기, 연결 해제 버튼은 아이콘만 있고 설명 텍스트가 없어야 함
    assert row.read_btn.text() == ""
    assert row.read_btn.toolTip() == "원문 보기"
    assert row.detach_btn is not None
    assert row.detach_btn.text() == ""
    assert row.detach_btn.toolTip() == "연결 해제"

    # 원문 보기 아이콘 클릭 -> 원문 판으로 전환
    row.read_btn.click()
    assert sources.stack.currentIndex() == 1 and sources.reader_doc_id == workspace.docs[0]
    # '자료 목록'은 아이콘만 표시(텍스트 없음), '원본'은 아이콘 + 원본 텍스트
    assert sources.back_button.text() == ""
    assert sources.back_button.toolTip() == "자료 목록"
    assert not sources.back_button.icon().isNull()
    assert sources.open_button.text() == "원본"
    assert not sources.open_button.icon().isNull()
    sources.repaint_icons()
    # '자료 목록' 아이콘 클릭 -> 목록으로 복귀
    sources.back_button.click()
    assert sources.stack.currentIndex() == 0

    # 연결 해제 아이콘 클릭 -> 문서가 업무노트에서 제거됨
    row.detach_btn.click()
    assert sources.source_list.count() == 0
    assert notebook.sources(db, book) == []

    # 보관된 업무노트(editable=False)에서는 연결 해제 버튼이 생성되지 않음
    notebook.add_sources(db, book, workspace.docs[:1])
    notebook.archive(db, book, True)
    view.refresh()
    item2 = sources.source_list.item(0)
    row2 = sources.source_list.itemWidget(item2)
    assert row2.detach_btn is None

