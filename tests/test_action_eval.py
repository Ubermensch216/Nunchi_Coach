"""합성 시나리오 평가가 돌고, 1단계 수치가 문턱을 넘는다 (구현계획서 8절).

이 시나리오는 문서 역할 규칙을 한 번 고치는 데 썼으므로 역할 수치는 개발용이다
(구현 기록 참조). 여기서는 수치가 뒤로 가지 않는지만 본다.
"""

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtWidgets import QApplication

from app.core import docrole
from app.jobs.pipeline import STAGE_ROLES, Pipeline
from app.tools import action_eval


@pytest.fixture(scope="module", autouse=True)
def qapp():
    # 평가 도구는 앱이 없으면 QCoreApplication을 만든다. 그것이 먼저 생기면 뒤의
    # 화면 시험이 QApplication을 얻지 못한다 — 여기서 먼저 만들어 둔다.
    return QApplication.instance() or QApplication([])


def test_memory_scores_hold(tmp_path):
    [result] = action_eval.run(tmp_path)
    thresholds = json.loads((action_eval.FIXTURES / "answers.json").read_text(
        encoding="utf-8"))["thresholds"]
    assert result.ratio("role") >= thresholds["role_accuracy"]
    for name in ("years", "revisions", "indicator_rows", "originals"):
        assert result.ratio(name) == 1.0, (name, result.problems)


def test_role_stage_is_incremental_and_follows_changed_documents(tmp_path):
    corpus = json.loads((action_eval.FIXTURES / "corpus.json").read_text(encoding="utf-8"))
    scenario = corpus["scenarios"][0]
    source = tmp_path / "자료"
    action_eval.write_corpus(source, scenario["documents"])
    db, _task_id, by_path = action_eval.analyse(
        tmp_path / "project", source, scenario, action_eval.date(2026, 10, 10),
        scenario["documents"])
    try:
        worker = Pipeline(db.path)
        reports = []
        worker.stage_done.connect(reports.append)
        assert docrole.pending(db) == []
        worker._roles(db)
        assert reports[-1].stage == STAGE_ROLES and reports[-1].total == 0
        assert "판 묶음 1개" in reports[-1].note

        # 사람이 정한 역할은 남고, 내용이 바뀐 문서만 다시 본다.
        kept = by_path["2025/2025_규제혁신평가_참고메모.txt"]
        changed = by_path["2025/2025_규제혁신평가_평가지표.hwpx"]
        docrole.set_role(db, kept, docrole.EVIDENCE)
        db.invalidate_document_analysis(kept)
        db.invalidate_document_analysis(changed)
        assert docrole.roles(db, [kept])[kept]["role"] == docrole.EVIDENCE
        assert changed not in docrole.roles(db, [changed])
    finally:
        db.close()


def test_workflow_scores_hold(tmp_path):
    """2단계 — 문턱은 answers.json의 thresholds. 틀린 비교와 근거 없는 올해 값은 0건이어야 한다."""
    [result] = action_eval.run(tmp_path)
    thresholds = json.loads((action_eval.FIXTURES / "answers.json").read_text(
        encoding="utf-8"))["thresholds"]
    assert result.ratio("discover_recall") >= thresholds["discover_recall"], result.problems
    assert result.ratio("discover_precision") >= thresholds["discover_precision"], result.problems
    assert result.counts["wrong_comparisons"] == thresholds["wrong_comparisons"], result.problems
    assert result.counts["unfounded_current_values"] == thresholds["unfounded_current_values"], result.problems
    assert result.counts["official_wrong_comparisons"] == 0, result.problems
    for name in ("run_state", "discover_suspect", "target_unconfirmed", "form_mode", "must_say",
                 "form_sheets", "form_columns", "checklist_no_due", "checklist_first",
                 "procedure_added", "injection_ignored", "official_new_run", "official_run_state",
                 "official_basis", "official_form_mode", "old_artifacts_superseded",
                 "official_due_in_checklist", "originals"):
        assert result.ratio(name) == 1.0, (name, result.problems)


def test_followup_scores_hold(tmp_path):
    """4단계 — 요청문 초안·서식 확정·회신 취합. 점검·증빙 건수가 정답과 다르면 안 되고, 인정이라는 말이 없어야 한다."""
    [result] = action_eval.run(tmp_path)
    for name in ("request_waits", "request_text", "request_due_blank", "request_hwpx_opens",
                 "form_not_final_before_approval", "request_completes", "form_final_after_approval",
                 "intake_completes", "intake_rows", "intake_files", "intake_problem_files",
                 "duplicate_groups", "depts", "replies_untouched", "intake_idempotent"):
        assert result.ratio(name) == 1.0, (name, result.problems)
    for name in ("wrong_issue_counts", "wrong_evidence_counts", "approval_words"):
        assert result.counts[name] == 0, (name, result.problems)
