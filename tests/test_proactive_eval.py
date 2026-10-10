"""선제 감지 평가 — 내야 할 제안은 내고, 내지 말아야 할 제안은 내지 않는다 (구현계획서 E2)."""

import json

from app.tools import proactive_eval


def test_detection_scores_hold(tmp_path):
    report = proactive_eval.run(tmp_path)
    assert report.fire_total >= 3 and report.quiet_total >= 10
    assert report.recall == 1.0, report.problems
    assert report.false_alarm_rate <= proactive_eval.FALSE_ALARM_LIMIT, report.problems
    assert report.quiet_wrong == 0, report.problems          # 합성 사례에서는 0이어야 한다


def test_every_quiet_case_says_why_and_the_file_is_frozen_data_only():
    data = json.loads((proactive_eval.FIXTURES / "cases.json").read_text(encoding="utf-8"))
    assert all(case["why"] for case in data["cases"])
    assert {case["expect"] for case in data["cases"]} == {"fire", "quiet"}
    assert "answers" not in (proactive_eval.FIXTURES / "cases.json").name
