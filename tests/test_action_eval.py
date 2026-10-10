"""합성 시나리오 평가가 돌고, 1단계 수치가 문턱을 넘는다 (구현계획서 8절).

이 시나리오는 문서 역할 규칙을 한 번 고치는 데 썼으므로 역할 수치는 개발용이다
(구현 기록 참조). 여기서는 수치가 뒤로 가지 않는지만 본다.
"""

import json

from app.core import docrole
from app.jobs.pipeline import STAGE_ROLES, Pipeline
from app.tools import action_eval


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
