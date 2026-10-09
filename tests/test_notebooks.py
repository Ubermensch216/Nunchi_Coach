import hashlib
import json
from types import SimpleNamespace

import pytest

from app.core import notebook
from app.db import Database
from app.search.rag import Answer,Citation,ask
from app.search.query import QueryScope


class Offline:
    def health(self):
        return SimpleNamespace(embedding_ready=False, message="offline")

    def embed(self,*_):
        raise AssertionError("검색 전용 모드는 모델을 호출하면 안 됩니다")


@pytest.fixture
def db(tmp_path):
    db=Database(tmp_path / "project.db");db.init()
    source=db.add_source(tmp_path)
    for i,text in enumerate(("예산요구액: 300만원", "예산요구액: 999만원"),1):
        path=tmp_path / f"예산{i}.txt";path.write_text(text,encoding="utf-8")
        doc=db.upsert_document(source,{"path":str(path),"filename":path.name,"ext":".txt","hash":hashlib.sha256(path.read_bytes()).hexdigest(),"parse_status":"ok","eff_year":2026})
        db.replace_document_chunks(doc,[(1,"1문단",text)])
    db.con.execute("INSERT INTO tasks(id,name) VALUES (1,'예산 업무')")
    yield db
    db.close()


def setup(db):
    key=notebook.create(db,"예산 연구")
    notebook.add_sources(db,key,[1,2])
    return key


def generated(db,monkeypatch):
    def fake(db,question,client,scope):
        assert scope.doc_ids==[1]
        return Answer(question,"예산요구액은 300만원입니다. [1]",False,
                      citations=[Citation(1,1,"예산1.txt","1문단",db.document(1)["path"])],model="fake")
    monkeypatch.setattr(notebook,"ask",fake)
    key=setup(db);notebook.select(db,key,2,False)
    message,_=notebook.query(db,key,"예산요구액은 얼마야?")
    return key,notebook.save_note(db,key,message,"예산 근거")


def test_offline_search_is_not_an_answer_and_never_leaks_other_sources(db):
    result=ask(db,"예산요구액",Offline(),QueryScope(doc_ids=[1]))
    assert result.withheld and not result.error
    assert result.generation["mode"]=="search_only"
    assert result.citations and {c.doc_id for c in result.citations}=={1}
    assert "999" not in " ".join(c.snippet for c in result.citations)
    assert not ask(db,"예산요구액",Offline(),QueryScope(doc_ids=[])).citations


def test_each_turn_uses_current_selection_and_history_cannot_reintroduce_removed_source(db,monkeypatch):
    key=setup(db)
    _,first=notebook.query(db,key,"예산요구액",Offline())
    assert {c.doc_id for c in first.citations}=={1,2}
    notebook.select(db,key,2,False)
    _,second=notebook.query(db,key,"그 사업 예산요구액",Offline())
    assert {c.doc_id for c in second.citations}=={1}
    snapshots=[json.loads(r[0]) for r in db.con.execute("SELECT source_snapshot FROM notebook_messages ORDER BY id")]
    assert set(snapshots[0])=={"1","2"} and set(snapshots[1])=={"1"}


def test_ai_note_needs_human_review_before_publish_and_preserves_lineage(db,monkeypatch):
    key,note=generated(db,monkeypatch)
    row=db.con.execute("SELECT * FROM notebook_notes WHERE id=?",(note,)).fetchone()
    assert row["state"]=="ai_draft"
    proofs=json.loads(row["evidence"])
    assert proofs[0]["version_hash"]==db.document(1)["hash"]
    assert proofs[0]["exact_quote"]=="예산요구액: 300만원"
    with pytest.raises(ValueError):notebook.promote(db,note,"", "2026년 예산")
    card=notebook.promote(db,note,"후임자","2026년도 요구액에만 적용")
    notebook.publish(db,card,1)
    assert notebook.published(db,1)[0]["id"]==card
    assert db.task_items(1)==[]
    assert key


def test_edit_invalidates_published_card_and_records_revision(db,monkeypatch):
    _,note=generated(db,monkeypatch)
    card=notebook.promote(db,note,"검토자","요구액");notebook.publish(db,card,1)
    notebook.edit_note(db,note,"수정한 요약","표현 수정")
    assert not notebook.published(db,1)
    assert db.con.execute("SELECT COUNT(*) FROM notebook_note_revisions WHERE note_id=?",(note,)).fetchone()[0]==2
    with pytest.raises(ValueError):notebook.publish(db,card,1)


def test_physical_source_change_invalidates_card_even_before_reanalysis(db,monkeypatch):
    _,note=generated(db,monkeypatch)
    card=notebook.promote(db,note,"검토자","요구액");notebook.publish(db,card,1)
    from pathlib import Path
    Path(db.document(1)["path"]).write_text("예산요구액: 500만원",encoding="utf-8")
    assert not notebook.published(db,1)
    with pytest.raises(ValueError):notebook.promote(db,note,"검토자","요구액")


def test_detach_and_archive_do_not_delete_originals_and_hide_knowledge(db,monkeypatch):
    key,note=generated(db,monkeypatch)
    card=notebook.promote(db,note,"검토자","요구액");notebook.publish(db,card,1)
    notebook.archive(db,key)
    assert not notebook.published(db,1)
    notebook.archive(db,key,False);notebook.detach(db,key,1)
    assert not notebook.published(db,1)
    assert db.document(1) is not None
    assert db.con.execute("SELECT COUNT(*) FROM chunks WHERE doc_id=1").fetchone()[0]==1


def test_model_off_search_cannot_be_promoted_as_ai_answer(db):
    key=setup(db);message,_=notebook.query(db,key,"예산요구액",Offline())
    with pytest.raises(ValueError):notebook.save_note(db,key,message,"검색 결과")


def test_source_change_during_generation_discards_result(db,monkeypatch):
    key=setup(db)
    def changing(*args,**kwargs):
        notebook.select(db,key,2,False)
        return Answer("질문","답변",False)
    monkeypatch.setattr(notebook,"ask",changing)
    with pytest.raises(ValueError,match="바뀌었습니다"):
        notebook.query(db,key,"예산")
    assert db.con.execute("SELECT COUNT(*) FROM notebook_messages").fetchone()[0]==0


def test_online_generation_receives_only_selected_original(db):
    from app.search.vector import pack
    for row in db.con.execute("SELECT id FROM chunks"):
        db.save_chunk_embedding(row[0],"test",3,pack([1,0,0]))
    class Online:
        embed_model="test"
        gen_model="test"
        def health(self):return SimpleNamespace(embedding_ready=True)
        def embed(self,text):return [[1,0,0]],None
        def generate_json(self,prompt,*args,**kwargs):
            assert "999" not in prompt
            assert "300" in prompt
            return {"answered":True,"sentences":[{"text":"예산요구액은 300만원이다.","sources":[1]}]},None,""
    result=ask(db,"예산요구액은 얼마야?",Online(),QueryScope(doc_ids=[1]))
    assert not result.withheld
    assert {c.doc_id for c in result.citations}=={1}


def test_markdown_export_has_lineage_and_cannot_overwrite_original(db,monkeypatch,tmp_path):
    _,note=generated(db,monkeypatch)
    target=tmp_path.parent / (tmp_path.name + "-note.md")
    notebook.export_note(db,note,target)
    text=target.read_text(encoding="utf-8")
    assert "ai_draft" in text and db.document(1)["hash"] in text
    before=(tmp_path / "예산1.txt").read_bytes()
    with pytest.raises(ValueError):notebook.export_note(db,note,tmp_path / "예산1.txt")
    assert (tmp_path / "예산1.txt").read_bytes()==before


def test_v5_upgrade_keeps_existing_data_and_creates_backup(db):
    db.set_meta("schema_version","5")
    db.init()
    assert db.get_meta("schema_version")=="7"
    assert db.path.with_name("project.db.bak-v5").is_file()
    assert db.document(1)["filename"]=="예산1.txt"
    assert notebook.create(db,"승격 후 노트북")


def test_long_passage_with_same_locator_keeps_the_cited_chunk(db,monkeypatch):
    chunks=db.replace_document_chunks(1,[(1,"긴 문단","첫 조각"),(2,"긴 문단","둘째 조각의 근거")])
    def fake(db,question,client,scope):
        return Answer(question,"둘째 조각의 근거 [1]",False,
                      citations=[Citation(1,1,"예산1.txt","긴 문단",db.document(1)["path"],chunk_id=chunks[1][0])])
    monkeypatch.setattr(notebook,"ask",fake)
    key=setup(db)
    message,_=notebook.query(db,key,"둘째 조각")
    proofs=json.loads(db.con.execute("SELECT citations FROM notebook_messages WHERE id=?",(message,)).fetchone()[0])
    assert proofs[0]["exact_quote"]=="둘째 조각의 근거"
    assert proofs[0]["chunk_id"]==chunks[1][0]
