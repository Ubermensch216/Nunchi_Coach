"""개인 로컬 노트북. 원문 링크·선택 범위·파생 근거를 별도로 보존한다."""

import hashlib
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

from ..search import expand
from ..search.overview import asks_whole, automatic
from ..search.query import QueryScope
from ..search.rag import ask


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def require(db, notebook_id, active=True):
    row = db.con.execute("SELECT * FROM notebooks WHERE id=?", (notebook_id,)).fetchone()
    if row is None or (active and row["archived"]):
        raise ValueError("열려 있는 노트북을 선택하세요.")
    return row


# 카드 표지의 색 이름. 화면(ui/views/notebooks/home.py)이 실제 색으로 옮긴다.
# 상태 색(빨강·주황·초록)과 겹치는 이름은 두지 않는다(widgets/task_card.py와 같은 이유).
ICONS = ("blue", "indigo", "violet", "teal", "slate", "plum")
DEFAULT_TITLE = "제목 없는 노트북"


def untitled(db):
    """이름을 묻지 않고 만들 때 쓰는 기본 이름. 이미 있으면 번호를 붙인다."""
    taken = {row["title"] for row in db.con.execute("SELECT title FROM notebooks")}
    title, number = DEFAULT_TITLE, 1
    while title in taken:
        number += 1
        title = f"{DEFAULT_TITLE} {number}"
    return title


def create(db, title, purpose="", task_id=None, icon=""):
    if not title.strip():
        raise ValueError("노트북 이름을 입력하세요.")
    if icon not in ICONS:
        icon = ICONS[sum(ord(c) for c in title.strip()) % len(ICONS)]
    with db.transaction():
        key = db.con.execute("INSERT INTO notebooks(title,purpose,task_id,icon,updated_at) "
                             "VALUES (?,?,?,?,datetime('now'))",
                             (title.strip(), purpose, task_id, icon)).lastrowid
        db.audit("notebook.create", str(key))
    return key


def overview(db, include_archived=False):
    """홈 화면의 카드 한 장에 필요한 것 — 이름·표지·자료 수·마지막으로 쓴 때."""
    return db.con.execute(
        "SELECT b.*, COALESCE(b.updated_at, b.created_at) AS touched_at, "
        "(SELECT COUNT(*) FROM notebook_sources s WHERE s.notebook_id=b.id) AS source_count "
        "FROM notebooks b WHERE (? OR b.archived=0) "
        "ORDER BY COALESCE(b.last_opened_at, b.updated_at, b.created_at) DESC, b.id DESC",
        (int(include_archived),)).fetchall()


def touch(db, notebook_id, opened=False):
    column = "last_opened_at" if opened else "updated_at"
    db.con.execute(f"UPDATE notebooks SET {column}=datetime('now') WHERE id=?", (notebook_id,))


def set_icon(db, notebook_id, icon):
    require(db, notebook_id, active=False)
    if icon not in ICONS:
        raise ValueError("표지 색을 선택하세요.")
    db.con.execute("UPDATE notebooks SET icon=? WHERE id=?", (icon, notebook_id))


def layout(db, notebook_id):
    row = require(db, notebook_id, active=False)
    try:
        value = json.loads(row["layout"] or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def save_layout(db, notebook_id, value):
    db.con.execute("UPDATE notebooks SET layout=? WHERE id=?", (dumps(value), notebook_id))


def duplicate(db, notebook_id):
    """자료 연결과 선택 상태만 옮긴다. 대화·메모·검토 기록은 원래 노트북의 것이다."""
    row = require(db, notebook_id, active=False)
    with db.transaction():
        key = db.con.execute("INSERT INTO notebooks(title,purpose,task_id,icon,updated_at) "
                             "VALUES (?,?,?,?,datetime('now'))",
                             (row["title"] + " 사본", row["purpose"], row["task_id"], row["icon"])).lastrowid
        db.con.execute("INSERT INTO notebook_sources(notebook_id,doc_id,version_hash,selected,allowed,origin,added_at,task_id) "
                       "SELECT ?,doc_id,version_hash,selected,allowed,origin,datetime('now'),task_id "
                       "FROM notebook_sources WHERE notebook_id=?", (key, notebook_id))
        db.audit("notebook.duplicate", f"{notebook_id}/{key}")
    return key


def delete(db, notebook_id):
    """노트북과 그 대화·메모·검토 지식을 지운다. 연결한 원문과 분석 결과는 남는다."""
    require(db, notebook_id, active=False)
    with db.transaction():
        db.con.execute("DELETE FROM notebooks WHERE id=?", (notebook_id,))
        db.audit("notebook.delete", str(notebook_id))


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


def add_sources(db, notebook_id, doc_ids, origin="project"):
    require(db, notebook_id)
    with db.transaction():
        for doc_id in set(doc_ids):
            row = db.document(doc_id)
            if row is None or not row["hash"]:
                raise ValueError("본문과 해시를 분석한 문서만 추가할 수 있습니다.")
            db.con.execute("INSERT OR IGNORE INTO notebook_sources(notebook_id,doc_id,version_hash,origin,added_at) "
                           "VALUES (?,?,?,?,datetime('now'))", (notebook_id, doc_id, row["hash"], origin))
        touch(db, notebook_id)
        db.audit("notebook.sources.add", str(notebook_id), detail=str(len(set(doc_ids))))


def sources(db, notebook_id):
    require(db, notebook_id, active=False)
    return db.con.execute("SELECT ns.*,d.filename,d.path,d.hash,d.missing_since,d.parse_status,"
                          "d.parse_note,d.parse_error,d.ext "
                          "FROM notebook_sources ns JOIN documents d ON d.id=ns.doc_id "
                          "WHERE ns.notebook_id=? ORDER BY d.filename,d.id", (notebook_id,)).fetchall()


def select(db, notebook_id, doc_id, selected):
    require(db, notebook_id)
    db.con.execute("UPDATE notebook_sources SET selected=? WHERE notebook_id=? AND doc_id=?",
                   (int(selected), notebook_id, doc_id))


def select_all(db, notebook_id, selected):
    require(db, notebook_id)
    db.con.execute("UPDATE notebook_sources SET selected=? WHERE notebook_id=?",
                   (int(selected), notebook_id))


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


def _can_generate(client) -> bool:
    return client is not None and bool(getattr(client.health(), "generation_ready", False))


def _law_lookup(db, question, client, answer):
    """현행 조문 조회. 켜 두지 않았으면 아무것도 하지 않는다 — 밖으로 나가는 통신이다."""
    from ..law import refs, service
    if not service.available(db):
        return {"text": "", "citations": [],
                "problems": ["법령 조회가 꺼져 있습니다. 설정 → 시스템에서 켜고 인증키를 입력하세요."]}
    # 자료가 인용한 조문도 현행 본문으로 확인한다. 보내는 것은 법령 이름과 조문 번호뿐이다.
    quoted = " ".join([answer.text] + [c.snippet for c in answer.citations])
    return service.answer(db, question, client, cited_in_documents=refs.extract(quoted)[:2])


def effective_mode(db, notebook_id, question, mode="", generating=True):
    """이 질문을 실제로 받을 방식. 주제 없는 요약 요청은 자료가 적으면 전체 정리로 받는다."""
    if mode or not generating:
        return mode
    doc_ids = [int(k) for k in selected_snapshot(db, notebook_id)]
    return "overview" if automatic(db, question, doc_ids) else ""


def query(db, notebook_id, question, client=None, mode="", law=False):
    """노트북의 한 차례 대화.

    mode="overview"면 선택한 자료 전체를 훑어 정리한다. law=True면 현행 법령을
    함께 조회해 따로 답한다(app/law) — 자료 답변과 섞지 않는다.
    """
    snapshot = selected_snapshot(db, notebook_id)
    effective = question.strip()
    if not effective:
        raise ValueError("질문을 입력하세요.")
    # 답변은 독립 근거로 다시 넣지 않는다. 같은 소스 선택의 **앞선 질문만** 문맥으로 쓴다.
    earlier = [row["question"] for row in db.con.execute(
        "SELECT question, source_snapshot FROM notebook_messages WHERE notebook_id=? ORDER BY id DESC LIMIT ?",
        (notebook_id, expand.HISTORY_TURNS)) if json.loads(row["source_snapshot"]) == snapshot][::-1]
    generating = _can_generate(client)
    rewritten = ""
    if earlier and expand.needs_context(effective):
        if generating:
            rewritten = expand.standalone(client, effective, earlier)
        effective = rewritten or earlier[-1] + "\n후속 질문: " + effective
    scope = QueryScope(doc_ids=[int(k) for k in snapshot])
    mode = effective_mode(db, notebook_id, question, mode, generating)
    if mode == "overview":
        from ..search.overview import overview
        answer = overview(db, effective, client, scope)
    elif generating and db.get_meta("notebook_query_expansion") != "0":
        answer = ask(db, effective, client=client, scope=scope,
                     expander=lambda text: expand.variants(client, text))
    else:
        answer = ask(db, effective, client=client, scope=scope)
    if getattr(client, "cancelled", False):
        raise ValueError("질문을 취소했습니다.")
    answer.question = question.strip()
    if answer.withheld and not answer.error and mode != "overview" and asks_whole(question):
        # 자료가 많아 보통 질문으로 받은 요약 요청이 보류됐다. 자료가 없는 것이 아니다.
        answer.guidance = ("자료 전체를 요약·정리하려면 입력창 옆 '전체 정리'를 켜고 다시 요청하세요. "
                           "특정 내용이 궁금하면 그 주제를 질문에 적어 주세요.")
        answer.generation["guidance"] = answer.guidance
    if selected_snapshot(db, notebook_id) != snapshot:
        raise ValueError("질문 중 자료 선택이나 판본이 바뀌었습니다. 다시 질문하세요.")
    # 법령 검색에는 사용자가 쓴 문장만 보낸다. 앞선 질문을 이어 붙인 문장은 보내지 않는다.
    statutes = _law_lookup(db, rewritten or question.strip(), client, answer) if law else {}
    if getattr(client, "cancelled", False):
        raise ValueError("질문을 취소했습니다.")
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
                                   "citations,generation,model,withheld,standalone_question,mode,law_citations) "
                                   "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                   (notebook_id, answer.question, answer.text, dumps(snapshot), dumps(citations),
                                    dumps(answer.generation), answer.model, int(answer.withheld),
                                    rewritten, mode or "rag", dumps(statutes))).lastrowid
        touch(db, notebook_id)
        db.audit("notebook.query", f"{notebook_id}/{message_id}", result="withheld" if answer.withheld else "ok")
    return message_id, answer


def messages(db, notebook_id):
    require(db, notebook_id, active=False)
    return db.con.execute("SELECT * FROM notebook_messages WHERE notebook_id=? ORDER BY id",
                          (notebook_id,)).fetchall()


def rate(db, message_id, rating):
    if rating not in ("right", "wrong", None):
        raise ValueError("평가 값을 확인하세요.")
    db.con.execute("UPDATE notebook_messages SET rating=? WHERE id=?", (rating, message_id))
    db.audit("notebook.message.rate", str(message_id), result=rating or "cleared")


def add_memo(db, notebook_id, title, body):
    """사람이 직접 적은 메모. 원문 근거가 없으므로 지식으로 승격할 수 없다."""
    require(db, notebook_id)
    if not title.strip() or not body.strip():
        raise ValueError("메모 제목과 내용을 입력하세요.")
    with db.transaction():
        note_id = db.con.execute("INSERT INTO notebook_notes(notebook_id,title,body,state,kind) "
                                 "VALUES (?,?,?,'memo','memo')", (notebook_id, title.strip(), body)).lastrowid
        db.con.execute("INSERT INTO notebook_note_revisions(note_id,revision,body,reason) VALUES (?,1,?,?)",
                       (note_id, body, "직접 작성"))
        touch(db, notebook_id)
        db.audit("notebook.memo.add", str(note_id))
    return note_id


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
        state = "memo" if row["kind"] == "memo" else "edited"
        db.con.execute("UPDATE notebook_notes SET body=?,revision=?,state=? WHERE id=?", (body, revision, state, note_id))
        db.con.execute("INSERT INTO notebook_note_revisions(note_id,revision,body,reason) VALUES (?,?,?,?)",
                       (note_id, revision, body, reason))
        db.con.execute("UPDATE knowledge_cards SET state='stale' WHERE note_id=? AND state!='revoked'", (note_id,))
        db.audit("notebook.note.edit", str(note_id))


def published_cards(db, note_id):
    """이 메모에서 승격해 업무 가이드에 올린 지식 카드 수."""
    return db.con.execute("SELECT COUNT(*) FROM knowledge_cards WHERE note_id=? AND published_at IS NOT NULL "
                          "AND state!='revoked'", (note_id,)).fetchone()[0]


def delete_note(db, note_id):
    """메모를 지운다. 고친 이력과, 이 메모에서 승격한 지식 카드도 함께 사라진다."""
    row = db.con.execute("SELECT notebook_id FROM notebook_notes WHERE id=?", (note_id,)).fetchone()
    if row is None:
        raise ValueError("메모를 선택하세요.")
    require(db, row["notebook_id"])
    with db.transaction():
        db.con.execute("DELETE FROM notebook_notes WHERE id=?", (note_id,))
        db.audit("notebook.note.delete", str(note_id))


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
    if row["kind"] == "memo":
        raise ValueError("직접 쓴 메모는 원문 근거가 없어 지식으로 승격할 수 없습니다.")
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
    memo = row["kind"] == "memo"
    if not memo and not evidence_current(db, row["notebook_id"], proofs, check_file=True):
        raise ValueError("원문 근거가 달라져 내보내기 전에 재검토가 필요합니다.")
    origin = "사람이 직접 쓴 메모 · 원문 근거 없음" if memo else "AI 답변에서 작성한 연구 노트"
    with safe_text_writer(path, db) as stream:
        stream.write(f"# {row['title']}\n\n노트북: {book['title']} · 개인 로컬 자료\n\n"
                     f"상태: {row['state']} · 수정판: {row['revision']} · {origin}\n\n"
                     f"{row['body']}\n\n## 원천 근거\n\n")
        for i, proof in enumerate(proofs, 1):
            stream.write(f"{i}. {proof['filename']} · {proof['locator']} · SHA-256 `{proof['version_hash']}`\n\n"
                         f"원문: {proof['path']}\n\n{proof['exact_quote']}\n\n")
        stream.write("저장·승격·게시 상태는 공식 공문 승인이나 현재 적용 가능성을 보장하지 않습니다.\n")
        if not memo and not evidence_current(db, row["notebook_id"], proofs, check_file=True):
            raise ValueError("내보내는 동안 원문이 변경됐습니다. 다시 검토하세요.")
    db.audit("notebook.note.export", str(note_id))
