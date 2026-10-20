"""업무 매뉴얼을 업무노트 소스로 넣을 때의 판본 기록 (개편계획서 §6.3·§7).

판본은 '업무노트에 넣은 내용'이 달라질 때만 오른다. 같은 내용을 다시 넣거나 다른
업무노트에 넣으면 같은 판본을 가리킨다 — 동일 입력으로 같은 판본을 되풀이해 만들지
않는다. 실패한 시도는 판본을 올리지 않고 사유와 함께 남긴다. 옛 판의 글은 보관소
문서로 남아 있어(notebook_tasks.add_brief) 지난 답의 인용은 그대로 열린다.
"""
from __future__ import annotations

from . import notebook_tasks


def latest(db, task_id: int):
    """가장 최근에 성공한 판본 행. 없으면 None."""
    return db.con.execute(
        "SELECT * FROM manual_versions WHERE task_id=? AND status='ok' "
        "ORDER BY version DESC, id DESC LIMIT 1", (task_id,)).fetchone()


def history(db, task_id: int, limit: int = 20) -> list:
    return db.con.execute(
        "SELECT mv.*, nb.title AS notebook_title FROM manual_versions mv "
        "LEFT JOIN notebooks nb ON nb.id = mv.notebook_id "
        "WHERE mv.task_id=? ORDER BY mv.id DESC LIMIT ?", (task_id, limit)).fetchall()


def record(db, task_id: int, notebook_id: int | None, doc_id: int | None, digest: str) -> int:
    """성공한 반영을 기록하고 판본 번호를 돌려준다."""
    last = latest(db, task_id)
    version = last["version"] if last is not None and last["digest"] == digest \
        else (last["version"] + 1 if last is not None else 1)
    with db.transaction():
        db.con.execute(
            "INSERT INTO manual_versions(task_id, version, digest, notebook_id, doc_id, status) "
            "VALUES (?,?,?,?,?, 'ok')", (task_id, version, digest, notebook_id, doc_id))
    return version


def record_failure(db, task_id: int, notebook_id: int | None, note: str) -> None:
    last = latest(db, task_id)
    with db.transaction():
        db.con.execute(
            "INSERT INTO manual_versions(task_id, version, digest, notebook_id, status, note) "
            "VALUES (?,?,?,?, 'failed', ?)",
            (task_id, last["version"] if last is not None else 0,
             last["digest"] if last is not None else "", notebook_id, note[:300]))


def current_state(db, task_id: int) -> dict:
    """화면이 보여 줄 한 줄 — 마지막 판, 현재 내용과 같은지, 마지막 실패."""
    last = latest(db, task_id)
    digest = notebook_tasks.brief_digest(db, task_id)
    failed = db.con.execute(
        "SELECT note, created_at FROM manual_versions WHERE task_id=? AND status='failed' "
        "AND id > COALESCE((SELECT MAX(id) FROM manual_versions WHERE task_id=? AND status='ok'), 0) "
        "ORDER BY id DESC LIMIT 1", (task_id, task_id)).fetchone()
    return {"version": last["version"] if last is not None else 0,
            "created_at": last["created_at"] if last is not None else None,
            "current": last is not None and last["digest"] == digest,
            "failure": failed["note"] if failed is not None else ""}
