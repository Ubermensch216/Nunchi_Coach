"""업무 수행 시나리오 평가 — 전임자 없이 자료만으로 어디까지 가는가 (구축계획서 §7).

    python -m app.tools.scenario_eval [--model] [--work 폴더] [--json 경로] [--report 경로]

질문에 답을 잘하는지(rag_report)와는 다른 것을 잰다. 빈 프로젝트에 원문 폴더만
넣었을 때, 사람이 아무것도 입력하기 전에

    · 문서에 적힌 핵심 항목과 절차를 얼마나 내놓는가        (자동 복원)
    · 과거 값·개정 전 값·없는 사실을 현재 것으로 내놓지 않는가 (오판 방지)
    · 모르는 것마다 구체적인 다음 행동이 있고, 그 공백이 자기
      단계만 막는가                                         (공백 대응)
    · 전임자의 확인·입력을 한 번도 요구하지 않는가            (전임자 독립성)
    · 단계를 기록하고 닫았다 열어도 이어지는가                (업무 연속성)
    · 원본이 그대로인가                                     (원본 보호)

를 정답과 견준다. 정답(tests/fixtures/scenarios/answers.json)은 이 도구만 읽고
제품에는 넣지 않는다.

읽을 때 주의
  · 기본은 생성 모델을 쓰지 않는다. 그래서 결과가 실행마다 같다.
  · `--model`을 주면 이 PC의 Ollama로 문장에서 찾은 내용(core/statements.py)까지
    넣어 잰다. 모델에 따라 달라지는 수치이므로 기본 결과와 따로 적는다.
  · 문서를 업무에 묶는 일(임베딩 군집)은 건너뛰고 시나리오의 문서를 그 업무에
    직접 배정한다. 업무 묶기의 품질은 여기서 재지 않는다.
  · 합성 자료 세 건이다. 실제 기관 문서에서의 정확도를 말해 주지 않는다.
  · 복원율의 분모에는 '문장으로만 적혀 있어 놓칠 것'(missed)도 넣는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..core import continuity, handover_release, reconstruction as rc, workcard, workstate as ws

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "scenarios"
_HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
_ASKS_PREDECESSOR = ("전임자에게", "전임자의 확인", "전임자 승인", "전임자가 확인할 때까지")


@dataclass(slots=True)
class Check:
    area: str
    name: str
    passed: bool
    detail: str = ""


@dataclass(slots=True)
class Result:
    scenario: str
    title: str
    checks: list[Check] = field(default_factory=list)
    restored: int = 0
    expected: int = 0

    @property
    def rate(self) -> float:
        return self.restored / self.expected if self.expected else 1.0

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.passed]

    def add(self, area: str, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(area, name, bool(passed), detail))


# ── 자료 만들기 ─────────────────────────────────────────────────────

def write_corpus(root: Path, scenario: dict) -> list[Path]:
    """시나리오의 문서를 실제 파일로 만든다. 제품은 이 폴더만 본다."""
    made = []
    for document in scenario["documents"]:
        path = root / document["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".hwpx":
            _write_hwpx(path, document["blocks"])
        else:
            path.write_text("\n\n".join(block for block in document["blocks"]
                                        if isinstance(block, str)), encoding="utf-8")
        made.append(path)
    return made


def _write_hwpx(path: Path, blocks: list) -> None:
    """한글이 쓰는 구조(문단 안의 표, subList·cellAddr·cellSpan)로 적는다."""
    def text(value: str) -> str:
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    parts = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(f"<hp:p><hp:run><hp:t>{text(block)}</hp:t></hp:run></hp:p>")
            continue
        rows = []
        for r, row in enumerate(block["table"]):
            cells = "".join(
                f'<hp:tc header="0"><hp:subList><hp:p><hp:run><hp:t>{text(value)}</hp:t>'
                f'</hp:run></hp:p></hp:subList><hp:cellAddr colAddr="{c}" rowAddr="{r}"/>'
                f'<hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>'
                for c, value in enumerate(row))
            rows.append(f"<hp:tr>{cells}</hp:tr>")
        parts.append(f"<hp:p><hp:run><hp:tbl>{''.join(rows)}</hp:tbl></hp:run></hp:p>")
    section = (f'<?xml version="1.0" encoding="UTF-8"?>'
               f'<hs:sec xmlns:hs="{_HP}" xmlns:hp="{_HP}">{"".join(parts)}</hs:sec>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr("Contents/section0.xml", section)


def _fingerprint(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


# ── 제품 돌리기 ─────────────────────────────────────────────────────

def analyse(project: Path, source: Path, task_name: str, basis: date, client=None):
    """빈 프로젝트에 원문 폴더만 넣고 분석한다. 사람의 입력은 없다."""
    from PySide6.QtCore import QCoreApplication

    from ..ai.client import OllamaClient
    from ..db import Database
    from ..jobs import pipeline as pipeline_module

    QCoreApplication.instance() or QCoreApplication([])
    db = Database(project / "project.db")
    db.init()
    db.add_source(source)
    worker = pipeline_module.Pipeline(db.path)
    worker._scan(db)
    worker._hash(db)
    worker._parse(db)
    worker._date(db)
    # 업무 묶기(임베딩 군집)는 이 평가의 대상이 아니다. 시나리오의 문서를 그
    # 업무에 직접 배정한다. client를 주지 않으면 생성 모델은 한 번도 부르지 않는다.
    task_id = db.con.execute(
        "INSERT INTO tasks(name, origin, confidence) VALUES (?, 'seeded', 'high')",
        (task_name,)).lastrowid
    for row in db.con.execute(
            "SELECT id FROM documents WHERE parse_status IN ('ok', 'partial')").fetchall():
        db.con.execute(
            "INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, ?, 'ai', 'high')",
            (task_id, row["id"]))
    worker._cycles(db)
    worker._steps(db)
    rc.restore(db, basis, client=client)
    handover_release.autosave(db)
    return db, task_id


# ── 채점 ────────────────────────────────────────────────────────────

def judge(db, task_id: int, scenario: dict, answer: dict, basis: date,
          before: dict[str, str], source: Path) -> Result:
    result = Result(scenario["id"], scenario["title"])
    items = db.task_items(task_id)
    card = workcard.build(db, task_id)
    gaps = {gap["gap_key"]: gap for gap in db.info_gaps(task_id)}
    stated = [step for step in card.stated_steps]
    draft = workcard.draft_markdown(db, task_id, basis)

    # ① 자동 복원 — 항목
    for expected in answer["fields"]:
        found = _find_item(items, expected)
        result.expected += 1
        result.restored += bool(found)
        label = f"{rc.KIND_LABEL.get(expected['kind'], expected['kind'])}: {expected['contains']}"
        result.add("자동 복원", label, found,
                   "" if found else "항목이 없거나 값·상태가 정답과 다르다")
    # ① 자동 복원 — 절차
    labels = [step["label"] for step in stated]
    for index, expected in enumerate(answer["steps"]):
        ok = index < len(labels) and expected in labels[index]
        result.expected += 1
        result.restored += ok
        result.add("자동 복원", f"절차 {index + 1}: {expected}", ok,
                   "" if ok else f"실제: {labels[index] if index < len(labels) else '없음'}")
    if not answer["steps"]:
        result.add("자동 복원", "문서에 절차가 없으면 절차를 지어내지 않는다", not stated,
                   "" if not stated else f"세워진 단계: {labels}")
    # 문장으로만 적혀 있어 놓칠 것으로 본 항목. 분모에 넣고, 내놓지 못했다고 적는다.
    # 모델이 문장에서 찾아 '추정'으로 내놓았으면 복원한 것으로 센다.
    told = [item["content"] for item in items if item["kind"] == rc.STATEMENT]
    for missed in answer.get("missed", []):
        result.expected += 1
        if any(missed["contains"] in content for content in told):
            result.restored += 1
            result.add("자동 복원", f"(문장) {missed['what']}", True, "문장에서 찾음 · 추정으로 표시")
            continue
        result.add("자동 복원", f"(놓침 예상) {missed['what']}", True,
                   f"내놓지 못함 — {missed['why']}")

    # ② 오판 방지
    blob = "\n".join([draft] + [item["content"] for item in items]
                     + [f"{g['detail']} {g['research'] or ''} {g['next_action'] or ''}"
                        for g in gaps.values()]
                     + [action["text"] for action in card.actions])
    for rule in answer["forbidden"]:
        if rule["where"] == "item_value":
            bad = [item["content"] for item in items
                   if item["kind"] == rule["kind"]
                   and rule.get("slot_contains", "") in item["slot"]
                   and rule["contains"] in item["content"]]
        else:
            bad = [rule["contains"]] if rule["contains"] in blob else []
        result.add("오판 방지", rule["why"], not bad, "" if not bad else f"나온 값: {bad[0]}")

    # ③ 공백 대응
    for expected in answer["gaps"]:
        gap = gaps.get(expected["key"])
        if gap is None:
            result.add("공백 대응", f"{expected['key']} 공백이 뜬다", False, "뜨지 않았다")
            continue
        action = gap["next_action"] or ""
        concrete = len(action) > 20 and not any(word in action for word in _ASKS_PREDECESSOR)
        result.add("공백 대응", f"{expected['key']}: 구체적인 다음 행동", concrete,
                   "" if concrete else f"문구: {action!r}")
        blocked = next((step["label"] for step in stated if step["id"] == gap["step_id"]), None)
        wanted = expected["blocks"]
        scoped = (blocked is None) if wanted is None else (blocked is not None and wanted in blocked)
        result.add("공백 대응", f"{expected['key']}: 막는 범위", scoped,
                   "" if scoped else f"막은 단계: {blocked!r}, 기대: {wanted!r}")
    for key in answer["no_gaps"]:
        result.add("공백 대응", f"{key} 공백이 뜨지 않는다(자료에 답이 있다)", key not in gaps,
                   "" if key not in gaps else "자료에 있는데 없다고 했다")
    unblocked = [step for step in stated if not step["blocked"]]
    if stated:
        result.add("공백 대응", "막히지 않은 단계가 남아 있다", bool(unblocked))

    # ④ 다음 행동
    first = card.actions[0] if card.actions else None
    wanted = answer["first_action"]
    ok = bool(first and first["type"] == wanted["type"] and wanted["contains"] in first["text"])
    result.add("다음 행동", f"첫 행동: {wanted['contains']}", ok,
               "" if ok else f"실제: {first['text'] if first else '없음'}")
    if "precedent_year" in answer:
        years = {step["slot"].split("-")[1] for step in card.precedent_steps}
        ok = years == {str(answer["precedent_year"])}
        result.add("다음 행동", f"과거 사례는 끝까지 간 회차({answer['precedent_year']}년)에서",
                   ok, "" if ok else f"실제 회차: {sorted(years)}")

    # ⑤ 전임자 독립성
    context = db.work_context()
    result.add("전임자 독립성", "전임자 정보 없이 '자료로 시작'",
               context["mode"] == ws.MODE_MATERIALS and context["predecessor"] is None)
    result.add("전임자 독립성", "사람의 확인·입력 0회",
               db.con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
               and all(item["human_state"] == ws.NONE for item in items))
    result.add("전임자 독립성", "전임자에게 묻거나 기다리라는 문구 없음",
               not any(word in blob for word in _ASKS_PREDECESSOR))
    result.add("전임자 독립성", "복원본이 사람 확인 없이 저장됨", len(db.snapshots()) == 1)

    # ⑥ 결과 초안
    values = [expected["contains"] for expected in answer["fields"]
              if expected["kind"] != rc.RECORD and _find_item(items, expected)]
    missing = [value for value in values if value not in draft]
    result.add("결과 초안", "복원한 값이 초안에 근거와 함께 실린다", not missing,
               "" if not missing else f"빠진 값: {missing}")
    blank = "| 자료 없음 |" in draft
    if answer["draft_complete"]:
        result.add("결과 초안", "필수 항목에 빈칸이 없다(초안 완성)", not blank)
    else:
        result.add("결과 초안", "자료에 없는 필수 항목은 빈칸으로 남는다", blank,
                   "" if blank else "빈칸이 없다 — 없는 값을 채웠을 수 있다")

    # ⑦ 업무 연속성
    _continuity(db, task_id, basis, result)

    # ⑧ 원본 보호
    result.add("원본 보호", "분석·저장 뒤 원본이 그대로다", _fingerprint(source) == before)
    return result


def _find_item(items: list[dict], expected: dict) -> bool:
    for item in items:
        if item["kind"] != expected["kind"] or expected["contains"] not in item["content"]:
            continue
        if expected.get("slot_contains") and expected["slot_contains"] not in item["slot"]:
            continue
        if expected.get("applicability") and item["applicability"] != expected["applicability"]:
            continue
        if expected.get("basis") and item["basis_state"] != expected["basis"]:
            continue
        return True
    return False


def _continuity(db, task_id: int, basis: date, result: Result) -> None:
    """단계 하나를 기록하고, 닫았다 열고, 다시 분석해도 이어지는가."""
    from ..db import Database

    steps = workcard.build(db, task_id).stated_steps
    doable = next((step for step in steps if not step["blocked"]), None)
    if doable is None:
        steps = workcard.build(db, task_id).precedent_steps
        doable = steps[0] if steps else None
    if doable is None:
        result.add("업무 연속성", "기록할 단계가 있다", False, "단계가 하나도 없다")
        return
    db.set_step_run(doable["id"], ws.DONE, note="평가 기록")
    path = db.path
    db.close()
    reopened = Database(path)
    reopened.init()
    try:
        rc.restore(reopened, basis)
        card = workcard.build(reopened, task_id)
        kept = next((step for step in card.stated_steps + card.precedent_steps
                     if step["id"] == doable["id"]), None)
        ok = bool(kept and kept["run_state"] == ws.DONE and kept["run_note"] == "평가 기록")
        result.add("업무 연속성", "닫았다 열고 다시 분석해도 수행 기록이 남는다", ok)
        following = continuity.next_actions(reopened, task_id)
        moved = not following or following[0]["step_id"] != doable["id"]
        result.add("업무 연속성", "다음 행동이 끝낸 단계를 다시 권하지 않는다", moved)
    finally:
        reopened.close()


# ── 실행 ────────────────────────────────────────────────────────────

def run(work: Path | None = None, client=None) -> list[Result]:
    corpus = json.loads((FIXTURES / "corpus.json").read_text(encoding="utf-8"))
    answers = json.loads((FIXTURES / "answers.json").read_text(encoding="utf-8"))
    basis = date.fromisoformat(corpus["basis"])
    owned = work is None
    work = Path(tempfile.mkdtemp(prefix="nunchi-scenarios-")) if owned else Path(work)
    results = []
    try:
        for scenario in corpus["scenarios"]:
            folder = work / scenario["id"]
            if folder.exists():
                shutil.rmtree(folder)
            source, project = folder / "자료", folder / "project"
            write_corpus(source, scenario)
            before = _fingerprint(source)
            db, task_id = analyse(project, source, scenario["task"], basis, client)
            try:
                results.append(judge(db, task_id, scenario, answers["scenarios"][scenario["id"]],
                                     basis, before, source))
            finally:
                db.close()
    finally:
        if owned:
            shutil.rmtree(work, ignore_errors=True)
    return results


def summary(results: list[Result], threshold: float, model: str = "") -> dict:
    restored = sum(r.restored for r in results)
    expected = sum(r.expected for r in results)
    return {
        "restored": restored, "expected": expected,
        "rate": restored / expected if expected else 1.0, "threshold": threshold,
        "model": model,
        "failed_checks": sum(len(r.failures) for r in results),
        "checks": sum(len(r.checks) for r in results),
        "critical": sum(1 for r in results for c in r.failures if c.area == "오판 방지"),
    }


def to_markdown(results: list[Result], totals: dict) -> str:
    out = ["# 업무 수행 시나리오 평가", "",
           f"- 자동 복원: {totals['restored']}/{totals['expected']} "
           f"({totals['rate']:.0%}, 기준 {totals['threshold']:.0%})",
           f"- 검사 {totals['checks']}건 중 실패 {totals['failed_checks']}건 · "
           f"오판(치명) {totals['critical']}건",
           f"- {'생성 모델 ' + totals['model'] + ' (문장에서 찾은 내용 포함)' if totals.get('model') else '생성 모델 미사용'}"
           " · 업무 묶기는 직접 배정 · 합성 자료", ""]
    for result in results:
        out += [f"## {result.title}", "",
                f"자동 복원 {result.restored}/{result.expected} ({result.rate:.0%})", "",
                "| 영역 | 검사 | 결과 | 비고 |", "|---|---|---|---|"]
        for check in result.checks:
            mark = "통과" if check.passed else "**실패**"
            if check.passed and check.name.startswith("(놓침 예상)"):
                mark = "놓침"
            out.append(f"| {check.area} | {check.name} | {mark} | {check.detail} |")
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="전임자 없이 자료만으로 하는 업무 수행 평가")
    parser.add_argument("--work", type=Path, help="자료와 프로젝트를 만들 폴더(남겨 둔다)")
    parser.add_argument("--model", action="store_true",
                        help="이 PC의 Ollama로 문장에서 찾은 내용까지 넣어 잰다")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)

    answers = json.loads((FIXTURES / "answers.json").read_text(encoding="utf-8"))
    client = None
    if args.model:
        from ..ai.client import OllamaClient

        client = OllamaClient()
        if not client.health().generation_ready:
            print("생성 모델이 준비되지 않았습니다. Ollama와 모델을 확인하세요.", file=sys.stderr)
            return 2
    results = run(args.work, client)
    totals = summary(results, answers["threshold"], client.gen_model if client else "")
    text = to_markdown(results, totals)
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({
            "summary": totals,
            "scenarios": [{"id": r.scenario, "restored": r.restored, "expected": r.expected,
                           "checks": [{"area": c.area, "name": c.name, "passed": c.passed,
                                       "detail": c.detail} for c in r.checks]}
                          for r in results]}, ensure_ascii=False, indent=2), encoding="utf-8")
    passed = (totals["rate"] >= totals["threshold"] and not totals["failed_checks"])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
