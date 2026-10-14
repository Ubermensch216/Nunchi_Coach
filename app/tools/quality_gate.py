"""업무 분석 품질 관문 — 잘못된 안내가 다시 생기지 않았는지 프로젝트마다 잰다 (개선계획서 P2·8장).

    python -m app.tools.quality_gate --db <project.db> [--truth 기준.json] [--json 결과.json] [--basis 2026-10-14]

규제개혁 비교연구에서 찾은 오류는 '틀린 값'보다 '틀린 행동'이었다. 남의 건의 절차를 지금 할
일로, 다른 위원회의 임기를 이 업무의 만료로, AI 보고서의 지표 설명을 계획의 목적으로, 서식의
빈칸을 제출처로 안내했다. 고친 뒤에도 자료가 바뀌거나 규칙을 손대면 다시 생길 수 있다. 이 도구는
그 꼴들을 **정답 없이도** 잴 수 있는 불변 조건으로 검사한다.

  G1 지난 회차의 적힌 절차를 확인 없이 '진행할 수 있습니다'로 안내한 업무
  G2 다른 기관이 하는 단계를 우리 행동으로 안내한 업무
  G3 서식의 빈칸('0000과', '000-000-0000')이 값이 된 항목
  G4 참고자료(AI 생성·발간물·압축 묶음)만 근거인 업무 값
  G5 건별 목록(한 행이 한 건인 긴 시트)의 칸에서 읽은 절차·준비물
  G6 자동으로 읽었는데 근거가 없는 항목
  G7 평가 기준이 초안인데 '확정본 확인' 공백이 없는 업무
  G8 마지막 폴더 조사가 실패했는데 결과가 '준비됨'으로 보이는 상태

정답 파일(선택)은 업무별 기대를 적은 JSON이다. 사람이 원문으로 정한 것만 적는다 — 이 도구의 출력을
정답으로 옮겨 적지 않는다.

    [{"task": "규제혁신 과제 제출",               업무 이름의 일부
      "must_not_contain": ["토지분할", "외국인등록증"],   항목·단계·다음 행동 어디에도 없어야 할 말
      "must_contain": [],                               어딘가에 있어야 할 말
      "first_action_not": "진행할 수 있습니다"}]          첫 다음 행동에 없어야 할 말

DB는 복사본에서 쓰기를 권한다. 이 도구는 읽기만 하지만, 지금 쓰는 프로젝트를 열어 둔 채 재지 않는다.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .console import force_utf8


@dataclass
class Gate:
    key: str
    title: str
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def measure(db, truth: list[dict] | None = None) -> dict:
    """불변 조건과 (있으면) 정답 기대를 잰다. 프로젝트 DB를 바꾸지 않는다."""
    from ..core import authority, guidance, initial_analysis, reconstruction as rc, workcard, workstate as ws

    tasks = list(db.tasks())
    gates = {key: Gate(key, title) for key, title in (
        ("G1", "지난 회차 절차를 확인 없이 진행 안내"), ("G2", "다른 기관 단계를 우리 행동으로 안내"),
        ("G3", "서식 빈칸이 값이 된 항목"), ("G4", "참고자료만 근거인 업무 값"),
        ("G5", "건별 목록 칸에서 읽은 절차·준비물"), ("G6", "근거 없는 자동 항목"),
        ("G7", "초안 평가 기준에 확정본 확인 없음"), ("G8", "폴더 조사 실패인데 '준비됨'"))}
    cards = {}
    for task in tasks:
        card = workcard.build(db, task["id"])
        cards[task["id"]] = card
        name = task["name"]
        steps = {s["id"]: s for s in card.stated_steps}
        first_do = next((a for a in card.actions if a["type"] == "do" and a["step_id"] in steps), None)
        if first_do:
            step = steps[first_do["step_id"]]
            if step["applicability"] == ws.PAST and step["human_state"] not in ws.CONFIRMED_BY \
                    and step["procedure_kind"] == ws.PROCEDURE_STATED:
                gates["G1"].failures.append(f"{name}: '{step['label']}'")
            if step["actor"] and step["actor"] != guidance.OURS:
                gates["G2"].failures.append(f"{name}: '{step['label']}' ({step['actor']})")
        for item in card.todo + card.prepare:
            if item["kind"] != rc.FORM and item["origin"] == "auto" and authority.is_placeholder(item["content"]):
                gates["G3"].failures.append(f"{name}: {item['slot']} = {item['content']}")
            if item["origin"] == "auto" and item["basis_state"] == ws.STATED and not item["evidence"]:
                gates["G6"].failures.append(f"{name}: {item['slot']}")
        if any(i["kind"] == rc.EVALUATION and "초안" in i["content"] for i in card.prepare) \
                and not any(g["gap_key"] == "evaluation:confirm" for g in card.gaps):
            gates["G7"].failures.append(name)

    _reference_values(db, gates["G4"], authority, rc, ws)
    _record_rows(db, gates["G5"], rc)
    outcome = initial_analysis.outcome(db)
    if initial_analysis.stale_sources(db) and outcome.state == "ready":
        gates["G8"].failures.append(outcome.title)

    report = {
        "tasks": len(tasks),
        "items": sum(len(c.todo) + len(c.prepare) for c in cards.values()),
        "stated_steps": sum(len(c.stated_steps) for c in cards.values()),
        "gaps": sum(len(c.gaps) for c in cards.values()),
        "matters": sum(len(c.matters) for c in cards.values()),
        "unread": dict(db.con.execute(
            "SELECT parse_status, COUNT(*) FROM documents WHERE missing_since IS NULL "
            "AND parse_status NOT IN ('ok','partial','pending','skipped') GROUP BY parse_status").fetchall()),
        "gates": [{"key": g.key, "title": g.title, "passed": g.passed, "count": len(g.failures),
                   "examples": g.failures[:5]} for g in gates.values()],
    }
    if truth is not None:
        report["truth"] = _against_truth(db, tasks, cards, truth)
    report["passed"] = all(g.passed for g in gates.values()) and all(
        t["passed"] for t in report.get("truth", []))
    return report


def _reference_values(db, gate: Gate, authority, rc, ws) -> None:
    """업무 값(기한·제출처·목적·절차·준비물·금액·담당)의 근거가 모두 참고자료인 항목."""
    kinds = (rc.DEADLINE, rc.RECIPIENT, rc.PURPOSE, rc.PROCEDURE, rc.INPUT, rc.AMOUNT, rc.CONTACT)
    marks = ", ".join("?" * len(kinds))
    for row in db.con.execute(
            f"SELECT i.id, i.slot, t.name FROM task_items i JOIN tasks t ON t.id = i.task_id "
            f"WHERE i.excluded = 0 AND i.origin = 'auto' AND i.kind IN ({marks})", kinds).fetchall():
        docs = [r["doc_id"] for r in db.con.execute(
            "SELECT doc_id FROM evidence WHERE owner_kind = 'item' AND owner_id = ? AND doc_id IS NOT NULL", (row["id"],))]
        if not docs:
            continue
        reference = set(authority.reference_only(db, docs))
        reference |= {r["id"] for r in db.con.execute(
            f"SELECT id FROM documents WHERE parser = 'archive' AND id IN ({', '.join('?' * len(docs))})", docs)}
        if set(docs) <= reference:
            gate.failures.append(f"{row['name']}: {row['slot']}")


def _record_rows(db, gate: Gate, rc) -> None:
    """절차·준비물 항목의 근거가 한 행이 한 건인 긴 시트의 칸인가."""
    sheets_of: dict[int, set] = {}
    for row in db.con.execute(
            "SELECT i.id, i.slot, t.name, e.doc_id, e.locator FROM task_items i JOIN tasks t ON t.id = i.task_id "
            "JOIN evidence e ON e.owner_kind = 'item' AND e.owner_id = i.id "
            "WHERE i.excluded = 0 AND i.origin = 'auto' AND i.kind IN (?, ?)", (rc.PROCEDURE, rc.INPUT)).fetchall():
        if row["doc_id"] not in sheets_of:
            sections = db.con.execute("SELECT locator, text, meta FROM document_sections WHERE doc_id = ?",
                                      (row["doc_id"],)).fetchall()
            sheets_of[row["doc_id"]] = rc._record_sheets(sections)
        if any((row["locator"] or "").startswith(f"'{sheet}'") for sheet in sheets_of[row["doc_id"]]):
            gate.failures.append(f"{row['name']}: {row['slot']}")


def _against_truth(db, tasks, cards, truth: list[dict]) -> list[dict]:
    out = []
    for expected in truth:
        task = next((t for t in tasks if expected["task"] in t["name"]), None)
        result = {"task": expected["task"], "passed": False, "problems": []}
        if task is None:
            result["problems"].append("업무를 찾지 못함")
            out.append(result)
            continue
        card = cards[task["id"]]
        text = "\n".join([*(f"{i['slot']} {i['content']}" for i in card.todo + card.prepare),
                          *(s["label"] for s in card.stated_steps), *(a["text"] for a in card.actions)])
        for word in expected.get("must_not_contain", []):
            if word in text:
                result["problems"].append(f"있으면 안 되는 말: {word}")
        for word in expected.get("must_contain", []):
            if word not in text:
                result["problems"].append(f"있어야 할 말이 없음: {word}")
        first = card.actions[0]["text"] if card.actions else ""
        if expected.get("first_action_not") and expected["first_action_not"] in first:
            result["problems"].append(f"첫 행동에 '{expected['first_action_not']}'")
        result["passed"] = not result["problems"]
        out.append(result)
    return out


def show(report: dict) -> None:
    print(f"업무 {report['tasks']} · 항목 {report['items']} · 적힌 절차 단계 {report['stated_steps']} · "
          f"공백 {report['gaps']} · 건 {report['matters']}")
    if report["unread"]:
        print("읽지 못한 문서: " + ", ".join(f"{k} {v}" for k, v in report["unread"].items()))
    for gate in report["gates"]:
        mark = "통과" if gate["passed"] else f"실패 {gate['count']}"
        print(f"  {gate['key']} {gate['title']}: {mark}")
        for example in gate["examples"]:
            print(f"      - {example}")
    for result in report.get("truth", []):
        print(f"  [기준] {result['task']}: {'통과' if result['passed'] else '; '.join(result['problems'])}")
    print("판정: " + ("통과" if report["passed"] else "실패"))


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description="업무 분석 품질 관문")
    parser.add_argument("--db", required=True, help="project.db 경로(복사본 권장)")
    parser.add_argument("--truth", help="업무별 기대 JSON")
    parser.add_argument("--json", help="결과를 저장할 JSON 경로")
    parser.add_argument("--restore", action="store_true",
                        help="재기 전에 규칙으로 업무 복원을 다시 돌린다(DB를 바꾼다 — 복사본에서만)")
    parser.add_argument("--basis", help="복원 기준일(YYYY-MM-DD)")
    args = parser.parse_args(argv)
    from ..db import Database

    path = Path(args.db)
    if not path.is_file():
        print(f"DB가 없습니다: {path}")
        return 2
    db = Database(path, readonly=not args.restore)
    try:
        if args.restore:
            from ..core import reconstruction

            db.init()
            reconstruction.restore(db, date.fromisoformat(args.basis) if args.basis else None, statements=False)
        truth = json.loads(Path(args.truth).read_text(encoding="utf-8")) if args.truth else None
        report = measure(db, truth)
    finally:
        db.close()
    show(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
