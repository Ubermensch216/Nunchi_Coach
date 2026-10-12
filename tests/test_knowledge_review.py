"""연결 근거의 설명과 사람 판정 (지식그래프 P5)."""
import json

import pytest

from test_knowledge_projection import db, source_snapshot   # noqa: F401 — 같은 합성 업무를 쓴다
from app.core.knowledge import lifecycle, projection, review
from app.db.knowledge import KnowledgeStore


@pytest.fixture
def projected(db):
    projection.project_task(db, 1)
    return db


def by_predicate(db, predicate):
    return next(i for i in review.items_for_task(db, 1) if i.predicate == predicate)


def test_items_show_value_year_state_and_original_quote(projected):
    items = review.items_for_task(projected, 1)
    assert {i.predicate for i in items} == {"REQUIRES_FORM", "SUBMITTED_TO", "due_date", "amount"}
    due = by_predicate(projected, "due_date")
    assert due.value == "2026-11-01" and due.title == "기한"
    assert due.applicability == "past" and due.decision is None
    proof = due.proofs[0]
    assert proof.filename == "a.txt" and proof.quote == "제출기한: 2026-11-01" and proof.current
    assert by_predicate(projected, "SUBMITTED_TO").value == "복지과"
    assert by_predicate(projected, "amount").value == "1000원"


def test_source_change_marks_proof_not_current(projected):
    projected.con.execute("UPDATE documents SET hash='hash-b' WHERE id=1")
    lifecycle.run_jobs(projected, seconds=30)
    items = review.items_for_task(projected, 1)
    assert all(not p.current for i in items for p in i.proofs)    # 낡은 근거는 최신으로 보이지 않는다


def test_confirm_reject_undo_leave_sources_and_actions_untouched(projected):
    before = source_snapshot(projected)
    runs = projected.con.execute("SELECT COUNT(*) FROM action_runs").fetchone()[0]
    claim = by_predicate(projected, "due_date")
    confirmed = review.confirm(projected, claim.claim_uuid)
    assert by_predicate(projected, "due_date").decision == "confirm"
    review.reject(projected, claim.claim_uuid)
    rejected = by_predicate(projected, "due_date")
    assert rejected.decision == "reject" and rejected.lifecycle == "retracted"   # 거절하면 곧 근거에서 빠진다
    review.undo_claim(projected, claim.claim_uuid)
    again = by_predicate(projected, "due_date")
    assert again.decision == "confirm" and again.lifecycle == "active"        # 직전 판정(맞음)으로 돌아간다
    review.undo_claim(projected, claim.claim_uuid)
    assert by_predicate(projected, "due_date").decision is None
    with pytest.raises(ValueError):
        review.undo_claim(projected, claim.claim_uuid)
    assert source_snapshot(projected) == before      # 판정은 원본 업무·문서를 바꾸지 않는다
    assert projected.con.execute("SELECT COUNT(*) FROM action_runs").fetchone()[0] == runs
    assert confirmed and projected.con.execute("SELECT COUNT(*) FROM kg_decisions").fetchone()[0] == 4


def test_decisions_survive_reprojection_and_rejected_claim_stays_out_of_search(projected):
    claim = by_predicate(projected, "due_date")
    review.reject(projected, claim.claim_uuid)
    lifecycle.run_jobs(projected, seconds=30)
    after = by_predicate(projected, "due_date")
    assert after.lifecycle == "retracted" and after.decision == "reject"
    assert projected.con.execute("SELECT COUNT(*) FROM kg_current_claims WHERE uuid=?",
                                 (claim.claim_uuid,)).fetchone()[0] == 0


def test_unknown_claim_is_refused(projected):
    with pytest.raises(ValueError):
        review.confirm(projected, "no-such-claim")


def make_twin(db):
    """다른 업무의 같은 이름 제출처 — 같은 이름이지만 같은 기관이라는 증거는 없다."""
    db.con.execute("INSERT INTO tasks(id,name) VALUES (2,'다른 업무')")
    db.con.execute("INSERT INTO task_docs(task_id,doc_id,origin) VALUES (2,1,'user')")
    db.sync_task_items(2, [{"kind": "recipient", "slot": "recipient", "content": "복지과",
                            "basis_state": "stated", "applicability": "past",
                            "evidence": [(1, "본문", "제출처: 복지과")]}])
    projection.project_task(db, 2)


def test_same_name_entities_are_listed_not_merged(projected):
    make_twin(projected)
    found = review.alias_candidates(projected)
    assert len(found) == 1 and found[0]["type"] == "Organization"
    first, second = found[0]["first"], found[0]["second"]
    assert first["uuid"] != second["uuid"] and first["label"] == second["label"] == "복지과"
    assert {first["tasks"][0], second["tasks"][0]} == {"신청 업무", "다른 업무"}
    assert KnowledgeStore(projected).canonical_entity(first["uuid"]) == first["uuid"]   # 후보일 뿐


def test_merge_and_separate_are_explicit_reversible_and_exclude_the_pair(projected):
    make_twin(projected)
    pair = review.alias_candidates(projected)[0]
    a, b = pair["first"]["uuid"], pair["second"]["uuid"]
    review.merge(projected, a, b)
    assert KnowledgeStore(projected).canonical_entity(a) == b
    assert review.alias_candidates(projected) == []
    review.undo_identity(projected, a, b)
    assert KnowledgeStore(projected).canonical_entity(a) == a
    assert len(review.alias_candidates(projected)) == 1
    review.separate(projected, a, b)
    assert review.alias_candidates(projected) == []                     # 거절한 쌍은 다시 올리지 않는다
    assert KnowledgeStore(projected).canonical_entity(a) == a
    review.undo_identity(projected, b, a)                               # 방향과 무관하게 되돌린다
    assert len(review.alias_candidates(projected)) == 1


def test_merge_requires_same_type(projected):
    form = projected.con.execute("SELECT uuid FROM kg_entities WHERE type='Form'").fetchone()[0]
    org = projected.con.execute("SELECT uuid FROM kg_entities WHERE type='Organization'").fetchone()[0]
    with pytest.raises(ValueError):
        review.merge(projected, form, org)


def test_flag_is_off_by_default(projected):
    assert not review.enabled(projected)
    projected.set_meta(review.FLAG, "1")
    assert review.enabled(projected)
