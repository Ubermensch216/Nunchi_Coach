"""선제 감지 평가 — 내야 할 제안을 냈는가, 내지 말아야 할 제안을 안 냈는가 (구현계획서 E2).

    python -m app.tools.proactive_eval [--json 파일]

tests/fixtures/proactive_scenarios/cases.json의 업무들(주기와 문서의 연·월만 합성)을 빈
프로젝트에 넣고 감지를 한 번 돌린다. 모델을 부르지 않는다.

읽는 법
  · 제안이 많은 것은 장점이 아니다. **내지 말아야 할 곳에서 낸 비율**이 문턱(0.10) 이하여야
    하고, 지금 합성 사례에서는 0이어야 한다.
  · 사례는 규칙을 짜기 전에 적어 동결했지만 같은 개발 AI가 만들었다. 실제 기관 자료에서의
    거짓 알림률을 말해 주지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..db import Database
from ..proactive import recommend

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "proactive_scenarios"
FALSE_ALARM_LIMIT = 0.10


@dataclass
class Report:
    fire_hit: int = 0
    fire_total: int = 0
    quiet_wrong: int = 0
    quiet_total: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.fire_hit / self.fire_total if self.fire_total else 1.0

    @property
    def false_alarm_rate(self) -> float:
        return self.quiet_wrong / self.quiet_total if self.quiet_total else 0.0


def seed(db: Database, spec: dict) -> int:
    """사례 하나를 업무·문서·주기로 넣는다. 업무 id."""
    source = db.con.execute("SELECT id FROM sources LIMIT 1").fetchone()
    source_id = source["id"] if source else db.con.execute(
        "INSERT INTO sources(path) VALUES ('합성')").lastrowid
    task_id = db.con.execute(
        "INSERT INTO tasks(name, origin, status, not_a_task) VALUES (?, 'ai', 'proposed', ?)",
        (spec["name"], int(spec.get("not_a_task", False)))).lastrowid
    if spec.get("merged"):
        other = db.con.execute("INSERT INTO tasks(name) VALUES (?)", (spec["name"] + " (대상)",)).lastrowid
        db.con.execute("UPDATE tasks SET merged_into = ? WHERE id = ?", (other, task_id))
    for number, (year, month, *kind) in enumerate(spec["docs"]):
        stamp = f"{year}-{month:02d}-05"
        doc_id = db.con.execute(
            "INSERT INTO documents(source_id, path, filename, ext, hash, eff_year, eff_month, eff_date, "
            "eff_date_kind, eff_precision, parse_status) VALUES (?, ?, ?, '.hwpx', ?, ?, ?, ?, ?, 'day', 'ok')",
            (source_id, f"/합성/{task_id}/{number}.hwpx", f"{year}_{spec['name']}_{number}.hwpx",
             f"{task_id}-{number}", year, month, stamp, kind[0] if kind else "body")).lastrowid
        db.con.execute("INSERT INTO task_docs(task_id, doc_id) VALUES (?, ?)", (task_id, doc_id))
    cycle = spec.get("cycle")
    if cycle:
        db.con.execute(
            "INSERT INTO task_cycles(task_id, kind, months, years_observed, confidence, decided_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, cycle["kind"], cycle["months"], cycle["years_observed"], cycle["confidence"],
             cycle.get("decided_by", "ai")))
    case = spec.get("case")
    if case:
        case_id, _ = db.open_case(task_id, case["year"], "")
        db.set_case_state(case_id, case["state"])
    return task_id


def judge(db: Database, specs: list[dict], today: date) -> Report:
    report = Report()
    ids = {seed(db, spec): spec for spec in specs}
    recommend.scan(db, today)
    fired = {rec["task_id"] for rec in db.recommendations(("new", "seen"))}
    for task_id, spec in ids.items():
        if spec["expect"] == "fire":
            report.fire_total += 1
            if task_id in fired:
                report.fire_hit += 1
            else:
                report.problems.append(f"놓친 제안: {spec['name']} — {spec['why']}")
        else:
            report.quiet_total += 1
            if task_id in fired:
                report.quiet_wrong += 1
                report.problems.append(f"내지 말아야 할 제안을 냈다: {spec['name']} — {spec['why']}")
    return report


def run(work: Path | None = None) -> Report:
    data = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))
    root = work or Path(tempfile.mkdtemp(prefix="nunchi-proactive-eval-"))
    db = Database(root / "project.db")
    db.init()
    try:
        return judge(db, data["cases"], date.fromisoformat(data["today"]))
    finally:
        db.close()


def to_text(report: Report) -> str:
    lines = ["■ 선제 감지 평가 (합성 사례 · 모델 없음)",
             f"  내야 할 제안       {report.fire_hit}/{report.fire_total}",
             f"  내지 말아야 할 제안 {report.quiet_wrong}/{report.quiet_total}  "
             f"(거짓 알림률 {report.false_alarm_rate:.0%}, 문턱 {FALSE_ALARM_LIMIT:.0%})"]
    lines += [f"    ✕ {problem}" for problem in report.problems]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", type=Path, help="결과를 JSON으로 저장")
    args = parser.parse_args(argv)
    report = run()
    print(to_text(report))
    if args.json:
        args.json.write_text(json.dumps(
            {"fire": [report.fire_hit, report.fire_total], "quiet_wrong": [report.quiet_wrong, report.quiet_total],
             "problems": report.problems}, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
