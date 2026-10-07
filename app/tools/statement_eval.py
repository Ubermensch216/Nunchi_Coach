"""문장 추출의 정밀도 재기 — 엉뚱한 문장을 얼마나 고르는가.

    python -m app.tools.statement_eval [--cases 경로] [--repeat 횟수] [--json 경로]

시나리오 평가(`scenario_eval --model`)는 재현율만 보여 준다. 그 자료에는 후보
문장이 1~2개뿐이고 모두 정답이라, 모델이 인사말이나 안내 문구에 갈래를 붙여도
드러나지 않는다. 이 도구는 쓸모 있는 문장과 그렇지 않은 문장을 섞은 표본을
제품과 같은 프롬프트·같은 검증으로 모델에 보내고 센다.

읽는 법
  정밀도       모델이 갈래를 붙인 문장 가운데 실제로 쓸모 있는 문장의 비율.
               낮으면 '문장에서 찾은 내용'에 인사말·안내 문구가 섞인다.
  재현율       쓸모 있는 문장 가운데 모델이 갈래를 붙인 비율.
  갈래 정확도  고른 쓸모 있는 문장 가운데 갈래까지 맞은 비율.

표본은 합성이며 정답도 만든 사람이 붙였다. 실제 기관 문서에서의 수치가 아니다.
모델의 답은 실행마다 다를 수 있으니 --repeat 3 이상으로 편차를 함께 본다.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..core import statements

DEFAULT_CASES = (Path(__file__).resolve().parents[2]
                 / "tests" / "fixtures" / "statements" / "precision.json")


@dataclass(slots=True)
class Tally:
    useful: int = 0             # 쓸모 있는 문장 수(정답)
    picked: int = 0             # 모델이 갈래를 붙인 문장 수
    hits: int = 0               # 그 가운데 쓸모 있는 문장
    right_kind: int = 0         # 그 가운데 갈래까지 맞은 것
    failed: int = 0             # 모델이 답하지 못한 묶음
    wrong: list[str] = field(default_factory=list)      # 엉뚱하게 고른 문장
    missed: list[str] = field(default_factory=list)     # 놓친 문장

    @property
    def precision(self) -> float:
        return self.hits / self.picked if self.picked else 1.0

    @property
    def recall(self) -> float:
        return self.hits / self.useful if self.useful else 1.0

    @property
    def kind_accuracy(self) -> float:
        return self.right_kind / self.hits if self.hits else 1.0


def judge(group: dict, picks: list[list] | None, tally: Tally) -> None:
    """묶음 하나에 대한 모델의 답(번호·갈래)을 정답과 견준다."""
    listed = group["sentences"]
    tally.useful += sum(1 for item in listed if item["accept"])
    if picks is None:
        tally.failed += 1
        tally.missed += [item["text"] for item in listed if item["accept"]]
        return
    chosen = {number: category for number, category in picks}
    for number, item in enumerate(listed, start=1):
        category = chosen.get(number)
        if category is None:
            if item["accept"]:
                tally.missed.append(item["text"])
            continue
        tally.picked += 1
        if not item["accept"]:
            tally.wrong.append(f"{item['text']}  → {statements.CATEGORIES[category]}")
            continue
        tally.hits += 1
        tally.right_kind += category in item["accept"]


def ask(client, group: dict) -> list[list] | None:
    """제품(statements.find)과 같은 프롬프트·스키마·검증으로 묻는다."""
    from ..ai.prompts_loader import render

    listed = group["sentences"]
    prompt, _version = render(statements.PROMPT, sentences="\n".join(
        f"{number}. {item['text']}" for number, item in enumerate(listed, start=1)))
    data, error, _raw = client.generate_json(
        prompt, statements.SCHEMA, num_predict=statements._NUM_PREDICT)
    return None if error else statements._verified(data, len(listed))


def run(client, groups: list[dict]) -> Tally:
    tally = Tally()
    for group in groups:
        judge(group, ask(client, group), tally)
    return tally


def to_text(rounds: list[Tally], model: str) -> str:
    lines = [f"문장 추출 정밀도 — {model}", ""]
    for number, tally in enumerate(rounds, start=1):
        lines.append(
            f"[{number}회차] 정밀도 {tally.hits}/{tally.picked} ({tally.precision:.0%}) · "
            f"재현율 {tally.hits}/{tally.useful} ({tally.recall:.0%}) · "
            f"갈래 정확도 {tally.right_kind}/{tally.hits} ({tally.kind_accuracy:.0%})"
            + (f" · 답하지 못한 묶음 {tally.failed}" if tally.failed else ""))
        lines += [f"    ✕ 엉뚱하게 고름: {text}" for text in tally.wrong]
        lines += [f"    · 놓침: {text}" for text in tally.missed]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="문장 추출의 정밀도 재기")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="표본 경로")
    parser.add_argument("--repeat", type=int, default=1, help="되풀이 횟수(편차를 본다)")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)

    from ..ai.client import OllamaClient

    client = OllamaClient()
    if not client.health().generation_ready:
        print("생성 모델이 준비되지 않았습니다. Ollama와 모델을 확인하세요.", file=sys.stderr)
        return 2
    groups = json.loads(args.cases.read_text(encoding="utf-8"))["groups"]
    rounds = [run(client, groups) for _ in range(max(1, args.repeat))]
    print(to_text(rounds, client.gen_model))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({
            "model": client.gen_model,
            "rounds": [{"useful": t.useful, "picked": t.picked, "hits": t.hits,
                        "right_kind": t.right_kind, "failed": t.failed,
                        "wrong": t.wrong, "missed": t.missed} for t in rounds],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
