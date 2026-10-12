"""사람의 연결 근거 판정이 인계본(형식 2)에 실려 받는 프로젝트에서 다시 이어지는지 (지식그래프 P7)."""
import copy
import json
import zipfile
from pathlib import Path

import pytest

from test_transfer import (NOTICE, REQUEST, qapp, put, receiver, rewrite, resigned,  # noqa: F401
                           sender)
from app.core import handover_release as release, transfer
from app.core.knowledge import lifecycle, portable, projection, review
from app.db.knowledge import KnowledgeStore


def claim_of(db, predicate):
    return db.con.execute("SELECT * FROM kg_claims WHERE predicate=? ORDER BY rowid", (predicate,)).fetchone()


def decide(sender_tuple):
    """전임자가 마감일은 거절하고 제출처는 맞다고 확인한 뒤 인계본을 다시 만든다."""
    db, _package, _told = sender_tuple
    projection.project_task(db, 1)
    due = claim_of(db, "due_date")
    review.reject(db, due["uuid"])
    review.confirm(db, claim_of(db, "SUBMITTED_TO")["uuid"])
    snapshot_id, _ = release.publish(db, "연결 근거 판정 포함")
    package = db.path.parent / "outputs" / "인계v2.zip"
    return transfer.export_package(db, snapshot_id, package), due


def drain(db):
    result = lifecycle.run_jobs(db, seconds=30, max_jobs=1000)
    assert not result.failed, result.diagnostics
    return result


def prepare_receiver(receiver):
    db, root = receiver
    put(db, root, "2025/요청.hwpx", REQUEST, task=None)
    put(db, root, "2025/시행공문.hwpx", NOTICE, when="2025-09-02", task=None)
    return db


def decisions(db):
    return db.con.execute("SELECT * FROM kg_decisions ORDER BY rowid").fetchall()


def test_without_decisions_the_package_stays_format_one(sender):
    _db, package, _told = sender
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        payload = json.loads(archive.read("snapshot.json"))
    assert manifest["package_format"] == 1 and payload["format"] == 1 and "knowledge" not in payload


def test_decisions_make_format_two_with_descriptors_not_local_ids(sender, tmp_path):
    package, due = decide(sender)
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        payload = json.loads(archive.read("snapshot.json"))
        blob = archive.read("snapshot.json").decode("utf-8") + archive.read("report.md").decode("utf-8")
    assert manifest["package_format"] == 2 and payload["format"] == 2
    assert [d["decision"] for d in payload["knowledge"]["decisions"]] == ["reject", "confirm"]
    target = payload["knowledge"]["decisions"][0]["target"]
    assert target["predicate"] == "due_date" and target["subject"]["task"] == "정기 실적보고"
    assert target["value"] == "2025-09-30" and "task:1" not in json.dumps(target)   # 번호가 아니라 설명으로 적는다
    assert due["claim_key"] not in blob and str(tmp_path) not in blob
    assert "연결 근거에 대한 사람의 판정" in blob and "판정 2건" in blob


def test_decisions_apply_once_the_receiver_has_projected_the_same_targets(sender, receiver):
    package, _due = decide(sender)
    db = prepare_receiver(receiver)
    result = transfer.import_package(db, package, receiver_name="이후임")
    assert result["decisions"]["received"] == 2
    drain(db)
    assert portable.pending_count(db) == 0
    assert claim_of(db, "due_date")["lifecycle"] == "retracted"      # 전임자의 거절이 받는 쪽 근거에서도 빠진다
    recipient = claim_of(db, "SUBMITTED_TO")
    mine = [d for d in decisions(db) if d["target_key"] == recipient["claim_key"]]
    assert [d["decision"] for d in mine] == ["confirm"] and mine[0]["actor"].startswith("가져옴:")
    assert {r["state"] for r in portable.imported_state(db)} == {"applied"}


def test_importing_twice_applies_each_decision_once(sender, receiver):
    package, _due = decide(sender)
    db = prepare_receiver(receiver)
    transfer.import_package(db, package)
    drain(db)
    transfer.import_package(db, package)
    drain(db)
    assert len(decisions(db)) == 2 and len(portable.imported_state(db)) == 2


def test_undo_follows_only_an_applied_decision(sender, receiver):
    db_s, _package, _told = sender
    projection.project_task(db_s, 1)
    due = claim_of(db_s, "due_date")
    review.reject(db_s, due["uuid"])
    review.undo_claim(db_s, due["uuid"])
    block = portable.export_decisions(db_s)
    assert [d["decision"] for d in block["decisions"]] == ["reject", "undo"]
    db = prepare_receiver(receiver)
    transfer.import_package(db, _package)
    drain(db)
    portable.import_decisions(db, block)
    drain(db)
    assert claim_of(db, "due_date")["lifecycle"] == "active"          # 거절 뒤 되돌림까지 그대로 따라간다
    assert {r["state"] for r in portable.imported_state(db)} == {"applied"}


def test_receiver_decision_made_later_is_kept(sender, receiver):
    db_s, package, _told = sender
    projection.project_task(db_s, 1)
    review.reject(db_s, claim_of(db_s, "due_date")["uuid"])
    review.confirm(db_s, claim_of(db_s, "SUBMITTED_TO")["uuid"])
    block = copy.deepcopy(portable.export_decisions(db_s))
    for item in block["decisions"]:
        item["at"] = "2000-01-01 00:00:00"                              # 받는 쪽의 판정이 확실히 더 나중
    db = prepare_receiver(receiver)
    transfer.import_package(db, package)
    drain(db)
    review.confirm(db, claim_of(db, "due_date")["uuid"])                # 후임자가 같은 연결을 직접 맞다고 확인
    portable.import_decisions(db, block)
    states = {json.loads(r["payload"])["decision"]: r["state"] for r in portable.imported_state(db)}
    assert states == {"reject": "kept_local", "confirm": "applied"}
    assert claim_of(db, "due_date")["lifecycle"] == "active"            # 후임자의 판정이 남는다


def test_unmatched_decision_is_preserved_not_dropped(sender, receiver):
    package, _due = decide(sender)
    db, _root = receiver                      # 원문·분석 없이 가져온다
    db.con.execute("INSERT INTO tasks(id,name) VALUES (9,'전혀 다른 업무')")
    block = json.loads(zipfile.ZipFile(package).read("snapshot.json"))["knowledge"]
    result = portable.import_decisions(db, block)
    assert result["received"] == 2 and result["applied"] == 0
    rows = portable.imported_state(db)
    assert len(rows) == 2 and all(r["state"] == "pending" and r["note"] for r in rows)
    assert decisions(db) == []                                         # 추측해서 다른 대상에 걸지 않는다


def test_same_description_found_twice_is_never_guessed(sender, receiver):
    package, _due = decide(sender)
    db = prepare_receiver(receiver)
    transfer.import_package(db, package)
    drain(db)
    db.con.execute("DELETE FROM kg_imported_decisions")
    # 같은 업무에 이름이 같은 단계가 둘이면 두 연결의 설명이 같아진다.
    db.sync_work_steps(1, [{"slot": "a", "ordinal": 1, "label": "집계"},
                           {"slot": "b", "ordinal": 2, "label": "집계"}])
    projection.project_task(db, 1)
    claims = db.con.execute("SELECT * FROM kg_claims WHERE predicate='HAS_STEP' AND lifecycle='active'").fetchall()
    twins = [c for c in claims if portable.claim_descriptor(db, c)["object"]["label"] == "집계"]
    assert len(twins) == 2
    descriptor = portable.claim_descriptor(db, twins[0])
    assert portable.claim_descriptor(db, twins[1]) == descriptor
    block = {"ontology_version": "1", "project_uuid": "other", "decisions": [{
        "uuid": "x1", "kind": "claim", "decision": "reject", "actor": "전임자", "at": "2026-01-01 00:00:00",
        "supersedes": None, "target": descriptor, "other": None, "before": {}, "after": {}}]}
    before = len(decisions(db))
    portable.import_decisions(db, block)
    row, = portable.imported_state(db)
    assert row["state"] == "pending" and "여럿" in row["note"]
    assert len(decisions(db)) == before                              # 어느 쪽에도 걸지 않았다
    assert all(db.con.execute("SELECT lifecycle FROM kg_claims WHERE uuid=?", (c["uuid"],)).fetchone()[0] == "active"
               for c in twins)


def test_identity_decisions_travel_by_description(sender):
    db, _package, _told = sender
    db.con.execute("INSERT INTO tasks(id,name) VALUES (2,'다른 업무')")
    for task in (1, 2):
        db.sync_task_items(task, [{"kind": "form", "slot": "form", "content": "집계표", "basis_state": "stated",
                                   "applicability": "past", "evidence": []}])
        projection.project_task(db, task)
    pair, = [p for p in review.alias_candidates(db) if p["type"] == "Form"]
    review.separate(db, pair["first"]["uuid"], pair["second"]["uuid"])
    item = portable.export_decisions(db)["decisions"][0]
    assert item["kind"] == "identity" and item["target"]["type"] == item["other"]["type"] == "Form"
    assert {item["target"]["task"], item["other"]["task"]} == {"정기 실적보고", "다른 업무"}


def test_malformed_knowledge_block_is_refused_without_writing(sender, receiver, tmp_path):
    package, _due = decide(sender)
    db = prepare_receiver(receiver)
    for change in (lambda p: p["knowledge"]["decisions"][0].update(decision="삭제"),
                   lambda p: p["knowledge"]["decisions"][0].update(kind="table"),
                   lambda p: p["knowledge"]["decisions"].append(copy.deepcopy(p["knowledge"]["decisions"][0])),
                   lambda p: p["knowledge"].update(decisions="없음")):
        bad = rewrite(package, tmp_path / "bad.zip", resigned(change))
        with pytest.raises(transfer.PackageError):
            transfer.import_package(db, bad)
    assert db.con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert db.con.execute("SELECT COUNT(*) FROM kg_imported_decisions").fetchone()[0] == 0


def test_format_one_cannot_carry_decisions_and_old_packages_still_import(sender, receiver, tmp_path):
    package, _due = decide(sender)

    def downgrade(members):
        resigned(lambda payload: payload.update(format=1))(members)
        manifest = json.loads(members["manifest.json"])
        manifest["package_format"] = 1
        members["manifest.json"] = json.dumps(manifest).encode("utf-8")

    smuggled = rewrite(package, tmp_path / "s.zip", downgrade)
    db = prepare_receiver(receiver)
    with pytest.raises(transfer.PackageError, match="형식 1 묶음"):
        transfer.import_package(db, smuggled)
    _s, plain, _t = sender                    # 판정이 없는 옛 형식 묶음은 그대로 받는다
    assert transfer.import_package(db, plain)["tasks"] == 1


def test_receipt_text_reports_decisions(sender, receiver, qapp):
    from app.ui.snapshot_dialog import SnapshotDialog
    package, _due = decide(sender)
    db = prepare_receiver(receiver)
    result = transfer.import_package(db, package)
    text = SnapshotDialog.receipt_text(result)
    assert "사람의 판정 2건을 받았습니다" in text and "다시 잇습니다" in text
