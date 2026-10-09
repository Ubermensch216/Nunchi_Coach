"""개인 로컬 노트북. 원문 링크·선택 범위·파생 근거를 별도로 보존한다."""

import hashlib
import json
import re
from dataclasses import asdict
from datetime import date
from pathlib import Path

from ..search.query import QueryScope
from ..search.rag import ask


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def require(db, notebook_id, active=True):
    row = db.con.execute("SELECT * FROM notebooks WHERE id=?", (notebook_id,)).fetchone()
    if row is None or (active and row["archived"]):
        raise ValueError("열려 있는 노트북을 선택하세요.")
    return row


def create(db, title, purpose="", task_id=None):
    if not title.strip():
        raise ValueError("노트북 이름을 입력하세요.")
    with db.transaction():
        key = db.con.execute("INSERT INTO notebooks(title,purpose,task_id) VALUES (?,?,?)",
                             (title.strip(), purpose, task_id)).lastrowid
        db.audit("notebook.create", str(key))
    return key


def archive(db, notebook_id, archived=True):
    require(db, notebook_id, active=False)
    db.con.execute("UPDATE notebooks SET archived=? WHERE id=?", (int(archived), notebook_id))
    db.audit("notebook.archive" if archived else "notebook.restore", str(notebook_id))


def rename(db, notebook_id, title):
    require(db, notebook_id)
    if not title.strip():
        raise ValueError("노트북 이름을 입력하세요.")
    db.con.execute("UPDATE notebooks SET title=? WHERE id=?", (title.strip(), notebook_id))
    db.audit("notebook.rename", str(notebook_id))


def add_sources(db, notebook_id, doc_ids):
    require(db, notebook_id)
    with db.transaction():
        for doc_id in set(doc_ids):
            row = db.document(doc_id)
            if row is None or not row["hash"]:
                raise ValueError("본문과 해시를 분석한 문서만 추가할 수 있습니다.")
            db.con.execute("INSERT OR IGNORE INTO notebook_sources(notebook_id,doc_id,version_hash) "
                           "VALUES (?,?,?)", (notebook_id, doc_id, row["hash"]))
        db.audit("notebook.sources.add", str(notebook_id), detail=str(len(set(doc_ids))))


def sources(db, notebook_id):
    require(db, notebook_id, active=False)
    return db.con.execute("SELECT ns.*,d.filename,d.path,d.hash,d.missing_since,d.parse_status "
                          "FROM notebook_sources ns JOIN documents d ON d.id=ns.doc_id "
                          "WHERE ns.notebook_id=? ORDER BY d.filename,d.id", (notebook_id,)).fetchall()


def select(db, notebook_id, doc_id, selected):
    require(db, notebook_id)
    db.con.execute("UPDATE notebook_sources SET selected=? WHERE notebook_id=? AND doc_id=?",
                   (int(selected), notebook_id, doc_id))


def detach(db, notebook_id, doc_id):
    require(db, notebook_id)
    db.con.execute("DELETE FROM notebook_sources WHERE notebook_id=? AND doc_id=?", (notebook_id, doc_id))
    refresh_states(db)
    db.audit("notebook.source.detach", f"{notebook_id}/{doc_id}")


def accept_version(db, notebook_id, doc_id):
    require(db, notebook_id)
    with db.transaction():
        refresh_states(db)
        db.con.execute("UPDATE notebook_sources SET version_hash=(SELECT hash FROM documents WHERE id=?) "
                       "WHERE notebook_id=? AND doc_id=?", (doc_id, notebook_id, doc_id))
        db.audit("notebook.source.version", f"{notebook_id}/{doc_id}")


def selected_snapshot(db, notebook_id):
    require(db, notebook_id)
    return {str(row["doc_id"]): row["version_hash"] for row in sources(db, notebook_id)
            if row["selected"] and row["allowed"] and not row["missing_since"]
            and row["hash"] == row["version_hash"] and row["parse_status"] in ("ok", "partial")
            and file_matches(row["path"], row["version_hash"])}


def file_matches(path, digest):
    try:
        with Path(path).open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest() == digest
    except OSError:
        return False


def query(db, notebook_id, question, client=None):
    snapshot = selected_snapshot(db, notebook_id)
    effective = question.strip()
    if not effective:
        raise ValueError("질문을 입력하세요.")
    # 답변은 독립 근거로 다시 넣지 않는다. 같은 소스 선택에서만 직전 질문을 문맥으로 쓴다.
    previous = db.con.execute("SELECT * FROM notebook_messages WHERE notebook_id=? ORDER BY id DESC LIMIT 1",
                              (notebook_id,)).fetchone()
    if previous and json.loads(previous["source_snapshot"]) == snapshot and re.search(r"그것|그때|이 업무|이 사업|그 업무|그 사업", effective):
        effective = previous["question"] + "\n후속 질문: " + effective
    answer = ask(db, effective, client=client, scope=QueryScope(doc_ids=[int(k) for k in snapshot]))
    if getattr(client, "cancelled", False):
        raise ValueError("질문을 취소했습니다.")
    answer.question = question.strip()
    if selected_snapshot(db, notebook_id) != snapshot:
        raise ValueError("질문 중 자료 선택이나 판본이 바뀌었습니다. 다시 질문하세요.")
    citations = []
    for citation in answer.citations:
        if str(citation.doc_id) not in snapshot:
            raise ValueError("선택 범위 밖의 인용이 발견됐습니다.")
        if citation.chunk_id is not None:
            row = db.con.execute("SELECT text FROM chunks WHERE id=? AND doc_id=? AND locator=?",
                                  (citation.chunk_id, citation.doc_id, citation.locator)).fetchone()
        else:
            row = db.con.execute("SELECT text FROM chunks WHERE doc_id=? AND locator=? ORDER BY id LIMIT 1",
                                  (citation.doc_id, citation.locator)).fetchone()
        if row:
            citations.append({**asdict(citation), "version_hash": snapshot[str(citation.doc_id)],
                              "exact_quote": row[0]})
    with db.transaction():
        message_id = db.con.execute("INSERT INTO notebook_messages(notebook_id,question,answer,source_snapshot,"
                                   "citations,generation,model,withheld) VALUES (?,?,?,?,?,?,?,?)",
                                   (notebook_id, answer.question, answer.text, dumps(snapshot), dumps(citations),
                                    dumps(answer.generation), answer.model, int(answer.withheld))).lastrowid
        db.audit("notebook.query", f"{notebook_id}/{message_id}", result="withheld" if answer.withheld else "ok")
    return message_id, answer


def save_note(db, notebook_id, message_id, title):
    require(db, notebook_id)
    row = db.con.execute("SELECT * FROM notebook_messages WHERE id=? AND notebook_id=?",
                          (message_id, notebook_id)).fetchone()
    if row is None or row["withheld"]:
        raise ValueError("답변을 생성한 뒤 노트로 저장하세요. 검색 결과·보류는 답변 노트가 아닙니다.")
    with db.transaction():
        note_id = db.con.execute("INSERT INTO notebook_notes(notebook_id,message_id,title,body,evidence) VALUES (?,?,?,?,?)",
                                (notebook_id, message_id, title.strip() or row["question"], row["answer"], row["citations"])).lastrowid
        db.con.execute("INSERT INTO notebook_note_revisions(note_id,revision,body,reason) VALUES (?,1,?,?)",
                       (note_id, row["answer"], "답변 저장"))
        db.audit("notebook.note.save", str(note_id))
    return note_id


def edit_note(db, note_id, body, reason):
    row = db.con.execute("SELECT * FROM notebook_notes WHERE id=?", (note_id,)).fetchone()
    if row is None or not body.strip() or not reason.strip():
        raise ValueError("노트 내용과 수정 이유를 입력하세요.")
    require(db, row["notebook_id"])
    with db.transaction():
        revision = row["revision"] + 1
        db.con.execute("UPDATE notebook_notes SET body=?,revision=?,state='edited' WHERE id=?", (body, revision, note_id))
        db.con.execute("INSERT INTO notebook_note_revisions(note_id,revision,body,reason) VALUES (?,?,?,?)",
                       (note_id, revision, body, reason))
        db.con.execute("UPDATE knowledge_cards SET state='stale' WHERE note_id=? AND state!='revoked'", (note_id,))
        db.audit("notebook.note.edit", str(note_id))


def evidence_current(db, notebook_id, evidence, check_file=False):
    if not evidence:
        return False
    linked = {r["doc_id"]: r for r in sources(db, notebook_id)}
    for entry in evidence:
        row = linked.get(entry["doc_id"])
        if row is None or not row["allowed"] or row["missing_since"] or row["hash"] != entry["version_hash"]:
            return False
        if check_file:
            if not file_matches(row["path"], entry["version_hash"]):
                return False
        # 위치와 인용문을 같은 판본의 분석 결과에서 대조한다.
        if not db.con.execute("SELECT 1 FROM chunks WHERE doc_id=? AND locator=? AND text=?",
                              (entry["doc_id"], entry["locator"], entry["exact_quote"])).fetchone():
            return False
    return True


def promote(db, note_id, reviewer, applicability, valid_until=None):
    row = db.con.execute("SELECT * FROM notebook_notes WHERE id=?", (note_id,)).fetchone()
    if row is None or not reviewer.strip() or not applicability.strip():
        raise ValueError("검토자와 적용 조건을 입력하세요.")
    require(db, row["notebook_id"])
    if valid_until is not None and date.fromisoformat(valid_until) < date.today():
        raise ValueError("유효기간이 지났습니다.")
    if not evidence_current(db, row["notebook_id"], json.loads(row["evidence"]), check_file=True):
        raise ValueError("원문의 판본·위치·근거를 확인할 수 없습니다. 다시 검토하세요.")
    with db.transaction():
        card_id = db.con.execute("INSERT INTO knowledge_cards(note_id,note_revision,body,evidence,reviewer,applicability,valid_until) "
                                "VALUES (?,?,?,?,?,?,?)", (note_id, row["revision"], row["body"], row["evidence"],
                                 reviewer.strip(), applicability.strip(), valid_until)).lastrowid
        db.con.execute("UPDATE notebook_notes SET state='verified' WHERE id=?", (note_id,))
        db.audit("notebook.note.promote", str(card_id))
    return card_id


def refresh_states(db):
    for row in db.con.execute("SELECT c.*,n.notebook_id,n.revision FROM knowledge_cards c "
                              "JOIN notebook_notes n ON n.id=c.note_id WHERE c.state IN ('verified','published')").fetchall():
        expired = row["valid_until"] and row["valid_until"] < date.today().isoformat()
        if expired or row["note_revision"] != row["revision"] or not evidence_current(
                db, row["notebook_id"], json.loads(row["evidence"]), check_file=True):
            db.con.execute("UPDATE knowledge_cards SET state='stale' WHERE id=?", (row["id"],))
            db.con.execute("UPDATE notebook_notes SET state='stale' WHERE id=? AND state='verified'", (row["note_id"],))
            db.audit("notebook.knowledge.stale", str(row["id"]))


def publish(db, card_id, task_id):
    refresh_states(db)
    row = db.con.execute("SELECT c.*,n.notebook_id FROM knowledge_cards c JOIN notebook_notes n ON n.id=c.note_id WHERE c.id=?", (card_id,)).fetchone()
    if row is None or row["state"] != "verified":
        raise ValueError("검토를 마친 현재 판본의 지식만 게시할 수 있습니다.")
    require(db, row["notebook_id"])
    task = db.task(task_id)
    if task is None or task["not_a_task"] or task["merged_into"]:
        raise ValueError("사용 중인 업무를 선택하세요.")
    with db.transaction():
        db.con.execute("UPDATE knowledge_cards SET task_id=?,state='published',published_at=datetime('now') WHERE id=?",
                       (task_id, card_id))
        db.audit("notebook.knowledge.publish", f"{card_id}/{task_id}")


def published(db, task_id):
    refresh_states(db)
    return db.con.execute("SELECT c.* FROM knowledge_cards c JOIN notebook_notes n ON n.id=c.note_id "
                          "JOIN notebooks b ON b.id=n.notebook_id WHERE c.task_id=? AND c.state='published' "
                          "AND b.archived=0 ORDER BY c.id DESC", (task_id,)).fetchall()


def export_note(db, note_id, path):
    from .output_policy import safe_text_writer
    row = db.con.execute("SELECT * FROM notebook_notes WHERE id=?", (note_id,)).fetchone()
    if row is None:
        raise ValueError("노트를 선택하세요.")
    book = require(db, row["notebook_id"])
    proofs = json.loads(row["evidence"])
    if not evidence_current(db, row["notebook_id"], proofs, check_file=True):
        raise ValueError("원문 근거가 달라져 내보내기 전에 재검토가 필요합니다.")
    with safe_text_writer(path, db) as stream:
        stream.write(f"# {row['title']}\n\n노트북: {book['title']} · 개인 로컬 자료\n\n"
                     f"상태: {row['state']} · 수정판: {row['revision']} · AI 답변에서 작성한 연구 노트\n\n"
                     f"{row['body']}\n\n## 원천 근거\n\n")
        for i, proof in enumerate(proofs, 1):
            stream.write(f"{i}. {proof['filename']} · {proof['locator']} · SHA-256 `{proof['version_hash']}`\n\n"
                         f"원문: {proof['path']}\n\n{proof['exact_quote']}\n\n")
        stream.write("저장·승격·게시 상태는 공식 공문 승인이나 현재 적용 가능성을 보장하지 않습니다.\n")
        if not evidence_current(db, row["notebook_id"], proofs, check_file=True):
            raise ValueError("내보내는 동안 원문이 변경됐습니다. 다시 검토하세요.")
    db.audit("notebook.note.export", str(note_id))
