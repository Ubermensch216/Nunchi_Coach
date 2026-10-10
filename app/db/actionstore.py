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
