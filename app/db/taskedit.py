"""업무 편집: 문서와 복원 기록을 같은 트랜잭션으로 옮긴다."""

from ..core import handover_release


TABLES = {"item": "task_items", "step": "work_steps", "gap": "info_gaps"}


def live(db, task_id):
    row = db.task(task_id)
    return row is not None and not row["not_a_task"] and row["merged_into"] is None


def plan(db, task_id, doc_ids=None):
    """연결된 항목·단계·공백을 함께 다룬다. 양쪽 문서에 걸친 묶음은 남긴다."""
    nodes = {(kind, row["id"]): dict(row) for kind, table in TABLES.items()
             for row in db.con.execute(f"SELECT * FROM {table} WHERE task_id=?", (task_id,))}
    edges = {key: set() for key in nodes}
    for key, row in nodes.items():
        refs = []
        if key[0] == "step" and row["after_step_id"]:
            refs.append(("step", row["after_step_id"]))
        if key[0] == "gap":
            refs.extend((kind, row[column]) for kind, column in
                        (("item", "item_id"), ("step", "step_id")) if row[column])
        for ref in refs:
            if ref in nodes:
                edges[key].add(ref)
                edges[ref].add(key)
    move, recheck, visited = set(), set(), set()
    chosen = set(doc_ids or [])
    for key in nodes:
        if key in visited:
            continue
        component, queue = set(), [key]
        while queue:
            current = queue.pop()
            if current in component:
                continue
            component.add(current)
            queue.extend(edges[current] - component)
        visited |= component
        proofs = set()
        unknown = False
        for kind, owner_id in component:
            for row in db.con.execute(
                    "SELECT doc_id FROM evidence WHERE owner_kind=? AND owner_id=?",
                    (kind, owner_id)):
                if row[0] is None:
                    unknown = True
                else:
                    proofs.add(row[0])
            if kind == "step":
                precedent = nodes[(kind, owner_id)]["task_step_id"]
                if precedent:
                    row = db.con.execute("SELECT doc_id FROM task_steps WHERE id=?", (precedent,)).fetchone()
                    if row and row[0]:
                        proofs.add(row[0])
        if doc_ids is None or (proofs and not unknown and proofs <= chosen):
            move |= component
        elif proofs & chosen:
            recheck |= component
    return {"move": move, "recheck": recheck, "nodes": nodes}


def preview(db, task_id, doc_ids=None):
    result = plan(db, task_id, doc_ids)
    return {**{kind: sum(key[0] == kind for key in result["move"]) for kind in TABLES},
            "recheck": len(result["recheck"])}


def transfer(db, source, target, decision):
    for kind, owner_id in sorted(decision["move"]):
        table = TABLES[kind]
        row = decision["nodes"][(kind, owner_id)]
        column = "gap_key" if kind == "gap" else "slot"
        slot = row[column]
        # 충돌한 값을 덮어쓰지 않고 ID·근거·검토·수행 기록을 모두 보존한다.
        clause, args = (" AND kind=?", [row["kind"]]) if kind == "item" else ("", [])
        while db.con.execute(f"SELECT 1 FROM {table} WHERE task_id=? AND {column}=?{clause}",
                             (target, slot, *args)).fetchone():
            slot += f"~{source}:{owner_id}"
        db.con.execute(f"UPDATE {table} SET task_id=?, {column}=?, origin='user' WHERE id=?",
                       (target, slot, owner_id))
    for kind, owner_id in decision["recheck"]:
        if kind in ("item", "step"):
            db.con.execute(f"UPDATE {TABLES[kind]} SET human_state='recheck', origin='user' WHERE id=?",
                           (owner_id,))
    # 과거 사례 ID를 보존해 이동한 수행 단계에서 계속 참조한다.
    for kind, owner_id in decision["move"]:
        if kind == "step" and decision["nodes"][(kind, owner_id)]["task_step_id"]:
            db.con.execute("UPDATE task_steps SET task_id=?, decided_by='user' WHERE id=?",
                           (target, decision["nodes"][(kind, owner_id)]["task_step_id"]))


def invalidate(db, *task_ids):
    for task_id in task_ids:
        db.con.execute("DELETE FROM task_health WHERE task_id=?", (task_id,))
    db.con.execute("DELETE FROM meta WHERE key IN ('cycles_checked','steps_checked','items_checked')")
    db.set_meta("content_revision", str(int(db.get_meta("content_revision") or 0) + 1))


def merge(db, source, target):
    if source == target or not live(db, source) or not live(db, target):
        return False
    with db.transaction():
        handover_release.save(db, label="업무 합치기 전")
        transfer(db, source, target, plan(db, source))
        db.con.execute(
            "INSERT OR IGNORE INTO task_docs(task_id,doc_id,origin,confidence,evidence,round_key) "
            "SELECT ?,doc_id,'user',confidence,evidence,round_key FROM task_docs WHERE task_id=?", (target, source))
        db.con.execute("UPDATE task_steps SET task_id=?,decided_by='user' WHERE task_id=?", (target, source))
        db.con.execute("UPDATE task_cycles SET task_id=?,decided_by='user' WHERE task_id=?", (target, source))
        db.con.execute("UPDATE knowledge_cards SET task_id=? WHERE task_id=?", (target, source))
        db.con.execute("UPDATE notebooks SET task_id=? WHERE task_id=?", (target, source))
        db.con.execute(
            "INSERT OR IGNORE INTO task_reading SELECT ?,doc_id,ordinal,score,reason "
            "FROM task_reading WHERE task_id=?", (target, source))
        for table in ("task_docs", "task_reading"):
            db.con.execute(f"DELETE FROM {table} WHERE task_id=?", (source,))
        db.con.execute("UPDATE tasks SET merged_into=?,review_state='confirmed',origin='user',"
                       "reviewed_at=datetime('now') WHERE id=?", (target, source))
        db.con.execute("UPDATE tasks SET status='edited',origin='user',review_state='confirmed',"
                       "reviewed_at=datetime('now') WHERE id=?", (target,))
        db._record("tasks", source, "merged_into", None, target)
        db.audit("task.merge", f"{source}→{target}")
        invalidate(db, source, target)
        check(db)
    return True


def split(db, source, doc_ids, name):
    name = name.strip()
    chosen = set(doc_ids)
    if not name or not chosen or not live(db, source):
        return None
    existing = {row[0] for row in db.con.execute("SELECT doc_id FROM task_docs WHERE task_id=?", (source,))}
    if not chosen < existing or db.con.execute("SELECT 1 FROM tasks WHERE name=?", (name,)).fetchone():
        return None
    with db.transaction():
        handover_release.save(db, label="업무 나누기 전")
        decision = plan(db, source, chosen)
        target = db.con.execute("INSERT INTO tasks(name,origin,status,confidence,review_state,reviewed_at) "
                                "VALUES (?,'user','edited','high','confirmed',datetime('now'))", (name,)).lastrowid
        transfer(db, source, target, decision)
        marks = ",".join("?" for _ in chosen)
        args = (target, source, *sorted(chosen))
        db.con.execute("INSERT INTO task_docs(task_id,doc_id,origin,confidence,evidence,is_primary,round_key) "
                       f"SELECT ?,doc_id,'user',confidence,evidence,is_primary,round_key "
                       f"FROM task_docs WHERE task_id=? AND doc_id IN ({marks})", args)
        db.con.execute(f"INSERT INTO task_reading SELECT ?,doc_id,ordinal,score,reason "
                       f"FROM task_reading WHERE task_id=? AND doc_id IN ({marks})", args)
        for table in ("task_docs", "task_reading"):
            db.con.execute(f"DELETE FROM {table} WHERE task_id=? AND doc_id IN ({marks})",
                           (source, *sorted(chosen)))
        db._record("tasks", source, "split", source, target)
        db.audit("task.split", f"{source}→{target}", f"{len(chosen)}건")
        invalidate(db, source, target)
        check(db)
    return target


def check(db):
    if db.con.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("업무 편집 뒤 참조 정합성을 확인하지 못했습니다.")
    for kind, table in (("item", "task_items"), ("step", "work_steps")):
        if db.con.execute(f"SELECT 1 FROM reviews r LEFT JOIN {table} t ON t.id=r.target_id "
                          "WHERE r.target_kind=? AND t.id IS NULL LIMIT 1", (kind,)).fetchone():
            raise ValueError("검토 기록의 대상이 없습니다.")
