"""구조 투영 예약. lease 작업자·종속 파티션 전파는 P3에서 제공한다."""
from ...db.knowledge import KnowledgeStore


def schedule_document(db, doc_id: int, reason: str = "notebook_ingest") -> None:
    KnowledgeStore(db).schedule(f"document:{doc_id}", reason)


def schedule_tasks(db, reason: str = "restored") -> None:
    store = KnowledgeStore(db)
    with db.transaction():
        for row in db.con.execute("SELECT id FROM tasks"):
            store.schedule(f"task:{row[0]}", reason)
