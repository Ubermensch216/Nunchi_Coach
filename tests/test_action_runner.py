"""실행기·정책·산출물 원장 (행동형 업무 비서 계획서 0단계, T07·T08·T09).

가짜 도구 셋(읽기·초안·변경)으로 실행기만 본다. 실제 업무 도구는 아직 없다.
"""

import json
import os
import sqlite3
import time
from datetime import date
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

from app.actions import artifacts, policy
from app.actions.plans import Plan, Plans, PlanStep
from app.actions.registry import Blocked, Registry, Tool, ToolError
from app.actions.runner import (COMPLETED, FAILED, PARTIAL, WAITING, CANCELLED, MAX_ATTEMPTS,
                                Runner)
from app.core import cases
from app.core.output_policy import UnsafeOutputError
from app.db import Database

TODAY = date(2026, 10, 10)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "project" / "project.db")
    database.init()
    source = tmp_path / "자료"
    source.mkdir()
    (source / "계획.txt").write_text("제출기한: 2025-11-14", encoding="utf-8")
    source_id = database.con.execute(
        "INSERT INTO sources(path) VALUES (?)", (str(source),)).lastrowid
    doc_id = database.con.execute(
        "INSERT INTO documents(source_id, path, filename, ext, hash) VALUES (?, ?, ?, ?, ?)",
        (source_id, str(source / "계획.txt"), "계획.txt", ".txt", "h1")).lastrowid
    task_id = database.con.execute("INSERT INTO tasks(name) VALUES ('평가 대응')").lastrowid
    database.con.execute("INSERT INTO task_docs(task_id, doc_id) VALUES (?, ?)", (task_id, doc_id))
    database.task_id, database.doc_id, database.source = task_id, doc_id, source
    yield database
    database.close()


class Kit:
    """가짜 도구와 계획 틀. 도구가 몇 번 불렸는지 센다."""

    def __init__(self):
        self.calls = {"find": 0, "write": 0, "finalize": 0}
        self.fail_write = 0          # 이만큼은 실패한다
        self.text = "초안 1"
        self.registry, self.plans = Registry(), Plans()
        self.registry.register(Tool("find", "1", policy.READ, self._find, label="자료 찾기"))
        self.registry.register(Tool("write", "1", policy.DRAFT, self._write, label="초안 쓰기"))
        self.registry.register(Tool("finalize", "1", policy.CHANGE, self._finalize,
                                    label="확정", scope=self._scope))
        self.plans.register(Plan("prep", "1", "준비", (
            PlanStep("find", "find"),
            PlanStep("write", "write", needs=("find",)),
            PlanStep("finalize", "finalize", needs=("write",)),
        )))

    def _find(self, context, params):
        self.calls["find"] += 1
        assert context.artifacts is None
        return {"documents": [row["id"] for row in context.db.con.execute(
            "SELECT id FROM documents")]}

    def _write(self, context, params):
        self.calls["write"] += 1
        if self.fail_write:
            self.fail_write -= 1
            raise RuntimeError("본문이 섞인 예외 문구: 제출기한 2025-11-14")
        artifact_id = context.artifacts.save(
            "checklist", "체크리스트", "체크리스트.md", self.text.encode("utf-8"),
            basis_year=params["target_year"])
        return {"artifact_id": artifact_id}

    def _scope(self, db, params):
        row = db.artifact(params["inputs"]["write"]["artifact_id"])
        return {"artifact": row["id"], "sha256": row["sha256"]}

    def _finalize(self, context, params):
        self.calls["finalize"] += 1
        context.db.set_artifact_state(params["inputs"]["write"]["artifact_id"], "final")
        return {"final": True}

    def runner(self, db, **extra):
        return Runner(db, self.registry, self.plans, today=TODAY, **extra)


@pytest.fixture
def kit():
    return Kit()


def start(kit, db, **extra):
    case_id = cases.open_case(db, db.task_id, 2026)
    runner = kit.runner(db, **extra)
    return runner, runner.start("prep", db.task_id, 2026, case_id=case_id)


def states(db, run_id):
    return [step["state"] for step in db.run_steps(run_id)]


# ── 등록부와 정책 ───────────────────────────────────────────────────

def test_external_tools_cannot_be_registered():
    registry = Registry()
    with pytest.raises(ValueError, match="외부 전송"):
        registry.register(Tool("send_letter", "1", policy.EXTERNAL, lambda c, p: {}))
    assert "send_letter" not in registry


def test_tool_without_a_known_risk_is_refused():
    with pytest.raises(ValueError, match="위험 등급"):
        Registry().register(Tool("mystery", "1", "", lambda c, p: {}))


def test_plan_step_cannot_need_a_later_step():
    with pytest.raises(ValueError, match="앞에 없습니다"):
        Plan("bad", "1", "", (PlanStep("a", "find", needs=("b",)), PlanStep("b", "find")))


# ── 승인 ────────────────────────────────────────────────────────────

def test_change_tool_waits_for_approval(db, kit):
    runner, run_id = start(kit, db)
    assert runner.execute(run_id) == WAITING
    assert states(db, run_id) == ["done", "done", WAITING]
    assert kit.calls["finalize"] == 0
    artifact = db.artifacts(run_id=run_id)[0]
    assert artifact["state"] == "draft"

    pending = runner.pending_approval(run_id)
    assert pending["risk"] == policy.CHANGE and pending["scope"]["sha256"] == artifact["sha256"]
    runner.decide(pending["step_id"], True)
    assert runner.execute(run_id) == COMPLETED
    assert kit.calls == {"find": 1, "write": 1, "finalize": 1}
    assert db.artifact(artifact["id"])["state"] == "final"


def test_rejection_keeps_the_draft(db, kit):
    """T08. 거절하면 변경 도구는 돌지 않고 초안은 그대로다."""
    runner, run_id = start(kit, db)
    runner.execute(run_id)
    artifact = db.artifacts(run_id=run_id)[0]
    before = Path(artifact["path"]).read_bytes()
    runner.decide(runner.pending_approval(run_id)["step_id"], False, "아직 검토 중")
    assert runner.execute(run_id) == COMPLETED
    assert kit.calls["finalize"] == 0
    assert states(db, run_id)[2] == "skipped"
    assert db.artifact(artifact["id"])["state"] == "draft"
    assert Path(artifact["path"]).read_bytes() == before


def test_approval_does_not_cover_changed_content(db, kit):
    """승인은 승인한 내용에 묶인다. 그 뒤 내용이 달라지면 다시 물어야 한다."""
    runner, run_id = start(kit, db)
    runner.execute(run_id)
    step_id = runner.pending_approval(run_id)["step_id"]
    runner.decide(step_id, True)
    # 승인한 뒤, 실행하기 전에 산출물의 내용이 바뀌었다.
    artifact = db.artifacts(run_id=run_id)[0]
    db.con.execute("UPDATE artifacts SET sha256 = 'other' WHERE id = ?", (artifact["id"],))
    assert runner.execute(run_id) == WAITING
    assert kit.calls["finalize"] == 0


def test_decide_refuses_a_step_that_is_not_waiting(db, kit):
    runner, run_id = start(kit, db)
    runner.execute(run_id)
    with pytest.raises(ValueError):
        runner.decide(db.run_steps(run_id)[0]["id"], True)


def test_approval_is_audited_without_the_name(db, kit):
    db.set_reviewer("김주무관", "successor")
    runner, run_id = start(kit, db)
    runner.execute(run_id)
    runner.decide(runner.pending_approval(run_id)["step_id"], True)
    approval = db.approvals(run_id)[0]
    assert approval["approver_name"] == "김주무관" and approval["decision"] == "approved"
    logged = " ".join(f"{row['target']} {row['detail']} {row['result']}" for row in db.recent_audit(200))
    assert "김주무관" not in logged and "action.approval" in {
        row["action"] for row in db.recent_audit(200)}


# ── 멱등성과 재개 ───────────────────────────────────────────────────

def test_same_request_is_the_same_run(db, kit):
    """T09. 다시 실행해도 실행·산출물·승인이 늘지 않는다."""
    runner, run_id = start(kit, db)
    runner.execute(run_id)
    runner.decide(runner.pending_approval(run_id)["step_id"], True)
    runner.execute(run_id)

    again, same = start(kit, db)
    assert same == run_id
    assert again.execute(same) == COMPLETED
    assert kit.calls == {"find": 1, "write": 1, "finalize": 1}
    count = lambda table: db.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    assert (count("action_runs"), count("artifacts"), count("action_approvals"),
            count("work_cases")) == (1, 1, 1, 1)


def test_changed_inputs_make_a_new_run(db, kit):
    runner, run_id = start(kit, db)
    db.con.execute("UPDATE documents SET hash = 'h2' WHERE id = ?", (db.doc_id,))
    _, other = start(kit, db)
    assert other != run_id


def test_failed_step_resumes_from_where_it_failed(db, kit):
    """T07. 실패 기록이 남고, 반쪽 파일이 없고, 다시 실행하면 그 단계부터 이어진다."""
    kit.fail_write = 1
    runner, run_id = start(kit, db)
    assert runner.execute(run_id) == PARTIAL
    assert states(db, run_id) == ["done", "failed", "pending"]
    error = db.run_steps(run_id)[1]["error"]
    assert "RuntimeError" in error and "2025-11-14" not in error   # 예외 문구는 남기지 않는다
    root = artifacts.actions_root(db)
    assert not root.exists() or not [p for p in root.rglob("*") if p.is_file()]

    assert runner.execute(run_id) == WAITING
    assert kit.calls["find"] == 1 and kit.calls["write"] == 2
    assert len(db.artifacts(run_id=run_id)) == 1


def test_step_stops_being_retried(db, kit):
    kit.fail_write = 99
    runner, run_id = start(kit, db)
    for _ in range(MAX_ATTEMPTS + 2):
        assert runner.execute(run_id) == PARTIAL
    assert kit.calls["write"] == MAX_ATTEMPTS
    assert "여러 번" in db.run_steps(run_id)[1]["error"]


def test_blocked_step_says_what_to_do_and_keeps_its_attempts(db, kit):
    """T10의 틀. 조치가 필요한 실패는 문구를 그대로 남기고 재시도 횟수를 쓰지 않는다."""
    def denied(context, params):
        raise Blocked("자료 폴더를 읽을 권한이 없습니다. 폴더 권한을 확인한 뒤 다시 실행해 주세요.")
    kit.registry._tools["find"] = Tool("find", "1", policy.READ, denied)
    runner, run_id = start(kit, db)
    for _ in range(MAX_ATTEMPTS + 1):
        assert runner.execute(run_id) == FAILED
    step = db.run_steps(run_id)[0]
    assert step["state"] == "blocked" and "권한" in step["error"] and step["attempts"] == 0


def test_interrupted_run_is_recovered(db, kit):
    runner, run_id = start(kit, db)
    db.set_run_state(run_id, "running")
    db.update_run_step(db.run_steps(run_id)[0]["id"], state="running")
    assert db.recover_actions() == 1
    assert db.run(run_id)["state"] == "partial" and states(db, run_id)[0] == "pending"
    assert runner.execute(run_id) == WAITING


def test_stop_request_leaves_finished_steps(db, kit):
    stop = {"now": False}
    runner, run_id = start(kit, db, should_stop=lambda: stop["now"])
    kit.registry._tools["find"] = Tool(
        "find", "1", policy.READ, lambda c, p: stop.update(now=True) or {"documents": []})
    assert runner.execute(run_id) == PARTIAL
    assert states(db, run_id) == ["done", "pending", "pending"]


def test_cancel_keeps_artifacts(db, kit):
    runner, run_id = start(kit, db)
    runner.execute(run_id)
    runner.cancel(run_id)
    assert db.run(run_id)["state"] == CANCELLED
    assert len(db.artifacts(run_id=run_id)) == 1
    assert runner.execute(run_id) == CANCELLED and kit.calls["finalize"] == 0


def test_run_made_by_an_older_tool_is_not_continued(db, kit):
    runner, run_id = start(kit, db)
    kit.registry._tools["find"] = Tool("find", "2", policy.READ, lambda c, p: {})
    with pytest.raises(ToolError, match="도구가 바뀌었습니다"):
        runner.execute(run_id)
    assert db.run(run_id)["state"] == FAILED


# ── 도구가 할 수 있는 것의 한계 ─────────────────────────────────────

def test_read_and_draft_tools_cannot_write_to_the_database(db, kit):
    def sneaky(context, params):
        context.db.con.execute("UPDATE tasks SET name = '바뀜'")
        return {}
    kit.registry._tools["find"] = Tool("find", "1", policy.READ, sneaky)
    runner, run_id = start(kit, db)
    assert runner.execute(run_id) == FAILED
    assert db.task(db.task_id)["name"] == "평가 대응"
    assert "OperationalError" in db.run_steps(run_id)[0]["error"]


def test_originals_are_untouched_and_a_change_fails_the_run(db, kit):
    original = db.source / "계획.txt"
    before = original.read_bytes()
    runner, run_id = start(kit, db)
    runner.execute(run_id)
    assert original.read_bytes() == before

    def vandal(context, params):
        time.sleep(0.02)
        original.write_text("바뀐 내용입니다", encoding="utf-8")
        return {"documents": []}
    kit.registry._tools["find"] = Tool("find", "1", policy.READ, vandal)
    db.con.execute("UPDATE documents SET hash = 'h9'")
    _, other = start(kit, db)
    assert kit.runner(db).execute(other) == FAILED
    assert any(row["action"] == "action.originals" for row in db.recent_audit(50))


# ── 산출물 원장 ─────────────────────────────────────────────────────

def writer(db):
    case_id = cases.open_case(db, db.task_id, 2026)
    run_id, _ = db.create_run("prep", "1", "ledger-test", {}, [], case_id=case_id)
    return artifacts.ArtifactWriter(db, run_id=run_id, step_id=None, case_id=case_id)


def test_same_content_is_one_artifact(db):
    pen = writer(db)
    first = pen.save("checklist", "체크리스트", "c.md", b"one")
    assert pen.save("checklist", "체크리스트", "c.md", b"one") == first
    assert len(db.artifacts(include_superseded=True)) == 1
    assert artifacts.current(db, db.artifact(first))


def test_new_content_is_a_new_version_and_the_old_file_stays(db):
    pen = writer(db)
    first = pen.save("checklist", "체크리스트", "c.md", b"one")
    second = pen.save("checklist", "체크리스트", "c.md", b"two")
    old, new = db.artifact(first), db.artifact(second)
    assert (old["state"], new["version"], new["supersedes_id"]) == ("superseded", 2, first)
    assert Path(old["path"]).read_bytes() == b"one" and Path(new["path"]).read_bytes() == b"two"
    assert [row["id"] for row in db.artifacts()] == [second]


def test_artifacts_stay_inside_the_output_folder(db):
    pen = writer(db)
    saved = db.artifact(pen.save("checklist", "체크리스트", "..\\..\\밖으로.md", b"x"))
    assert Path(saved["path"]).resolve().is_relative_to(artifacts.actions_root(db).resolve())
    with pytest.raises(UnsafeOutputError):
        pen.save("checklist", "빈 이름", " . ", b"x")


def test_writer_refuses_the_source_folder(db, monkeypatch):
    monkeypatch.setattr(artifacts, "actions_root", lambda database: db.source)
    monkeypatch.setattr(artifacts, "run_dir", lambda database, case_id, run_id: db.source)
    with pytest.raises(UnsafeOutputError):
        writer(db).save("checklist", "체크리스트", "c.md", b"x")
    assert sorted(p.name for p in db.source.iterdir()) == ["계획.txt"]


def test_missing_file_is_written_again(db):
    pen = writer(db)
    saved = db.artifact(pen.save("checklist", "체크리스트", "c.md", b"one"))
    Path(saved["path"]).unlink()
    assert pen.save("checklist", "체크리스트", "c.md", b"one") == saved["id"]
    assert Path(saved["path"]).read_bytes() == b"one"


# ── 수행 건 ─────────────────────────────────────────────────────────

def test_case_is_one_per_task_and_year(db):
    first = cases.open_case(db, db.task_id, 2026)
    assert cases.open_case(db, db.task_id, 2026) == first
    assert cases.open_case(db, db.task_id, 2025) != first
    assert cases.title(db.case(first)) == "2026년 평가 대응"


def test_due_date_needs_a_confirmed_source(db):
    """과거 사례에서 짐작한 날짜를 올해 기한으로 넣는 길이 없다."""
    case_id = cases.open_case(db, db.task_id, 2026)
    assert cases.due_text(db.case(case_id)) == "기한 확인 필요"
    with pytest.raises(ValueError):
        cases.set_due(db, case_id, "2026-11-13", source="precedent")
    assert db.case(case_id)["due_date"] is None
    cases.set_due(db, case_id, date(2026, 11, 13), cases.OFFICIAL)
    assert cases.due_text(db.case(case_id)) == "2026-11-13 (공식 기한)"
    cases.set_due(db, case_id, None)
    assert db.case(case_id)["due_state"] == "unknown"


def test_case_cannot_open_on_a_merged_task(db):
    other = db.con.execute("INSERT INTO tasks(name) VALUES ('남는 업무')").lastrowid
    db.con.execute("UPDATE tasks SET merged_into = ? WHERE id = ?", (other, db.task_id))
    with pytest.raises(ValueError):
        cases.open_case(db, db.task_id, 2026)


def test_closing_as_not_this_year_is_kept(db):
    case_id = cases.open_case(db, db.task_id, 2026)
    cases.close(db, case_id, not_this_year=True)
    assert db.case(case_id)["state"] == "not_this_year" and db.case(case_id)["closed_at"]
    assert [row["id"] for row in db.cases(state="open")] == []


# ── 스키마 ──────────────────────────────────────────────────────────

def test_v9_project_upgrades_without_touching_existing_rows(db):
    for table in ("checklist_items", "artifacts", "action_approvals", "action_steps",
                  "action_runs", "recommendations", "signals", "work_cases", "doc_years",
                  "doc_roles"):
        db.con.execute(f"DROP TABLE {table}")
    db.set_meta("schema_version", "9")
    db.close()
    db.init()
    assert db.get_meta("schema_version") == "11"
    assert db.task(db.task_id)["name"] == "평가 대응"
    backup = Path(str(db.path) + ".bak-v9")
    with sqlite3.connect(backup) as old:
        assert old.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "9"
    assert cases.open_case(db, db.task_id, 2026)


def test_evidence_follows_its_owner(db):
    case_id = cases.open_case(db, db.task_id, 2026)
    artifact_id = writer(db).save("comparison", "비교", "비교.md", b"x")
    db.add_evidence("artifact", artifact_id, db.doc_id, "h1", "1쪽", "제출기한: 2025-11-14",
                    "계획.txt", "계획.txt")
    db.con.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
    assert db.con.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
    assert case_id


# ── 뒤에서 돌리기 ───────────────────────────────────────────────────

def test_background_runner_reports_and_releases_its_thread(db, kit):
    from PySide6.QtWidgets import QApplication
    from app.jobs.action_task import ActionRunner

    app = QApplication.instance() or QApplication([])
    _, run_id = start(kit, db)
    job = ActionRunner(db.path, runner_factory=lambda database, **extra: kit.runner(database, **extra))
    finished, steps = [], []
    job.done.connect(lambda *args: finished.append(args))
    job.progress.connect(lambda *args: steps.append(args[3]))
    try:
        assert job.start(run_id) and not job.start(run_id)
        deadline = time.monotonic() + 5
        while not finished and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert finished == [(run_id, WAITING, "")]
        assert steps == ["자료 찾기", "초안 쓰기"] and not job.running
    finally:
        job.stop()
    assert json.loads(db.run_steps(run_id)[1]["output"])["artifact_id"]
