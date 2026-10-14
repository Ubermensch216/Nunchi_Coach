"""새 자료가 업무에 미친 영향 — 다시 분석할 때 자동으로 읽은 값이 어떻게 바뀌었나 (개선계획서 P2).

새 공문이 들어오면 기한·제출처·절차가 바뀔 수 있다. 화면은 지금 값만 보여 주므로 "무엇이 바뀌었나"를
알 길이 없었다. 업무 복원이 값을 맞추기 전과 후를 견주어 남긴다.

  · 처음 분석할 때는 남기지 않는다 — 전부 '새로 생김'이라 아무 정보가 없다.
  · 자주 바뀌는 요약(마지막 기록)과 업무 사이의 관계 표시는 세지 않는다.
  · 사람이 고친 값은 대상이 아니다. 자동으로 읽은 값만 견준다.
"""

from __future__ import annotations

from datetime import datetime

from . import reconstruction as rc

_SKIP_KINDS = (rc.RECORD, rc.AGENCY, rc.NOTE)
# 바뀌면 다음 행동으로 알릴 종류 — 일정과 낼 곳과 하는 법.
WATCHED = (rc.DEADLINE, rc.RECIPIENT, rc.PROCEDURE, rc.PURPOSE)
_KEEP_RUNS = 5
LABEL = {"added": "새로 생김", "changed": "바뀜", "removed": "사라짐"}


def ready(db) -> bool:
    return db.con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'restore_changes'").fetchone() is not None


def values(db, task_id: int) -> dict[tuple[str, str], str]:
    """지금 저장된, 자동으로 읽은 업무 값 {(종류, 이름표): 값}."""
    marks = ", ".join("?" * len(_SKIP_KINDS))
    return {(row["kind"], row["slot"]): row["content"] for row in db.con.execute(
        f"SELECT kind, slot, content FROM task_items WHERE task_id = ? AND excluded = 0 AND origin = 'auto' "
        f"AND kind NOT IN ({marks})", (task_id, *_SKIP_KINDS))}


def record(db, task_id: int, before: dict, after: dict, run: str | None = None) -> int:
    """전후를 견주어 남긴다. 남긴 변경 수."""
    if not before or not ready(db):
        return 0
    run = run or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        change = "added" if old is None else "removed" if new is None else "changed"
        rows.append((task_id, run, change, key[0], key[1], old or "", new or ""))
    if rows:
        db.con.executemany(
            "INSERT INTO restore_changes(task_id, run, change, kind, slot, before, after) VALUES (?,?,?,?,?,?,?)", rows)
        old_runs = [r["run"] for r in db.con.execute(
            "SELECT DISTINCT run FROM restore_changes WHERE task_id = ? ORDER BY run DESC", (task_id,))][_KEEP_RUNS:]
        for stale in old_runs:
            db.con.execute("DELETE FROM restore_changes WHERE task_id = ? AND run = ?", (task_id, stale))
    return len(rows)


def latest(db, task_id: int) -> list[dict]:
    """가장 최근에 바뀐 것이 있었던 복원의 변경들."""
    if not ready(db):
        return []
    row = db.con.execute("SELECT MAX(run) FROM restore_changes WHERE task_id = ?", (task_id,)).fetchone()
    if not row or row[0] is None:
        return []
    return [dict(r) for r in db.con.execute(
        "SELECT * FROM restore_changes WHERE task_id = ? AND run = ? ORDER BY change, kind, slot", (task_id, row[0]))]


def describe(change: dict) -> str:
    """'기한 · 제출기한: 8월 22일 → 9월 5일'."""
    name = f"{rc.KIND_LABEL.get(change['kind'], change['kind'])} · {change['slot']}"
    if change["change"] == "added":
        return f"{name}: {change['after']} (새로 생김)"
    if change["change"] == "removed":
        return f"{name}: {change['before']} (사라짐)"
    return f"{name}: {change['before']} → {change['after']}"


def summary(found: list[dict]) -> str:
    counts = {key: sum(c["change"] == key for c in found) for key in LABEL}
    parts = " · ".join(f"{LABEL[key]} {n}" for key, n in counts.items() if n)
    return f"지난 분석 이후 바뀐 것({found[0]['run'][:16]}): {parts}" if found else ""
