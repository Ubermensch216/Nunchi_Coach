"""수행 건·실행·승인·산출물 저장 계층 (스키마 v10, 행동형 업무 비서 계획서 6절).

Database에 섞어 쓰는 믹스인이다(workstore.py와 같은 방식). 연결·트랜잭션·감사
기록은 Database의 것을 쓴다.

지키는 규칙
  · 수행 건의 기한과 근거 문서는 그 해의 공식 문서나 사람의 확인에서만 온다.
    과거 사례의 날짜를 받는 길이 없다.
  · 같은 지문(idem_key)의 실행은 하나다.
  · 승인은 승인한 내용의 지문과 함께 남는다.
  · 감사 기록에는 식별자와 결과만 적는다(SEC-005).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence

from ..core.paths import relative_to_source

CASE_STATES = ("open", "closed", "not_this_year")
CONFIRMED_SOURCES = ("official", "user")
RUN_STATES = ("planned", "running", "waiting_for_approval", "completed", "failed",
              "partial", "cancelled")
STEP_STATES = ("pending", "running", "done", "failed", "skipped", "blocked",
               "waiting_for_approval")
ARTIFACT_STATES = ("draft", "reviewed", "final", "stale", "superseded")
ARTIFACT_MODES = ("preliminary", "official")
DECISIONS = ("approved", "rejected")

_STEP_FIELDS = {"state", "input_hash", "output", "model", "prompt_version", "attempts",
                "error", "started_at", "finished_at"}


def _require(value: str, allowed: Sequence[str], name: str) -> str:
    if value not in allowed:
        raise ValueError(f"{name}에 쓸 수 없는 값입니다: {value}")
    return value


class ActionStore:
    # ── 수행 건 ─────────────────────────────────────────────────────
    def open_case(self, task_id: int, period_year: int, label: str = "") -> tuple[int, bool]:
        """(수행 건 id, 새로 만들었는가). 같은 업무·연도·이름이면 있던 것을 돌려준다."""
        label = " ".join((label or "").split())
        row = self.con.execute(
            "SELECT id FROM work_cases WHERE task_id = ? AND period_year = ? AND label = ?",
            (task_id, period_year, label)).fetchone()
        if row:
            return row["id"], False
        case_id = self.con.execute(
            "INSERT INTO work_cases(task_id, period_year, label) VALUES (?, ?, ?)",
            (task_id, period_year, label)).lastrowid
        self.audit("case.open", str(case_id), detail=f"task={task_id} year={period_year}")
        return case_id, True

    def case(self, case_id: int):
        return self.con.execute(
            "SELECT c.*, t.name AS task_name FROM work_cases c JOIN tasks t ON t.id = c.task_id "
            "WHERE c.id = ?", (case_id,)).fetchone()

    def cases(self, state: str | None = None, task_id: int | None = None) -> list:
        where, params = [], []
        if state is not None:
            where.append("c.state = ?")
            params.append(_require(state, CASE_STATES, "수행 건 상태"))
        if task_id is not None:
            where.append("c.task_id = ?")
            params.append(task_id)
        return self.con.execute(
            "SELECT c.*, t.name AS task_name FROM work_cases c JOIN tasks t ON t.id = c.task_id "
            + ("WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY c.period_year DESC, c.id DESC", params).fetchall()

    def set_case_state(self, case_id: int, state: str) -> None:
        _require(state, CASE_STATES, "수행 건 상태")
        self.con.execute(
            "UPDATE work_cases SET state = ?, closed_at = CASE WHEN ? = 'open' THEN NULL "
            "ELSE datetime('now') END WHERE id = ?", (state, state, case_id))
        self.audit("case.state", str(case_id), result=state)

    def set_case_basis(self, case_id: int, doc_id: int | None, source: str = "") -> None:
        """올해의 공식 근거 문서. 문서를 떼면 '미확인'으로 돌아간다."""
        state = _require(source, CONFIRMED_SOURCES, "근거의 출처") if doc_id else "unconfirmed"
        self.con.execute("UPDATE work_cases SET basis_doc_id = ?, basis_state = ? WHERE id = ?",
                         (doc_id, state, case_id))
        self.audit("case.basis", str(case_id), detail=f"doc={doc_id}", result=state)

    def set_case_due(self, case_id: int, due_date: str | None, source: str = "") -> None:
        """올해의 기한. 출처는 공식 문서나 사람의 확인뿐이다."""
        state = _require(source, CONFIRMED_SOURCES, "기한의 출처") if due_date else "unknown"
        self.con.execute("UPDATE work_cases SET due_date = ?, due_state = ? WHERE id = ?",
                         (due_date or None, state, case_id))
        self.audit("case.due", str(case_id), result=state)

    # ── 실행 ────────────────────────────────────────────────────────
    def create_run(self, plan_key: str, plan_version: str, idem_key: str, params: dict,
                   steps: Iterable[dict], task_id: int | None = None,
                   case_id: int | None = None,
                   recommendation_id: int | None = None) -> tuple[int, bool]:
        """(실행 id, 새로 만들었는가). 같은 지문의 실행이 있으면 그것을 돌려준다."""
        row = self.con.execute("SELECT id FROM action_runs WHERE idem_key = ?",
                               (idem_key,)).fetchone()
        if row:
            return row["id"], False
        with self.transaction():
            run_id = self.con.execute(
                "INSERT INTO action_runs(case_id, task_id, recommendation_id, plan_key, "
                "plan_version, idem_key, params) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (case_id, task_id, recommendation_id, plan_key, plan_version, idem_key,
                 json.dumps(params, ensure_ascii=False, sort_keys=True))).lastrowid
            for ordinal, step in enumerate(steps):
                self.con.execute(
                    "INSERT INTO action_steps(run_id, ordinal, step_key, tool, tool_version, risk) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, ordinal, step["key"], step["tool"], step["version"], step["risk"]))
        self.audit("action.run", str(run_id), detail=plan_key, result="planned")
        return run_id, True

    def run(self, run_id: int):
        return self.con.execute("SELECT * FROM action_runs WHERE id = ?", (run_id,)).fetchone()

    def runs(self, case_id: int | None = None, task_id: int | None = None) -> list:
        where, params = [], []
        if case_id is not None:
            where.append("case_id = ?")
            params.append(case_id)
        if task_id is not None:
            where.append("task_id = ?")
            params.append(task_id)
        return self.con.execute(
            "SELECT * FROM action_runs " + ("WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY id DESC", params).fetchall()

    def set_run_state(self, run_id: int, state: str) -> None:
        _require(state, RUN_STATES, "실행 상태")
        ended = state in ("completed", "failed", "partial", "cancelled")
        self.con.execute(
            "UPDATE action_runs SET state = ?, "
            "started_at = COALESCE(started_at, datetime('now')), "
            "finished_at = CASE WHEN ? THEN datetime('now') ELSE NULL END WHERE id = ?",
            (state, int(ended), run_id))
        self.audit("action.run", str(run_id), result=state)

    def run_steps(self, run_id: int) -> list:
        return self.con.execute(
            "SELECT * FROM action_steps WHERE run_id = ? ORDER BY ordinal", (run_id,)).fetchall()

    def run_step(self, step_id: int):
        return self.con.execute("SELECT * FROM action_steps WHERE id = ?", (step_id,)).fetchone()

    def update_run_step(self, step_id: int, **fields) -> None:
        unknown = set(fields) - _STEP_FIELDS
        if unknown:
            raise ValueError(f"단계에 없는 열입니다: {sorted(unknown)}")
        if "state" in fields:
            _require(fields["state"], STEP_STATES, "단계 상태")
        if isinstance(fields.get("output"), dict):
            fields["output"] = json.dumps(fields["output"], ensure_ascii=False, sort_keys=True)
        columns = ", ".join(f"{name} = ?" for name in fields)
        self.con.execute(f"UPDATE action_steps SET {columns} WHERE id = ?",
                         (*fields.values(), step_id))
        if "state" in fields:
            row = self.run_step(step_id)
            self.audit("action.step", str(step_id), detail=f"run={row['run_id']} tool={row['tool']}",
                       result=fields["state"])

    def recover_actions(self) -> int:
        """실행 도중 프로세스가 죽은 흔적을 되돌린다. 되돌린 실행 수를 준다.

        '실행 중'으로 남은 단계는 끝났는지 알 수 없으므로 다시 할 일로 돌린다.
        파일은 임시 이름으로 쓰고 바꿔 끼우므로 반쪽 산출물은 남지 않는다.
        """
        runs = [row["id"] for row in self.con.execute(
            "SELECT id FROM action_runs WHERE state = 'running'")]
        if not runs:
            return 0
        with self.transaction():
            self.con.execute("UPDATE action_steps SET state = 'pending' WHERE state = 'running'")
            self.con.execute(
                "UPDATE action_runs SET state = 'partial', finished_at = datetime('now') "
                "WHERE state = 'running'")
        self.audit("action.recover", detail=f"runs={len(runs)}", result="partial")
        return len(runs)

    # ── 승인 ────────────────────────────────────────────────────────
    def add_approval(self, run_id: int, step_id: int, action: str, scope_hash: str,
                     decision: str, approver_name: str = "", approver_role: str = "successor",
                     note: str = "") -> int:
        _require(decision, DECISIONS, "승인 결정")
        approval_id = self.con.execute(
            "INSERT INTO action_approvals(run_id, step_id, action, scope_hash, decision, "
            "approver_name, approver_role, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, step_id, action, scope_hash, decision,
             " ".join((approver_name or "").split()), approver_role, note or None)).lastrowid
        # 이름은 감사 기록에 남기지 않는다(work.reviewer와 같다).
        self.audit("action.approval", str(step_id), detail=f"run={run_id} tool={action}",
                   result=decision)
        return approval_id

    def approval_for(self, step_id: int, scope_hash: str):
        """그 내용에 대한 가장 최근의 결정. 내용이 달랐던 결정은 돌려주지 않는다."""
        return self.con.execute(
            "SELECT * FROM action_approvals WHERE step_id = ? AND scope_hash = ? "
            "ORDER BY id DESC LIMIT 1", (step_id, scope_hash)).fetchone()

    def approvals(self, run_id: int) -> list:
        return self.con.execute(
            "SELECT * FROM action_approvals WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()

    # ── 산출물 ──────────────────────────────────────────────────────
    def add_artifact(self, kind: str, title: str, fmt: str, path: str, sha256: str, *,
                     run_id: int | None = None, step_id: int | None = None,
                     case_id: int | None = None, mode: str = "preliminary",
                     basis_year: int | None = None, sources: Sequence[dict] = (),
                     version: int = 1, supersedes_id: int | None = None) -> int:
        _require(mode, ARTIFACT_MODES, "산출물 모드")
        artifact_id = self.con.execute(
            "INSERT INTO artifacts(run_id, step_id, case_id, kind, title, mode, basis_year, "
            "format, path, sha256, source_snapshot, version, supersedes_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, step_id, case_id, kind, title, mode, basis_year, fmt, path, sha256,
             json.dumps(list(sources), ensure_ascii=False, sort_keys=True), version,
             supersedes_id)).lastrowid
        self.audit("artifact.save", str(artifact_id), detail=f"kind={kind} v={version}",
                   result=mode)
        return artifact_id

    def artifact(self, artifact_id: int):
        return self.con.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()

    def artifacts(self, case_id: int | None = None, run_id: int | None = None,
                  include_superseded: bool = False) -> list:
        where, params = [], []
        if case_id is not None:
            where.append("case_id = ?")
            params.append(case_id)
        if run_id is not None:
            where.append("run_id = ?")
            params.append(run_id)
        if not include_superseded:
            where.append("state != 'superseded'")
        return self.con.execute(
            "SELECT * FROM artifacts " + ("WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY id", params).fetchall()

    def latest_artifact(self, case_id: int | None, kind: str, title: str):
        return self.con.execute(
            "SELECT * FROM artifacts WHERE case_id IS ? AND kind = ? AND title = ? "
            "AND state != 'superseded' ORDER BY id DESC LIMIT 1",
            (case_id, kind, title)).fetchone()

    def set_artifact_state(self, artifact_id: int, state: str) -> None:
        _require(state, ARTIFACT_STATES, "산출물 상태")
        self.con.execute("UPDATE artifacts SET state = ? WHERE id = ?", (state, artifact_id))
        self.audit("artifact.state", str(artifact_id), result=state)

    # ── 체크 항목 ───────────────────────────────────────────────────
    def checklist(self, case_id: int) -> list:
        return self.con.execute(
            "SELECT * FROM checklist_items WHERE case_id = ? ORDER BY ordinal, id",
            (case_id,)).fetchall()

    def sync_checklist(self, case_id: int, run_id: int | None, items: Iterable[dict]) -> dict:
        """자동으로 만든 체크 항목을 반영한다. 사람이 손댄 것은 덮지 않는다.

        같은 항목은 slot으로 찾는다. 사람이 더했거나 고친 항목(origin='user')과
        이미 손을 댄 항목(진행·완료·건너뜀)의 상태는 그대로 둔다. 이번에 나오지 않은
        자동 항목은 아직 손대지 않았을 때만 지운다.
        """
        counts = {"added": 0, "updated": 0, "kept": 0, "removed": 0}
        existing = {row["slot"]: row for row in self.checklist(case_id)}
        seen: set[str] = set()
        ids: dict[str, int] = {}
        with self.transaction():
            for ordinal, item in enumerate(items):
                slot = item["slot"]
                seen.add(slot)
                _require(item["source_type"], CHECK_SOURCES, "체크 항목의 출처")
                due = item.get("due_date")
                due_state = _require(item.get("due_state", "official"), CONFIRMED_SOURCES,
                                     "기한의 출처") if due else "unknown"
                state = item.get("state", "todo")
                row = existing.get(slot)
                values = (run_id, item.get("work_step_id"), item.get("code", ""), ordinal,
                          item.get("phase", ""), item["title"], item.get("description", ""),
                          item["source_type"], due, due_state,
                          int(bool(item.get("approval_required"))),
                          item.get("completion_kind"), item.get("completion_ref"))
                if row is None:
                    ids[slot] = self.con.execute(
                        "INSERT INTO checklist_items(case_id, slot, run_id, work_step_id, code, "
                        "ordinal, phase, title, description, source_type, due_date, due_state, "
                        "approval_required, completion_kind, completion_ref, state) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (case_id, slot, *values, state)).lastrowid
                    counts["added"] += 1
                    continue
                ids[slot] = row["id"]
                if row["origin"] == "user":
                    counts["kept"] += 1
                    continue
                touched = row["state"] in CHECK_TOUCHED
                self.con.execute(
                    "UPDATE checklist_items SET run_id = ?, work_step_id = ?, code = ?, "
                    "ordinal = ?, phase = ?, title = ?, description = ?, source_type = ?, "
                    "due_date = ?, due_state = ?, approval_required = ?, completion_kind = ?, "
                    "completion_ref = ?, state = ?, updated_at = datetime('now') WHERE id = ?",
                    (*values, row["state"] if touched else state, row["id"]))
                counts["kept" if touched else "updated"] += 1
            for slot, row in existing.items():
                if slot not in seen and row["origin"] == "auto" and row["state"] not in CHECK_TOUCHED:
                    self.con.execute("DELETE FROM checklist_items WHERE id = ?", (row["id"],))
                    counts["removed"] += 1
            for item in items:
                after = item.get("after")
                if after in ids:
                    self.con.execute(
                        "UPDATE checklist_items SET depends_on = ? WHERE id = ? AND origin = 'auto'",
                        (ids[after], ids[item["slot"]]))
        self.audit("checklist.sync", str(case_id),
                   detail=" ".join(f"{key}={value}" for key, value in counts.items()))
        return counts

    def set_check_state(self, item_id: int, state: str, note: str = "") -> None:
        """사람이 체크 항목의 상태를 바꾼다. 연결된 수행 단계가 있으면 함께 적는다."""
        _require(state, CHECK_STATES, "체크 항목 상태")
        row = self.con.execute("SELECT * FROM checklist_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise ValueError("없는 체크 항목입니다.")
        with self.transaction():
            self.con.execute(
                "UPDATE checklist_items SET state = ?, completion_kind = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (state, "user" if state == "done" else row["completion_kind"], item_id))
            if row["work_step_id"] and state in _RUN_OF_CHECK:
                self.set_step_run(row["work_step_id"], _RUN_OF_CHECK[state], note=note or None)
        self.audit("checklist.state", str(item_id), result=state)

    def add_check_item(self, case_id: int, title: str, description: str = "",
                       phase: str = "") -> int:
        """사람이 더한 항목. 다시 만들어도 남는다."""
        title = " ".join((title or "").split())
        if not title:
            raise ValueError("항목의 이름을 적어 주세요.")
        ordinal = self.con.execute(
            "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM checklist_items WHERE case_id = ?",
            (case_id,)).fetchone()[0]
        slot = f"user:{ordinal}:{title[:40]}"
        item_id = self.con.execute(
            "INSERT INTO checklist_items(case_id, slot, ordinal, phase, title, description, "
            "source_type, origin) VALUES (?, ?, ?, ?, ?, ?, 'user', 'user')",
            (case_id, slot, ordinal, phase, title, description)).lastrowid
        self.audit("checklist.add", str(item_id), detail=f"case={case_id}")
        return item_id

    # ── 신호와 제안 ─────────────────────────────────────────────────
    def add_signal(self, kind: str, task_id: int | None, key: str, detail: dict) -> tuple[int, bool]:
        """(신호 id, 새로 적었는가). 같은 열쇠의 신호는 한 번만 적는다."""
        _require(kind, SIGNAL_KINDS, "신호의 갈래")
        row = self.con.execute("SELECT id FROM signals WHERE signal_key = ?", (key,)).fetchone()
        if row:
            return row["id"], False
        signal_id = self.con.execute(
            "INSERT INTO signals(kind, task_id, signal_key, detail) VALUES (?, ?, ?, ?)",
            (kind, task_id, key, json.dumps(detail, ensure_ascii=False, sort_keys=True))).lastrowid
        self.audit("signal.add", str(signal_id), detail=f"{kind} task={task_id}")
        return signal_id, True

    def recommendation(self, rec_id: int):
        return self.con.execute(
            "SELECT r.*, t.name AS task_name FROM recommendations r "
            "LEFT JOIN tasks t ON t.id = r.task_id WHERE r.id = ?", (rec_id,)).fetchone()

    def recommendation_by_key(self, key: str):
        return self.con.execute("SELECT * FROM recommendations WHERE rec_key = ?", (key,)).fetchone()

    def recommendations(self, states: Sequence[str] | None = None) -> list:
        where, params = "", []
        if states:
            where = "WHERE r.state IN (%s)" % ", ".join("?" * len(states))
            params = [_require(state, REC_STATES, "제안 상태") for state in states]
        return self.con.execute(
            "SELECT r.*, t.name AS task_name, g.detail AS signal_detail FROM recommendations r "
            "LEFT JOIN tasks t ON t.id = r.task_id LEFT JOIN signals g ON g.id = r.signal_id "
            f"{where} ORDER BY r.id", params).fetchall()

    def add_recommendation(self, signal_id: int | None, task_id: int, target_year: int, plan_key: str,
                           rec_key: str, certainty: str) -> int:
        _require(certainty, CERTAINTIES, "제안의 확실성")
        rec_id = self.con.execute(
            "INSERT INTO recommendations(signal_id, task_id, target_year, plan_key, rec_key, certainty) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (signal_id, task_id, target_year, plan_key, rec_key, certainty)).lastrowid
        self.audit("recommendation.add", str(rec_id), detail=f"{plan_key} task={task_id} year={target_year}",
                   result=certainty)
        return rec_id

    def update_recommendation(self, rec_id: int, **fields) -> None:
        unknown = set(fields) - {"signal_id", "certainty"}
        if unknown:
            raise ValueError(f"제안에 없는 열입니다: {sorted(unknown)}")
        if "certainty" in fields:
            _require(fields["certainty"], CERTAINTIES, "제안의 확실성")
        columns = ", ".join(f"{name} = ?" for name in fields)
        self.con.execute(f"UPDATE recommendations SET {columns} WHERE id = ?", (*fields.values(), rec_id))

    def set_recommendation_state(self, rec_id: int, state: str, reason: str | None = None,
                                 snooze_until: str | None = None) -> None:
        _require(state, REC_STATES, "제안 상태")
        decided = state in ("accepted", "dismissed", "snoozed", "superseded", "expired")
        self.con.execute(
            "UPDATE recommendations SET state = ?, dismiss_reason = COALESCE(?, dismiss_reason), "
            "snooze_until = ?, decided_at = CASE WHEN ? THEN datetime('now') ELSE decided_at END "
            "WHERE id = ?", (state, reason, snooze_until, int(decided), rec_id))
        self.audit("recommendation.state", str(rec_id), result=state)

    def replace_recommendation_evidence(self, rec_id: int, items: Iterable[dict]) -> None:
        """제안의 근거를 통째로 바꾼다. 같은 지문의 제안이 다시 계산될 때 쓴다."""
        with self.transaction():
            self.con.execute("DELETE FROM evidence WHERE owner_kind = 'rec' AND owner_id = ?", (rec_id,))
            for item in items:
                document = self.con.execute(
                    "SELECT d.hash, d.filename, d.path, s.path AS root FROM documents d "
                    "JOIN sources s ON s.id = d.source_id WHERE d.id = ?", (item["doc_id"],)).fetchone()
                if document is None:
                    continue
                self.add_evidence("rec", rec_id, item["doc_id"], document["hash"], item.get("locator", ""),
                                  item.get("quote", ""), document["filename"],
                                  relative_to_source(document["path"], document["root"] or ""))

    def mark_artifacts_stale(self, case_id: int, modes: Sequence[str]) -> int:
        """입력이 달라져 더는 최신이 아닌 산출물을 ‘낡음’으로 표시한다. 파일은 지우지 않는다."""
        rows = self.con.execute(
            "SELECT id FROM artifacts WHERE case_id = ? AND state IN ('draft', 'reviewed') "
            f"AND mode IN ({', '.join('?' * len(modes))})", (case_id, *modes)).fetchall()
        for row in rows:
            self.con.execute("UPDATE artifacts SET state = 'stale' WHERE id = ?", (row["id"],))
        if rows:
            self.audit("artifact.stale", str(case_id), detail=f"n={len(rows)}", result=",".join(modes))
        return len(rows)


SIGNAL_KINDS = ("time", "event", "gap", "conversation")
REC_STATES = ("new", "seen", "accepted", "dismissed", "snoozed", "expired", "superseded")
CERTAINTIES = ("official", "precedent", "suggested")
CHECK_SOURCES = ("official", "stated", "precedent", "suggested", "user")
CHECK_STATES = ("todo", "doing", "review", "done", "skipped")
CHECK_TOUCHED = ("doing", "done", "skipped")
_RUN_OF_CHECK = {"todo": "not_started", "doing": "in_progress", "done": "done"}
