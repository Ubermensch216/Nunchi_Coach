"""업무 분석 결과 보고 — 무엇을 업무로 보았고 왜 그렇게 보았는지 한눈에 본다.

    python -m app.tools.task_report --db <project.db 경로> [--json 경로] [--truth 정답.csv]

왜 필요한가
  분석 결과가 합당한지 따져 보려면 지금은 프로젝트 DB를 직접 열어 질의해야 한다.
  진단 로그에는 시작·종료만 남는다(본문·경로를 남기지 않기로 했으므로). 이 도구는
  DB에 있는 결과를 사람이 읽는 표로 낸다.

무엇을 내는가
  · 읽기 범위      읽은 문서, 읽지 못한 문서와 그 까닭, 대상이 아닌 파일
  · 업무           문서 수, 어느 폴더에서 왔는지, 반복 주기, 처리 단계, 공백
  · 시점           연도만 아는 문서, 근거별 건수, 의심스러운 연도
  · 갈래           문서 갈래별 건수
  · (정답이 있으면) 실제 업무 하나가 몇 개로 흩어졌고, 한 업무에 몇 개가 섞였는지

정답 파일은 `경로의 앞부분,업무` 두 칸짜리 CSV다. 자료 폴더 기준 상대 경로가 그
앞부분으로 시작하는 문서가 그 업무다. 위에서부터 먼저 맞는 줄을 쓴다.

DB는 읽기 전용으로 연다. 본문은 싣지 않는다 — 파일 이름과 폴더 이름은 싣는다.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path, PureWindowsPath

from .console import force_utf8


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description="업무 분석 결과 보고")
    parser.add_argument("--db", required=True, help="project.db 경로")
    parser.add_argument("--json", help="같은 내용을 JSON으로 저장할 경로")
    parser.add_argument("--truth", help="정답 CSV(경로의 앞부분,업무)")
    args = parser.parse_args(argv)

    path = Path(args.db)
    if not path.is_file():
        print(f"DB가 없습니다: {path}")
        return 2
    con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        report = build(con, _load_truth(Path(args.truth)) if args.truth else None)
    finally:
        con.close()
    show(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON 저장: {args.json}")
    return 0


def build(con: sqlite3.Connection, truth: list[tuple[str, str]] | None = None) -> dict:
    roots = {row["id"]: row["path"] for row in con.execute("SELECT id, path FROM sources")}

    def relative(row) -> str:
        try:
            return str(PureWindowsPath(row["path"]).relative_to(PureWindowsPath(roots[row["source_id"]])))
        except (KeyError, ValueError):
            return row["path"]

    documents = con.execute(
        "SELECT id, source_id, path, parse_status, eff_year, eff_month, eff_date_kind, "
        "eff_precision FROM documents WHERE missing_since IS NULL").fetchall()
    read = [d for d in documents if d["parse_status"] in ("ok", "partial")]
    reading = {
        "files": len(documents),
        "read": len(read),
        "by_status": dict(Counter(d["parse_status"] for d in documents).most_common()),
        "stages": [dict(row) for row in con.execute(
            "SELECT stage, state, done, total, note FROM pipeline_stage_results")],
    }

    dated = [d for d in documents if d["eff_year"]]
    dating = {
        "by_kind": dict(Counter(d["eff_date_kind"] for d in dated).most_common()),
        "by_precision": dict(Counter(d["eff_precision"] for d in dated).most_common()),
        "year_only_with_month": sum(1 for d in dated if d["eff_precision"] == "year" and d["eff_month"]),
        "years": dict(sorted(Counter(d["eff_year"] for d in dated).items())),
    }

    tasks = []
    assigned: dict[int, int] = {}
    for task in con.execute(
            "SELECT id, name, description, origin, status, confidence FROM tasks "
            "WHERE not_a_task = 0 AND merged_into IS NULL ORDER BY id"):
        members = con.execute(
            "SELECT d.id, d.source_id, d.path, d.parse_status, td.evidence FROM task_docs td "
            "JOIN documents d ON d.id = td.doc_id WHERE td.task_id = ?", (task["id"],)).fetchall()
        for member in members:
            assigned[member["id"]] = task["id"]
        top = Counter(PureWindowsPath(relative(m)).parts[0] if len(PureWindowsPath(relative(m)).parts) > 1
                      else "(자료 폴더 바로 아래)" for m in members)
        cycle = con.execute(
            "SELECT kind, months, years_observed, confidence FROM task_cycles WHERE task_id = ? "
            "ORDER BY (decided_by = 'user') DESC, years_observed DESC LIMIT 1", (task["id"],)).fetchone()
        steps = {}
        for row in con.execute(
                "SELECT year, label, day_hint FROM task_steps WHERE task_id = ? ORDER BY year, ordinal",
                (task["id"],)):
            steps.setdefault(row["year"], []).append(
                row["label"] + (f" ({row['day_hint']})" if row["day_hint"] else ""))
        tasks.append({
            "id": task["id"], "name": task["name"], "confidence": task["confidence"],
            "status": task["status"], "description": task["description"],
            "documents": len(members),
            "unread": sum(1 for m in members if m["parse_status"] not in ("ok", "partial")),
            "by": dict(Counter(m["evidence"] or "본문 유사도" for m in members).most_common()),
            "folders": dict(top.most_common(4)),
            "cycle": dict(cycle) if cycle else None,
            "steps": steps,
            "items": dict(Counter(row["kind"] for row in con.execute(
                "SELECT kind FROM task_items WHERE task_id = ? AND excluded = 0", (task["id"],)))),
            "gaps": dict(Counter(row["field"] for row in con.execute(
                "SELECT field FROM info_gaps WHERE task_id = ? AND state = 'open'", (task["id"],)))),
            "reading": [row["filename"] for row in con.execute(
                "SELECT d.filename FROM task_reading r JOIN documents d ON d.id = r.doc_id "
                "WHERE r.task_id = ? ORDER BY r.ordinal LIMIT 3", (task["id"],))],
        })
    tasks.sort(key=lambda item: -item["documents"])

    # 연간 일정 — 문서에 적힌 일정·기한을 달별로 모은다. 지난 회차의 날짜이므로
    # '올해 이 날'이 아니라 '지난번에는 이때 했다'로 읽는다.
    calendar: dict[int, list[str]] = {}
    for row in con.execute(
            "SELECT t.name, i.slot, i.content FROM task_items i JOIN tasks t ON t.id = i.task_id "
            "WHERE i.kind = 'deadline' AND i.excluded = 0 AND t.not_a_task = 0 "
            "AND t.merged_into IS NULL ORDER BY t.id, i.id"):
        what = row["slot"].removeprefix("일정 · ")
        for value in row["content"].split(" / "):
            month = _month_of(value)
            if month:
                calendar.setdefault(month, []).append(f"{value} · {what} ({row['name']})")

    report = {
        "reading": reading, "dating": dating, "tasks": tasks,
        "calendar": {month: sorted(set(lines)) for month, lines in sorted(calendar.items())},
        "task_count": len(tasks),
        "single_document_tasks": sum(1 for t in tasks if t["documents"] == 1),
        "unassigned_read": sum(1 for d in read if d["id"] not in assigned),
        "roles": dict(Counter(row["role"] for row in con.execute("SELECT role FROM doc_roles")).most_common()),
        "gap_total": con.execute("SELECT COUNT(*) FROM info_gaps WHERE state = 'open'").fetchone()[0],
    }
    if truth:
        report["against_truth"] = _compare(documents, assigned, truth, relative,
                                           {t["id"]: t["name"] for t in tasks})
    return report


def _month_of(value: str) -> int | None:
    import re

    found = re.match(r"\s*(?:\d{4}-(\d{2})-\d{2}|(\d{1,2})월)", value)
    if not found:
        return None
    month = int(found.group(1) or found.group(2))
    return month if 1 <= month <= 12 else None


def _load_truth(path: Path) -> list[tuple[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [(row[0].strip(), row[1].strip()) for row in csv.reader(handle)
                if len(row) >= 2 and row[0].strip() and not row[0].startswith("#")]


def _compare(documents, assigned, truth, relative, names) -> dict:
    spread: dict[str, Counter] = {}
    mixed: dict[int, Counter] = {}
    for document in documents:
        if document["parse_status"] == "skipped":
            continue
        where = relative(document)
        label = next((task for prefix, task in truth if where.startswith(prefix)), None)
        if label is None:
            continue
        task_id = assigned.get(document["id"])
        spread.setdefault(label, Counter())[task_id] += 1
        if task_id is not None:
            mixed.setdefault(task_id, Counter())[label] += 1
    rows = []
    for label, counter in sorted(spread.items()):
        placed = {k: v for k, v in counter.items() if k is not None}
        total = sum(counter.values())
        best = max(placed.values(), default=0)
        rows.append({"task": label, "documents": total, "unassigned": counter.get(None, 0),
                     "proposed_tasks": len(placed), "largest_share": round(best / total, 2) if total else 0})
    return {
        "per_real_task": rows,
        "max_spread": max((row["proposed_tasks"] for row in rows), default=0),
        "max_mixed": max((len(counter) for counter in mixed.values()), default=0),
        "mixed": {names.get(task_id, str(task_id)): dict(counter.most_common())
                  for task_id, counter in mixed.items() if len(counter) > 1},
    }


def show(report: dict) -> None:
    reading = report["reading"]
    print(f"파일 {reading['files']:,}개 · 읽음 {reading['read']:,}건")
    print("  상태별: " + " · ".join(f"{k} {v:,}" for k, v in reading["by_status"].items()))
    for stage in reading["stages"]:
        print(f"  [{stage['state']}] {stage['stage']}: {stage['note']}")

    dating = report["dating"]
    print("\n시점")
    print("  근거별: " + " · ".join(f"{k} {v:,}" for k, v in dating["by_kind"].items()))
    print("  정밀도: " + " · ".join(f"{k} {v:,}" for k, v in dating["by_precision"].items()))
    print(f"  연도만 아는데 달이 채워진 문서: {dating['year_only_with_month']:,}건")
    print("  연도: " + " · ".join(f"{k}:{v}" for k, v in dating["years"].items()))

    print(f"\n업무 {report['task_count']}개 (문서 한 건짜리 {report['single_document_tasks']}개) · "
          f"읽었으나 미분류 {report['unassigned_read']:,}건 · 열린 공백 {report['gap_total']:,}건")
    for task in report["tasks"]:
        print(f"\n■ {task['name']}  — 문서 {task['documents']:,}건"
              + (f" (본문 못 읽음 {task['unread']:,})" if task["unread"] else "")
              + f" · 확신 {task['confidence']} · {task['status']}")
        print("    배정 근거: " + " · ".join(f"{k} {v}" for k, v in task["by"].items()))
        print("    폴더: " + " · ".join(f"{k} {v}" for k, v in task["folders"].items()))
        cycle = task["cycle"]
        if cycle:
            when = "매월" if cycle["kind"] == "monthly" else f"{cycle['months']}월"
            print(f"    주기: {cycle['kind']} {when} · {cycle['years_observed']}개 해 관찰 · {cycle['confidence']}")
        for year, labels in task["steps"].items():
            shown = " → ".join(labels[:12]) + (f" … 외 {len(labels) - 12}" if len(labels) > 12 else "")
            print(f"    {year}년 {len(labels)}단계: {shown}")
        if task["items"]:
            print("    항목: " + " · ".join(f"{k} {v}" for k, v in task["items"].items()))
        if task["gaps"]:
            print("    공백: " + " · ".join(f"{k} {v}" for k, v in task["gaps"].items()))
        if task["reading"]:
            print("    먼저 읽을 문서: " + " / ".join(task["reading"]))

    if report.get("calendar"):
        print("\n연간 일정 (문서에 적힌 지난 회차의 일정·기한)")
        for month, lines in report["calendar"].items():
            print(f"  {month}월")
            for line in lines:
                print(f"    {line}")

    print("\n문서 갈래: " + " · ".join(f"{k} {v:,}" for k, v in report["roles"].items()))

    truth = report.get("against_truth")
    if truth:
        print("\n정답과 견주기 (실제 업무 → 제안 업무 수 · 가장 큰 묶음의 비중 · 미분류)")
        for row in truth["per_real_task"]:
            print(f"  {row['task']}: 문서 {row['documents']} → {row['proposed_tasks']}개 · "
                  f"{row['largest_share']:.0%} · 미분류 {row['unassigned']}")
        print(f"  한 업무가 흩어진 최대 {truth['max_spread']}개 · 한 제안에 섞인 최대 {truth['max_mixed']}개")
        for name, labels in truth["mixed"].items():
            print(f"    섞임 — {name}: " + ", ".join(f"{k} {v}" for k, v in labels.items()))


if __name__ == "__main__":
    raise SystemExit(main())
