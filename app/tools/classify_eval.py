"""업무 분류 정확도 측정 — 사람이 매긴 정답과 분류 결과를 견준다.

    python -m app.tools.classify_eval --db 프로젝트.db --gold 정답.csv [--model] [--json 결과.json]
                                      [--max-calls 150]

정답 파일(UTF-8 CSV)은 머리글이 `path,item`이다. 한 줄이 문서 하나다.

    path  문서 경로의 뒷부분. 자료 폴더 아래 상대 경로이거나 파일 이름이다. 슬래시와 역슬래시는 같다.
    item  사무분장 항목 이름. 분장 어디에도 속하지 않는 자료는 `분장 밖`이라고 적는다.
          (선택) `part` 열에 세부업무 이름을 적으면 세부업무도 견준다.

읽는 법
  정확도          정답이 있는 문서 가운데 분류가 정답과 같은 비율. 판단 불가는 틀린 것으로 센다.
  적중 범위       판단 불가가 아니라 어디엔가 나눈 비율. 정확도만 보면 조심스러운 분류기가 손해다.
  판단한 것의 정확도  판단 불가를 뺀 정확도. 틀린 배정이 얼마나 되는지를 본다.
  근거별 정확도   이름 일치 · AI 판단 · 낱말 겹침 · 인용 · 사용자 학습이 각각 얼마나 맞는가.
                  모델(AI 판단)이 규칙보다 나은지, 호출 상한(--max-calls)이 적절한지 여기서 본다.

프로젝트 DB는 읽기만 한다. 분류는 임시 사본에서 돌린다. `--model`을 주면 프로젝트에 설정된
생성 모델로 판정한다(없으면 규칙만).
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

OUTSIDE_LABEL = "분장 밖"
UNKNOWN_LABEL = "(판단 불가)"


@dataclass(slots=True)
class GoldRow:
    path: str
    item: str
    part: str = ""


@dataclass(slots=True)
class Report:
    gold: int = 0
    found: int = 0
    correct: int = 0
    decided: int = 0
    decided_correct: int = 0
    part_gold: int = 0
    part_correct: int = 0
    missing: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    by_basis: dict[str, dict[str, int]] = field(default_factory=dict)
    by_item: dict[str, dict[str, int]] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)

    @property
    def accuracy(self) -> float:
        return self.correct / self.found if self.found else 0.0

    @property
    def coverage(self) -> float:
        return self.decided / self.found if self.found else 0.0

    @property
    def decided_accuracy(self) -> float:
        return self.decided_correct / self.decided if self.decided else 0.0

    def as_dict(self) -> dict:
        return {
            "gold": self.gold, "found": self.found, "missing": self.missing,
            "accuracy": round(self.accuracy, 4), "coverage": round(self.coverage, 4),
            "decided_accuracy": round(self.decided_accuracy, 4),
            "part_accuracy": round(self.part_correct / self.part_gold, 4) if self.part_gold else None,
            "by_basis": self.by_basis, "by_item": self.by_item, "calls": self.calls, "errors": self.errors,
        }


def read_gold(path: Path) -> list[GoldRow]:
    rows: list[GoldRow] = []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            location = (raw.get("path") or "").strip()
            item = (raw.get("item") or "").strip()
            if location and item:
                rows.append(GoldRow(location.replace("\\", "/").strip("/"), item, (raw.get("part") or "").strip()))
    return rows


def _find(index: list[tuple[str, int]], needle: str) -> int | None:
    """경로 뒷부분이 같은 문서. 둘 이상이면 정하지 않는다."""
    hits = [doc_id for path, doc_id in index if path == needle or path.endswith("/" + needle)]
    return hits[0] if len(hits) == 1 else None


def evaluate(db, gold: list[GoldRow]) -> Report:
    """이미 분류를 마친 DB(unit_assignments)를 정답과 견준다."""
    from ..ai import classify_units as cu

    report = Report(gold=len(gold))
    mine = cu.assignments(db)
    label_of: dict[int, cu.Assignment] = {}
    for row in db.con.execute("SELECT unit_id, doc_id, u.unit_key AS key FROM work_unit_docs "
                              "JOIN work_units u ON u.id = work_unit_docs.unit_id"):
        if row["key"] in mine:
            label_of[row["doc_id"]] = mine[row["key"]]
    index = [(r["path"].replace("\\", "/"), r["id"]) for r in
             db.con.execute("SELECT id, path FROM documents WHERE missing_since IS NULL")]
    basis_stat: dict[str, Counter] = defaultdict(Counter)
    item_stat: dict[str, Counter] = defaultdict(Counter)

    for row in gold:
        doc_id = _find(index, row.path)
        assignment = label_of.get(doc_id) if doc_id is not None else None
        if assignment is None:
            report.missing.append(row.path)
            continue
        report.found += 1
        if assignment.bucket == cu.ITEM:
            got = assignment.item_name
        elif assignment.bucket == cu.OUTSIDE:
            got = OUTSIDE_LABEL
        else:
            got = UNKNOWN_LABEL
        right = got == row.item
        decided = got != UNKNOWN_LABEL
        report.correct += right
        report.decided += decided
        report.decided_correct += decided and right
        basis = assignment.basis or "(없음)"
        basis_stat[basis]["n"] += 1
        basis_stat[basis]["correct"] += right
        item_stat[row.item]["gold"] += 1
        item_stat[row.item]["correct"] += right
        item_stat[got]["predicted"] += 1
        item_stat[got]["predicted_correct"] += right
        if row.part:
            report.part_gold += 1
            report.part_correct += right and assignment.part == row.part
        if not right:
            report.errors.append({"path": row.path, "expected": row.item, "got": got, "basis": basis,
                                  "confidence": assignment.confidence, "evidence": assignment.evidence[:120]})
    report.by_basis = {k: dict(v) for k, v in basis_stat.items()}
    report.by_item = {k: dict(v) for k, v in item_stat.items()}
    return report


def run(db_path: Path, gold: list[GoldRow], use_model: bool = False, max_calls: int | None = None) -> Report:
    """프로젝트 DB의 임시 사본에서 분류를 돌려 정답과 견준다."""
    from ..ai import classify_units as cu
    from ..db import Database

    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "project.db"
        shutil.copy2(db_path, copy)
        db = Database(copy)
        try:
            db.init()
            client = None
            if use_model:
                from ..ai.client import OllamaClient
                from ..ai.settings import project_client
                candidate = project_client(db, OllamaClient)
                client = candidate if candidate.health().generation_ready else None
            kwargs = {"max_calls": max_calls} if max_calls else {}
            calls = cu.classify(db, client, **kwargs)
            report = evaluate(db, gold)
            report.calls = {k: v for k, v in calls.items() if k in ("model_calls", "not_judged")}
            return report
        finally:
            db.close()


def render(report: Report) -> str:
    def pct(value: float) -> str:
        return f"{value * 100:.1f}%"

    lines = [
        f"정답 {report.gold}건 · 분류 결과와 이어진 문서 {report.found}건"
        + (f" · 못 찾음 {len(report.missing)}건" if report.missing else ""),
        f"정확도 {pct(report.accuracy)} ({report.correct}/{report.found}) · "
        f"적중 범위 {pct(report.coverage)} ({report.decided}/{report.found}) · "
        f"판단한 것의 정확도 {pct(report.decided_accuracy)} ({report.decided_correct}/{report.decided})",
    ]
    if report.part_gold:
        lines.append(f"세부업무 정확도 {pct(report.part_correct / report.part_gold)} "
                     f"({report.part_correct}/{report.part_gold})")
    if report.calls:
        lines.append(f"모델 호출 {report.calls.get('model_calls', 0)}회 · 상한으로 못 물은 단위 {report.calls.get('not_judged', 0)}건")
    lines.append("근거별 정확도")
    for basis, stat in sorted(report.by_basis.items(), key=lambda kv: -kv[1]["n"]):
        lines.append(f"  {basis:<16} {pct(stat['correct'] / stat['n']):>7}  ({stat['correct']}/{stat['n']})")
    lines.append("항목별 (정답 수 · 재현율 / 분류된 수 · 정밀도)")
    for item, stat in sorted(report.by_item.items()):
        gold = stat.get("gold", 0)
        predicted = stat.get("predicted", 0)
        recall = pct(stat.get("correct", 0) / gold) if gold else "-"
        precision = pct(stat.get("predicted_correct", 0) / predicted) if predicted else "-"
        lines.append(f"  {item:<20} {gold:>4} · {recall:>7} / {predicted:>4} · {precision:>7}")
    if report.errors:
        lines.append(f"틀린 것 {len(report.errors)}건 (앞 10건)")
        for error in report.errors[:10]:
            lines.append(f"  {error['path']} → {error['got']} (정답 {error['expected']}) [{error['basis']}]")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="업무 분류 정확도 측정")
    parser.add_argument("--db", required=True, type=Path, help="프로젝트 DB 파일")
    parser.add_argument("--gold", required=True, type=Path, help="정답 CSV (path,item[,part])")
    parser.add_argument("--model", action="store_true", help="설정된 생성 모델로 판정한다")
    parser.add_argument("--max-calls", type=int, default=None, help="모델 호출 상한(기본 150)")
    parser.add_argument("--json", type=Path, default=None, help="결과를 JSON으로도 저장한다")
    args = parser.parse_args(argv)
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    gold = read_gold(args.gold)
    if not gold:
        print("정답 파일에 읽을 줄이 없습니다. 머리글은 path,item 입니다.")
        return 2
    report = run(args.db, gold, use_model=args.model, max_calls=args.max_calls)
    print(render(report))
    if args.json:
        args.json.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
