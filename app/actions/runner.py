"""실행기 — 계획 틀의 단계를 차례로 돌린다 (행동형 업무 비서 계획서 5.2절).

    실행  planned → running → waiting_for_approval → running → completed
                         ↘ failed / partial / cancelled
    단계  pending → running → done | failed | skipped | blocked | waiting_for_approval

지키는 것
  · **승인 없이 변경하지 않는다.** 변경 등급 도구는 그 내용의 지문에 묶인 승인이
    있을 때만 부른다. 내용이 달라졌으면 예전 승인은 쓰지 못한다.
  · **다시 눌러도 둘이 되지 않는다.** 계획 틀·업무·연도·입력 문서의 판·인자가
    같으면 같은 실행이다. 끝난 단계는 다시 돌지 않는다.
  · **실패는 그 자리에 남는다.** 실패한 단계에서 멈추고, 다시 실행하면 그 단계부터
    이어진다. 같은 단계를 끝없이 되풀이하지 않는다(MAX_ATTEMPTS).
  · **원본을 건드리지 않는다.** 실행 앞뒤로 원본의 지문을 견주고, 달라졌으면
    실행을 실패로 남긴다.
  · 기록에는 식별자와 결과만 적는다. 도구가 낸 오류 가운데 사용자에게 보여도
    되는 문장(ToolError)만 그대로 남기고, 그 밖의 예외는 종류만 남긴다 — 예외
    문구에는 문서의 본문이 섞여 있을 수 있다.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import date

from ..core import diagnostics
from ..db import Database
from . import context as ctx
from . import policy
from .plans import PLANS, Plans
from .registry import REGISTRY, Blocked, Registry, ToolError
from .tools import verify

MAX_ATTEMPTS = 3      # jobs/pipeline.py의 재시도 상한과 같다

PLANNED, RUNNING, WAITING = "planned", "running", "waiting_for_approval"
COMPLETED, FAILED, PARTIAL, CANCELLED = "completed", "failed", "partial", "cancelled"
ENDED = (COMPLETED, CANCELLED)

RUN_LABEL = {PLANNED: "시작 전", RUNNING: "실행 중", WAITING: "승인 대기", COMPLETED: "완료",
             FAILED: "실패", PARTIAL: "일부만 완료", CANCELLED: "취소"}
STEP_LABEL = {"pending": "대기", "running": "실행 중", "done": "완료", "failed": "실패",
              "skipped": "건너뜀", "blocked": "조치 필요", WAITING: "승인 대기"}

NOT_APPROVED = "승인하지 않아 실행하지 않았습니다."
NEEDS_MISSING = "앞 단계가 끝나지 않아 건너뛰었습니다."
TOO_MANY = "여러 번 시도했지만 끝내지 못했습니다."
ORIGINALS_CHANGED = "실행하는 동안 원본 자료가 달라졌습니다. 결과를 믿을 수 없어 실패로 남깁니다."


class Runner:
    def __init__(self, db: Database, registry: Registry = REGISTRY, plans: Plans = PLANS,
                 today: date | None = None, should_stop: Callable[[], bool] = lambda: False,
                 client=None, on_step: Callable[[int, int, str], None] | None = None) -> None:
        self.db = db
        self.registry, self.plans = registry, plans
        self.today = today or date.today()
        self.should_stop = should_stop
        self.client = client
        self.on_step = on_step or (lambda ordinal, total, label: None)

    # ── 시작 ────────────────────────────────────────────────────────
    def start(self, plan_key: str, task_id: int | None = None, target_year: int | None = None,
              params: dict | None = None, case_id: int | None = None,
              recommendation_id: int | None = None,
              inputs: Sequence[str] | None = None) -> int:
        """실행을 만든다. 같은 일이 이미 있으면 그 실행의 id를 돌려준다.

        inputs는 이 실행이 딛고 선 문서의 판(해시)이다. 주지 않으면 그 업무에 묶인
        문서 전부다 — 자료가 달라지면 다른 실행이 된다.
        """
        plan = self.plans.get(plan_key)
        tools = [self.registry.get(step.tool) for step in plan.steps]
        params = {**(params or {}), "task_id": task_id, "target_year": target_year}
        if inputs is None:
            inputs = self._task_hashes(task_id)
        key = policy.digest({"plan": plan.key, "version": plan.version, "task": task_id,
                             "year": target_year, "inputs": sorted(inputs), "params": params,
                             "tools": [(tool.name, tool.version) for tool in tools]})
        run_id, _ = self.db.create_run(
            plan.key, plan.version, key, params,
            [{"key": step.key, "tool": tool.name, "version": tool.version, "risk": tool.risk}
             for step, tool in zip(plan.steps, tools)],
            task_id=task_id, case_id=case_id, recommendation_id=recommendation_id)
        return run_id

    def _task_hashes(self, task_id: int | None) -> list[str]:
        if task_id is None:
            return []
        return [row["hash"] for row in self.db.con.execute(
            "SELECT d.hash FROM task_docs td JOIN documents d ON d.id = td.doc_id "
            "WHERE td.task_id = ? AND d.hash IS NOT NULL AND d.missing_since IS NULL",
            (task_id,))]

    # ── 실행 ────────────────────────────────────────────────────────
    def execute(self, run_id: int) -> str:
        """끝나거나, 승인을 기다리거나, 막힐 때까지 돌린다. 실행의 상태를 돌려준다."""
        db = self.db
        run = db.run(run_id)
        if run is None:
            raise ValueError("없는 실행입니다.")
        if run["state"] in ENDED:
            return run["state"]
        plan = self.plans.get(run["plan_key"])
        if plan.version != run["plan_version"]:
            raise ToolError("이 실행을 만든 뒤 계획 틀이 바뀌었습니다. 새로 실행해 주세요.")
        params = json.loads(run["params"])

        # 지난번에 '실행 중'으로 남은 단계는 끝났는지 알 수 없다. 다시 한다.
        for step in db.run_steps(run_id):
            if step["state"] == "running":
                db.update_run_step(step["id"], state="pending")

        before = verify.fingerprint(db)
        db.set_run_state(run_id, RUNNING)
        reader = ctx.ReadOnlyDatabase(db.path)
        outcome = None
        try:
            outcome = self._steps(run, plan, params, reader)
        finally:
            reader.close()
            if verify.changed(before, verify.fingerprint(db)):
                db.audit("action.originals", str(run_id), result="changed")
                outcome = FAILED
            db.set_run_state(run_id, outcome or FAILED)
        return outcome

    def _steps(self, run, plan, params: dict, reader: Database) -> str:
        db, run_id = self.db, run["id"]
        total = len(plan.steps)
        for plan_step in plan.steps:
            if self.should_stop():
                return PARTIAL
            step = db.run_steps(run_id)[plan.steps.index(plan_step)]
            if step["state"] in ("done", "skipped"):
                continue
            outputs = self._outputs(run_id)
            if any(need not in outputs for need in plan_step.needs):
                db.update_run_step(step["id"], state="skipped", error=NEEDS_MISSING)
                continue

            tool = self.registry.get(step["tool"])
            if tool.version != step["tool_version"]:
                raise ToolError("이 실행을 만든 뒤 도구가 바뀌었습니다. 새로 실행해 주세요.")
            bound = plan_step.bind(params, {key: outputs[key] for key in plan_step.needs})

            if policy.needs_approval(tool.risk):
                decision = db.approval_for(step["id"], self._scope(tool, bound))
                if decision is None:
                    db.update_run_step(step["id"], state=WAITING)
                    return WAITING
                if decision["decision"] != policy.APPROVED:
                    db.update_run_step(step["id"], state="skipped", error=NOT_APPROVED)
                    continue

            if step["attempts"] >= MAX_ATTEMPTS:
                db.update_run_step(step["id"], state="failed", error=TOO_MANY)
                return self._stopped_at(run_id)

            self.on_step(step["ordinal"], total, tool.label or tool.name)
            db.update_run_step(step["id"], state="running", attempts=step["attempts"] + 1,
                               input_hash=policy.digest([tool.name, tool.version, bound]),
                               error=None)
            db.con.execute("UPDATE action_steps SET started_at = datetime('now') WHERE id = ?",
                           (step["id"],))
            handle = ctx.build(db, reader, tool.risk, run_id=run_id, step_id=step["id"],
                               task_id=run["task_id"], case_id=run["case_id"], today=self.today,
                               should_stop=self.should_stop, client=self.client)
            try:
                result = tool.run(handle, bound)
            except Blocked as exc:
                # 사용자의 조치가 있어야 한다. 시도 횟수를 쓰지 않는다.
                db.update_run_step(step["id"], state="blocked", error=str(exc)[:300],
                                   attempts=step["attempts"])
                return self._stopped_at(run_id)
            except ToolError as exc:
                db.update_run_step(step["id"], state="failed", error=str(exc)[:300])
                return self._stopped_at(run_id)
            except Exception as exc:
                diagnostics.record("action.error", exc)
                db.update_run_step(step["id"], state="failed",
                                   error=f"도구가 끝나지 못했습니다 ({type(exc).__name__}).")
                return self._stopped_at(run_id)
            if not isinstance(result, dict):
                db.update_run_step(step["id"], state="failed",
                                   error="도구가 결과를 돌려주지 않았습니다.")
                return self._stopped_at(run_id)
            db.update_run_step(step["id"], state="done", output=result)
            db.con.execute("UPDATE action_steps SET finished_at = datetime('now') WHERE id = ?",
                           (step["id"],))
        return COMPLETED

    def _outputs(self, run_id: int) -> dict[str, dict]:
        return {step["step_key"]: json.loads(step["output"])
                for step in self.db.run_steps(run_id) if step["state"] == "done"}

    def _stopped_at(self, run_id: int) -> str:
        """끝낸 단계가 하나라도 있으면 '일부만 완료', 없으면 '실패'."""
        done = any(step["state"] == "done" for step in self.db.run_steps(run_id))
        return PARTIAL if done else FAILED

    def _scope(self, tool, bound: dict) -> str:
        target = tool.scope(self.db, bound) if tool.scope is not None else bound
        return policy.scope_hash(tool.name, tool.version, target)

    # ── 승인 ────────────────────────────────────────────────────────
    def pending_approval(self, run_id: int) -> dict | None:
        """승인을 기다리는 단계와 승인할 내용. 화면이 이것을 보여 주고 묻는다."""
        for step in self.db.run_steps(run_id):
            if step["state"] != WAITING:
                continue
            tool = self.registry.get(step["tool"])
            bound = self._bound(run_id, step)
            return {"step_id": step["id"], "tool": tool.name, "label": tool.label or tool.name,
                    "risk": tool.risk, "params": bound,
                    "scope": tool.scope(self.db, bound) if tool.scope is not None else bound}
        return None

    def decide(self, step_id: int, approved: bool, note: str = "") -> None:
        """기다리는 단계를 승인하거나 거절한다. 이어서 돌리려면 execute를 다시 부른다.

        표시명은 로컬 이력일 뿐 본인 확인이 아니다(reviews와 같다).
        """
        step = self.db.run_step(step_id)
        if step is None or step["state"] != WAITING:
            raise ValueError("승인을 기다리는 단계가 아닙니다.")
        tool = self.registry.get(step["tool"])
        name, role = self.db.reviewer()
        self.db.add_approval(
            step["run_id"], step_id, tool.name,
            self._scope(tool, self._bound(step["run_id"], step)),
            policy.APPROVED if approved else policy.REJECTED, name, role, note)

    def _bound(self, run_id: int, step) -> dict:
        run = self.db.run(run_id)
        plan_step = self.plans.get(run["plan_key"]).steps[step["ordinal"]]
        outputs = self._outputs(run_id)
        return plan_step.bind(json.loads(run["params"]),
                              {key: outputs[key] for key in plan_step.needs if key in outputs})

    # ── 취소 ────────────────────────────────────────────────────────
    def cancel(self, run_id: int) -> None:
        """남은 단계를 하지 않기로 한다. 이미 만든 산출물은 그대로 둔다."""
        run = self.db.run(run_id)
        if run is None or run["state"] in ENDED:
            return
        with self.db.transaction():
            for step in self.db.run_steps(run_id):
                if step["state"] not in ("done", "skipped"):
                    self.db.update_run_step(step["id"], state="skipped", error="취소했습니다.")
            self.db.set_run_state(run_id, CANCELLED)
