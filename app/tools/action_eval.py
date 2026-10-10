"""행동형 업무 비서 평가 — 합성 시나리오로 재는 정확도 (구현계획서 8절).

    python -m app.tools.action_eval [--work 폴더] [--json 파일]

tests/fixtures/action_scenarios/corpus.json으로 실제 파일(hwpx·xlsx·txt)을 만들어
빈 프로젝트에 넣고, 분석한 뒤, answers.json과 견준다. 생성 모델은 부르지 않는다.

읽는 법
  · 전부 합성 자료다. 실제 기관 문서에서의 성능을 말하지 않는다.
  · 정답은 도구를 짜기 전에 적어 동결했다. 결과를 보고 규칙을 고쳤다면 그 뒤의
    수치는 개발용 수치다.
  · 업무 묶기(임베딩 군집)는 이 평가의 대상이 아니다. 시나리오가 적어 둔 배정을
    그대로 쓴다 — 일부러 틀리게 섞은 배정이 들어 있다.

지금 재는 것: 문서 역할·연도 갈래·판 묶음·지표 표(1단계). 후보 찾기·비교·서식·
체크리스트는 해당 도구가 들어올 때 덧붙인다.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..core import docrole, docyears, indicators, reconstruction as rc, versions
from . import scenario_eval

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "action_scenarios"


@dataclass
class Result:
    scenario: str
    scores: dict[str, tuple[int, int]] = field(default_factory=dict)   # 이름 → (맞은 수, 전체)
    problems: list[str] = field(default_factory=list)

    def score(self, name: str, hit: int, total: int) -> None:
        self.scores[name] = (hit, total)

    def ratio(self, name: str) -> float:
        hit, total = self.scores.get(name, (0, 0))
        return hit / total if total else 1.0


# ── 자료 만들기 ─────────────────────────────────────────────────────

def write_corpus(root: Path, documents: list[dict]) -> list[Path]:
    """시나리오의 문서를 실제 파일로 만든다. 제품은 이 폴더만 본다."""
    made = []
    for document in documents:
        path = root / document["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".hwpx":
            scenario_eval._write_hwpx(path, document["blocks"])
        elif path.suffix == ".xlsx":
            _write_xlsx(path, document["blocks"])
        else:
            path.write_text("\n\n".join(block for block in document["blocks"]
                                        if isinstance(block, str)), encoding="utf-8")
        made.append(path)
    return made


def _write_xlsx(path: Path, blocks: list) -> None:
    import openpyxl

    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for block in blocks:
        sheet = workbook.create_sheet(block["sheet"][:31])
        for row in block["rows"]:
            sheet.append(row)
    workbook.save(path)


# ── 제품 돌리기 ─────────────────────────────────────────────────────

def analyse(project: Path, source: Path, scenario: dict, basis: date, documents: list[dict]):
    """빈 프로젝트에 원문 폴더를 넣고 분석한다. 사람의 입력은 없다.

    (db, 이 업무의 id, 경로 → 문서 id)를 돌려준다.
    """
    from PySide6.QtCore import QCoreApplication

    from ..core import handover_release
    from ..db import Database
    from ..jobs import pipeline as pipeline_module

    QCoreApplication.instance() or QCoreApplication([])
    db = Database(project / "project.db")
    db.init()
    if not db.sources():
        db.add_source(source)
    worker = pipeline_module.Pipeline(db.path)
    worker._scan(db)
    worker._hash(db)
    worker._parse(db)
    worker._date(db)

    def task(name: str) -> int:
        row = db.con.execute("SELECT id FROM tasks WHERE name = ?", (name,)).fetchone()
        return row["id"] if row else db.con.execute(
            "INSERT INTO tasks(name, origin, confidence) VALUES (?, 'seeded', 'high')",
            (name,)).lastrowid

    owners = {"task": task(scenario["task"]), "other": task(scenario["other_task"])}
    by_path = {}
    for row in db.con.execute("SELECT id, path FROM documents").fetchall():
        by_path[Path(row["path"]).relative_to(source).as_posix()] = row["id"]
    for document in documents:
        owner = owners.get(document["assigned"])
        if owner is not None:
            db.con.execute(
                "INSERT OR IGNORE INTO task_docs(task_id, doc_id, origin, confidence) "
                "VALUES (?, ?, 'ai', 'high')", (owner, by_path[document["path"]]))
    worker._roles(db)
    worker._cycles(db)
    worker._steps(db)
    rc.restore(db, basis)
    handover_release.autosave(db)
    return db, owners["task"], by_path


# ── 채점 ────────────────────────────────────────────────────────────

def judge_memory(db, answer: dict, by_path: dict[str, int], result: Result) -> None:
    """1단계 — 역할·연도·판 묶음·지표 표."""
    stored = docrole.roles(db, by_path.values())
    hit = total = held = 0
    for path, expected in answer["roles"].items():
        got = stored.get(by_path[path], {}).get("role", docrole.OTHER)
        if expected == docrole.OTHER:
            continue
        total += 1
        if got == expected:
            hit += 1
        elif got == docrole.OTHER:
            held += 1                          # 보류는 오답으로 적지 않되 맞은 것도 아니다
        else:
            result.problems.append(f"역할: {path} — {expected}여야 하는데 {got}")
    result.score("role", hit, total)
    result.score("role_held", held, total)

    marks = docyears.years(db, by_path.values())
    hit = total = 0
    for path, expected in answer["years"].items():
        got = {kind: years for kind, years in marks.get(by_path[path], {}).items()
               if kind != docyears.AUTHORED}
        total += 1
        if got == expected:
            hit += 1
        else:
            result.problems.append(f"연도: {path} — {expected}여야 하는데 {got}")
    result.score("years", hit, total)

    hit = total = 0
    for expected in answer["revisions"]:
        total += 1
        ids = {by_path[path] for path in expected["members"]}
        groups = [group for group in versions.groups_for(db, ids)
                  if {member["doc_id"] for member in group["members"]} == ids]
        if len(groups) != 1:
            result.problems.append(f"판 묶음: 구성원 {len(ids)}건이 한 묶음으로 서지 않았다")
        elif not expected["latest_decided"] and groups[0]["latest_doc_id"] is not None:
            result.problems.append("판 묶음: 단서가 엇갈리는데 최신본 후보를 세웠다")
        else:
            hit += 1
    result.score("revisions", hit, total)

    expected = answer["indicators"]
    hit = total = 0
    for year in (expected["earlier"], expected["later"]):
        names = {change["name"] for change in expected["changes"]
                 if (change["status"] != "added" if year == expected["earlier"]
                     else change["status"] != "removed")}
        read = set()
        for path, doc_id in by_path.items():
            if stored.get(doc_id, {}).get("role") == docrole.CRITERIA and path.startswith(f"{year}/") \
                    and "규제혁신" in path:
                read |= {row.name for row in indicators.read_document(db, doc_id)}
        total += len(names)
        hit += len(names & read)
        for name in sorted(read - names):
            result.problems.append(f"지표 표: {year}년에 없는 지표를 읽었다 — {name}")
        for name in sorted(names - read):
            result.problems.append(f"지표 표: {year}년 지표를 읽지 못했다 — {name}")
    result.score("indicator_rows", hit, total)


def run(work: Path | None = None) -> list[Result]:
    corpus = json.loads((FIXTURES / "corpus.json").read_text(encoding="utf-8"))
    answers = json.loads((FIXTURES / "answers.json").read_text(encoding="utf-8"))["scenarios"]
    basis = date.fromisoformat(corpus["basis"])
    root = work or Path(tempfile.mkdtemp(prefix="nunchi-action-eval-"))
    results = []
    try:
        for scenario in corpus["scenarios"]:
            folder = root / scenario["id"]
            source = folder / "자료"
            write_corpus(source, scenario["documents"])
            before = scenario_eval._fingerprint(source)
            db, _task_id, by_path = analyse(folder / "project", source, scenario, basis,
                                            scenario["documents"])
            result = Result(scenario["id"])
            try:
                judge_memory(db, answers[scenario["id"]], by_path, result)
                result.score("originals", int(scenario_eval._fingerprint(source) == before), 1)
            finally:
                db.close()
            results.append(result)
    finally:
        if work is None:
            shutil.rmtree(root, ignore_errors=True)
    return results


def to_text(results: list[Result]) -> str:
    lines = ["■ 행동형 업무 비서 평가 (합성 자료 · 모델 없음)"]
    for result in results:
        lines.append(f"  [{result.scenario}]")
        for name, (hit, total) in result.scores.items():
            lines.append(f"    {name:<16} {hit}/{total}")
        lines.extend(f"    ✕ {problem}" for problem in result.problems)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work", type=Path, help="만든 자료와 프로젝트를 남길 폴더")
    parser.add_argument("--json", type=Path, help="결과를 JSON으로 저장")
    args = parser.parse_args(argv)
    results = run(args.work)
    print(to_text(results))
    if args.json:
        args.json.write_text(json.dumps(
            [{"scenario": r.scenario, "scores": r.scores, "problems": r.problems} for r in results],
            ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
