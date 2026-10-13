from collections import Counter
import hashlib

import pytest

from app.tools.knowledge_eval import FIXTURES, load_fixture, run

PARAPHRASE = FIXTURES.parent / 'knowledge_paraphrase'


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


def test_shadow_preserves_frozen_baseline_and_reports_projection_limits():
    baseline = run('holdout')
    shadow = run('holdout', 'shadow')
    assert shadow['scope_leaks'] == 0 and shadow['graph_added_questions'] == 0
    assert shadow['categories'] == baseline['categories']
    assert shadow['graph_current_claims'] > 0
    assert [r['retrieved'] for r in shadow['results']] == [r['retrieved'] for r in baseline['results']]
    # 서술문 항목을 읽은 뒤에도 시드를 못 얻는 질문(어휘 검색이 아무것도 못 찾은 것)은 기존 검색으로 물러난다.
    assert 0 < shadow['graph_fallbacks'] < shadow['questions']


def _recall(result, category, keep=lambda row: True):
    rows = [r for r in result['results'] if r['category'] == category and r['recall_at_10'] is not None and keep(r)]
    return sum(r['recall_at_10'] for r in rows) / len(rows)


def test_graph_gate_on_frozen_holdout():
    """상세구현계획 §12.3: 연결 +5%p 이상, 단순 조회 하락 2%p 이내, 범위 밖 노출 0."""
    off, on = run('holdout'), run('holdout', 'on')
    assert on['scope_leaks'] == 0
    assert _recall(on, 'linked') - _recall(off, 'linked') >= 0.05
    assert _recall(off, 'simple') - _recall(on, 'simple') <= 0.02
    for category in ('alias', 'temporal'):
        assert _recall(on, category) >= _recall(off, category)
    assert on['graph_p95_ms'] <= 150


def test_paraphrase_set_generalizes_to_other_sentence_shapes_and_states_its_limit():
    corpus, manifest = load_fixture(PARAPHRASE)
    style = {g['id']: g['style'] for g in corpus['groups']}
    assert manifest['holdout'] == len(corpus['questions'])
    off, on = run('all', 'off', PARAPHRASE), run('all', 'on', PARAPHRASE)
    assert on['scope_leaks'] == 0
    assert _recall(on, 'simple') == _recall(off, 'simple') == 1.0

    def by_style(wanted):
        return lambda row: style[row['id'].split('-')[0]] in wanted
    # 이름표 줄·다른 이름표의 서술문·동사형 문장에서는 연결이 올라간다.
    assert _recall(on, 'linked', by_style({1, 2, 3})) - _recall(off, 'linked', by_style({1, 2, 3})) >= 0.3
    # 자유 서술(문체 4)은 규칙이 읽지 못한다 — 개선을 주장하지 않는다.
    assert _recall(on, 'linked', by_style({4})) == _recall(off, 'linked', by_style({4}))


def test_graph_precision_cost_is_reported_not_hidden():
    """그래프가 문맥에 문서를 더 올리는 비용(정밀도)을 지표로 남긴다. 줄었다고 주장하지 않는다."""
    off, on = run('all', 'off', PARAPHRASE), run('all', 'on', PARAPHRASE)
    for category in ('simple', 'linked', 'missing'):
        before, after = off['categories'][category], on['categories'][category]
        assert before['documents_per_question'] is not None
        assert after['documents_per_question'] >= before['documents_per_question']
    # 근거 없는 질문에도 같은 업무의 문서가 더 붙는다 — 기본 off의 이유 중 하나.
    assert (on['categories']['missing']['unexpected_documents_per_question']
            > off['categories']['missing']['unexpected_documents_per_question'])
