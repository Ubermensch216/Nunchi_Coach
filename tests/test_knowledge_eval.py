from collections import Counter
import hashlib

import pytest

from app.tools.knowledge_eval import FIXTURES, load_fixture, run


def test_frozen_questions_groups_and_exact_gold_anchors():
    corpus, manifest = load_fixture()
    assert Counter(q["category"] for q in corpus["questions"]) == {"simple": 30, "alias": 20, "linked": 25, "temporal": 25, "missing": 20}
    assert Counter(q["split"] for q in corpus["questions"]) == {"tuning": 40, "holdout": 80}
    groups = {g["id"]: g for g in corpus["groups"]}
    assert {q["group"] for q in corpus["questions"] if q["split"] == "tuning"}.isdisjoint(
        {q["group"] for q in corpus["questions"] if q["split"] == "holdout"})
    for q in corpus["questions"]:
        assert q["split"] == groups[q["group"]]["split"]
        documents = {d["key"]: d for d in groups[q["group"]]["documents"]}
        for e in q["expected_evidence"]:
            doc = documents[e["document"]]
            assert e["sha256"] == hashlib.sha256(doc["text"].encode()).hexdigest()
            assert doc["text"][e["offsets"]["start"]:e["offsets"]["end"]] == e["exact_quote"]
    assert manifest["graph_enabled"] is False


def test_fixture_hash_mismatch_rejected(tmp_path):
    (tmp_path / "corpus.json").write_bytes((FIXTURES / "corpus.json").read_bytes() + b" ")
    (tmp_path / "manifest.json").write_bytes((FIXTURES / "manifest.json").read_bytes())
    with pytest.raises(ValueError, match="해시"):
        load_fixture(tmp_path)


def test_existing_retrieval_baseline_records_failures_without_claiming_answers():
    result = run("holdout")
    assert result["questions"] == 80 and result["scope_leaks"] == 0
    assert result["categories"]["missing"]["recall_at_10"] is None
    assert result["comparison"] == "A" and "LLM 답변 정확도" in result["unmeasured"]
    assert any(r["recall_at_10"] is not None and r["recall_at_10"] < 1 for r in result["results"])
