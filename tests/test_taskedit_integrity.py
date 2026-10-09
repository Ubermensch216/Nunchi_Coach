import pytest

from app.db import Database


@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path / "project.db")
    db.init()
    source = db.add_source(tmp_path / "sources")
    for i in range(1, 4):
        db.upsert_document(source, {"path": str(tmp_path / f"doc{i}.txt"), "filename": f"doc{i}.txt", "ext": ".txt"})
    db.con.execute("INSERT INTO tasks(id,name) VALUES (1,'원 업무'),(2,'받는 업무')")
    db.con.execute("INSERT INTO task_docs(task_id,doc_id) VALUES (1,1),(1,2),(1,3),(2,3)")
    yield db
    db.close()


def records(db, task_id=1, slot="기한", docs=(1,)):
    item = db.con.execute("INSERT INTO task_items(task_id,kind,slot,content,human_state) VALUES (?,'deadline',?,'9월 30일','successor')", (task_id, slot)).lastrowid
    step = db.con.execute("INSERT INTO work_steps(task_id,slot,label) VALUES (?,?,'제출')", (task_id, slot)).lastrowid
    gap = db.con.execute("INSERT INTO info_gaps(task_id,item_id,step_id,kind) VALUES (?,?,?,'missing')", (task_id,item,step)).lastrowid
    for doc in docs:
        db.add_evidence("item",item,doc,None,"본문","기한",None,None)
    db.set_step_run(step,"done",result_path="outputs/result.txt",note="제출 완료")
    db.con.execute("INSERT INTO reviews(target_kind,target_id,action,reviewer_name) VALUES ('item',?,'confirm','검토자')",(item,))
    return item,step,gap


def test_merge_keeps_collision_ids_evidence_reviews_and_runs(db):
    item,step,gap=records(db)
    other,_,_=records(db,2)
    assert db.merge_tasks(1,2)
    assert db.con.execute("SELECT task_id FROM task_items WHERE id=?",(item,)).fetchone()[0]==2
    assert db.con.execute("SELECT content FROM task_items WHERE id=?",(other,)).fetchone()[0]=="9월 30일"
    assert db.con.execute("SELECT task_id FROM info_gaps WHERE id=?",(gap,)).fetchone()[0]==2
    assert db.con.execute("SELECT result_path FROM step_runs WHERE step_id=?",(step,)).fetchone()[0]=="outputs/result.txt"
    assert db.con.execute("SELECT COUNT(*) FROM evidence WHERE owner_id=? AND owner_kind='item'",(item,)).fetchone()[0]==1
    assert db.reviews("item",item)[0]["reviewer_name"]=="검토자"
    assert db.snapshots()
    assert db.con.execute("PRAGMA foreign_key_check").fetchall()==[]
    assert db.merge_tasks(1,2) is False


def test_split_moves_connected_records_and_keeps_mixed_records(db):
    item,step,gap=records(db,docs=(1,))
    mixed,_,_=records(db,slot="혼합",docs=(1,2))
    new=db.split_task(1,[1],"새 업무")
    assert new
    assert db.con.execute("SELECT task_id FROM work_steps WHERE id=?",(step,)).fetchone()[0]==new
    assert db.con.execute("SELECT task_id FROM info_gaps WHERE id=?",(gap,)).fetchone()[0]==new
    row=db.con.execute("SELECT * FROM task_items WHERE id=?",(mixed,)).fetchone()
    assert row["task_id"]==1 and row["human_state"]=="recheck"
    assert db.con.execute("SELECT state FROM step_runs WHERE step_id=?",(step,)).fetchone()[0]=="done"
    db.sync_task_items(new,[])
    assert db.con.execute("SELECT id FROM task_items WHERE id=?",(item,)).fetchone()


def test_failure_rolls_back_snapshot_documents_and_records(db):
    item,_,_=records(db)
    db.con.execute("CREATE TRIGGER fail_merge BEFORE UPDATE OF merged_into ON tasks BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(Exception,match="injected"):
        db.merge_tasks(1,2)
    assert not db.snapshots()
    assert db.con.execute("SELECT task_id FROM task_items WHERE id=?",(item,)).fetchone()[0]==1
    assert {d["id"] for d in db.task_documents(1)}=={1,2,3}


@pytest.mark.parametrize("docs", [[9], [1,9], [1,2,3]])
def test_split_rejects_invalid_or_empty_remaining_selection(db,docs):
    assert db.split_task(1,docs,"새 업무") is None
    assert not db.snapshots()
