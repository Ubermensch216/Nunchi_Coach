"""업무 수행 시나리오 평가 (구축계획서 §7.1·§7.2).

이 시험은 평가 도구가 돌아가는지와, 오판·공백·독립성·연속성·원본 보호 검사가
모두 통과하는지를 본다. 자동 복원율은 **숫자를 고정**해 둔다 — 규칙을 바꿔 복원율이
오르거나 내리면 이 시험이 알려 주고, 그때 정답 파일이 아니라 바뀐 이유를 본다.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core import continuity, reconstruction as rc, workstate as ws  # noqa: E402
from app.db import Database  # noqa: E402
from app.tools import scenario_eval  # noqa: E402

BASIS = date(2025, 9, 10)


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    return scenario_eval.run(tmp_path_factory.mktemp("scenarios"))


def test_corpus_holds_no_answers_and_answers_are_not_product_input():
    corpus = (scenario_eval.FIXTURES / "corpus.json").read_text(encoding="utf-8")
    assert "forbidden" not in corpus and "first_action" not in corpus
    # 제품 코드는 정답 파일을 읽지 않는다. 평가 도구만 읽는다.
    app = Path(scenario_eval.__file__).resolve().parents[1]
    readers = [path.name for path in app.rglob("*.py")
               if "answers.json" in path.read_text(encoding="utf-8")]
    assert readers == ["scenario_eval.py"]


def test_every_scenario_runs_without_a_predecessor_or_a_model(results):
    assert [r.scenario for r in results] == ["report", "contract", "revised"]
    for result in results:
        independence = [c for c in result.checks if c.area == "전임자 독립성"]
        assert len(independence) == 4 and all(c.passed for c in independence), result.scenario


def test_no_check_fails_and_nothing_is_misjudged(results):
    failures = [(r.scenario, c.area, c.name, c.detail) for r in results for c in r.failures]
    assert failures == []
    totals = scenario_eval.summary(results, 0.9)
    assert totals["critical"] == 0 and totals["checks"] >= 90


def test_restoration_rate_is_reported_with_what_was_missed(results):
    """놓친 것을 분모에서 빼지 않는다. 지금의 규칙 기반 추출은 기준(90%)에 못 미친다."""
    by_id = {r.scenario: (r.restored, r.expected) for r in results}
    assert by_id == {"report": (12, 13), "contract": (5, 7), "revised": (7, 8)}
    totals = scenario_eval.summary(results, 0.9)
    assert (totals["restored"], totals["expected"]) == (24, 28)
    assert totals["rate"] < totals["threshold"]
    missed = [c.name for r in results for c in r.checks if c.name.startswith("(놓침 예상)")]
    assert len(missed) == 4
    text = scenario_eval.to_markdown(results, totals)
    assert "자동 복원: 24/28 (86%, 기준 90%)" in text and "| 놓침 |" in text


def test_tool_exit_code_reflects_the_unmet_threshold(tmp_path, capsys):
    code = scenario_eval.main(["--work", str(tmp_path / "work"),
                               "--json", str(tmp_path / "out.json"),
                               "--report", str(tmp_path / "out.md")])
    capsys.readouterr()
    saved = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert code == 1 and saved["summary"]["failed_checks"] == 0
    assert (tmp_path / "out.md").read_text(encoding="utf-8").startswith("# 업무 수행 시나리오 평가")
    # --work를 주면 자료와 프로젝트가 남아 사람이 열어 볼 수 있다.
    assert (tmp_path / "work" / "report" / "project" / "project.db").is_file()


# ── 평가가 드러내 고친 것들 ─────────────────────────────────────────

def project(tmp_path, scenario_id):
    corpus = json.loads((scenario_eval.FIXTURES / "corpus.json").read_text(encoding="utf-8"))
    scenario = next(s for s in corpus["scenarios"] if s["id"] == scenario_id)
    source = tmp_path / "자료"
    scenario_eval.write_corpus(source, scenario)
    return scenario_eval.analyse(tmp_path / "project", source, scenario["task"], BASIS)


def test_precedent_comes_from_the_last_finished_round_and_done_cases_are_not_offered_again(tmp_path):
    db, task = project(tmp_path, "contract")
    try:
        cases = [s for s in db.work_steps(task) if s["procedure_kind"] == ws.PROCEDURE_PRECEDENT]
        assert {s["slot"].split("-")[1] for s in cases} == {"2024"} and len(cases) == 3
        first = continuity.next_actions(db, task)[0]
        assert first["step_id"] == cases[0]["id"] and "부터 했습니다" in first["text"]
        db.set_step_run(cases[0]["id"], ws.DONE)
        second = continuity.next_actions(db, task)[0]
        assert second["step_id"] == cases[1]["id"] and "그다음에 했습니다" in second["text"]
        # 마지막 기록 뒤가 비어 있을 때, 지난 회차의 같은 단계 다음을 실마리로 준다.
        gap = next(g for g in db.info_gaps(task) if g["gap_key"] == "afterwards")
        assert "지난 기록에서는 그다음에" in gap["next_action"] or "결재·발송" in gap["next_action"]
    finally:
        db.close()


def test_revised_guideline_keeps_the_older_value_in_view_without_blocking(tmp_path):
    db, task = project(tmp_path, "revised")
    try:
        gaps = {g["gap_key"]: g for g in db.info_gaps(task)}
        form = gaps["changed:form:사용서식"]
        assert form["kind"] == "changed" and form["step_id"] is None
        assert "2023년 ‘별지 제1호 서식’ — 2023_보조금_교부_지침.hwpx" in form["research"]
        assert "최근 문서라는 이유만으로" in form["detail"]
        evidence = db.evidence_for("gap", [form["id"]])[form["id"]]
        assert {e["filename"] for e in evidence} == {
            "2023_보조금_교부_지침.hwpx", "2025_보조금_교부_지침_개정.hwpx"}
        # 사람이 지금 값을 확인하면 더 묻지 않는다.
        item = next(i for i in db.task_items(task) if i["kind"] == rc.FORM)
        db.review_task_item(item["id"], "confirm")
        continuity.review_task(db, task, BASIS)
        assert "changed:form:사용서식" not in {g["gap_key"] for g in db.info_gaps(task)}

        # 받는 기한(신청기한)과 '통보' 단계는 제출처·제출기한에 걸리지 않는다.
        steps = [s for s in db.work_steps(task) if s["procedure_kind"] == ws.PROCEDURE_STATED]
        assert not any(s["blocked"] for s in steps)
        assert steps[-1]["label"] == "교부 결정 통보" and not steps[-1]["done_criteria"]
        assert gaps["stale:deadline:신청기한"]["step_id"] is None
    finally:
        db.close()
