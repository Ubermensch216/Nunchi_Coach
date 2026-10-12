"""연결 근거의 설명과 사람 판정 (지식그래프 계획 §9.1, P5).

화면이 보여 주는 것은 파생 색인이 아니라 **원문 근거가 붙은 주장**이다. 판정은
`kg_decisions`에 덧붙이기만 하고(되돌리기는 undo 결정), 원본 업무·문서·수행 표와
실행 승인(action 경로)은 건드리지 않는다. 그래프 관계를 '맞음'으로 눌러도 수행 완료나
공식 기한이 되지 않는다.

기능 플래그 `knowledge_review_ui`가 1일 때만 화면이 이 모듈을 부른다. 인계 v2(P7) 전에는
내부 사용으로만 열어 둔다 — 판정이 DB 백업에는 남고 인계에서 사라지면 안 된다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ...db.knowledge import KnowledgeStore
from .resolution import effective_decisions, latest_decision

FLAG = "knowledge_review_ui"
# 사람이 읽고 판정할 만한 관계. 구조 연결(HAS_CASE·PRECEDES 등)은 업무 화면의 다른 자리가 보여 준다.
REVIEWABLE = ("REQUIRES_FORM", "SUBMITTED_TO", "OWNED_BY", "due_date", "amount", "BASED_ON", "SUPERSEDES")
PREDICATE_LABEL = {
    "REQUIRES_FORM": "필요한 서식", "SUBMITTED_TO": "제출처", "OWNED_BY": "담당",
    "due_date": "기한", "amount": "금액", "BASED_ON": "근거 문서", "SUPERSEDES": "대체한 것",
}
DECISION_LABEL = {"confirm": "맞다고 확인", "reject": "아니라고 거절"}
_SCOPE_YEAR = re.compile(r"case:\d+:year:(\d{4})")


def enabled(db) -> bool:
    return db.get_meta(FLAG) == "1"


@dataclass
class Proof:
    filename: str
    doc_id: int | None
    locator: str
    quote: str
    current: bool
    kind: str


@dataclass
class Item:
    claim_uuid: str
    claim_key: str
    predicate: str
    title: str
    value: str
    year: int | None
    basis_state: str
    applicability: str
    lifecycle: str
    decision: str | None = None
    decision_id: str | None = None
    proofs: list[Proof] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)


def actor(db) -> str:
    name, role = db.reviewer()
    return f"{role}:{name}" if name else role


def _entity_label(db, uuid: str | None) -> str:
    if not uuid:
        return ""
    store = KnowledgeStore(db)
    canonical = store.canonical_entity(uuid)
    row = db.con.execute("SELECT label FROM kg_entities WHERE uuid=?", (canonical,)).fetchone()
    return row[0] if row else ""


def _claim_value(db, row) -> str:
    if row["object_id"]:
        return _entity_label(db, row["object_id"])
    value = json.loads(row["value_json"])
    if row["value_type"] == "decimal_amount" and isinstance(value, dict):
        return f"{value.get('amount')}{value.get('currency') or value.get('unit') or ''}"
    return str(value)


def _year(row) -> int | None:
    match = _SCOPE_YEAR.search(row["scope_key"] or "")
    if match:
        return int(match[1])
    for edge in (row["valid_from"], row["valid_to"]):
        if edge:
            return int(edge[:4])
    return None


def _proofs(db, claim_uuid: str) -> list[Proof]:
    rows = db.con.execute(
        "SELECT e.kind,e.doc_id,e.doc_hash,e.locator,e.exact_quote,d.filename,d.hash AS now_hash,d.missing_since "
        "FROM kg_claim_evidence s JOIN kg_evidence e ON e.uuid=s.evidence_id "
        "JOIN kg_generations g ON g.id=s.generation_id "
        "LEFT JOIN documents d ON d.id=e.doc_id "
        "WHERE s.claim_id=? AND s.active=1 AND g.active=1 AND s.support_role='supports' "
        "ORDER BY (e.kind='document_quote') DESC, e.rowid", (claim_uuid,)).fetchall()
    out = []
    for row in rows:
        if row["kind"] == "source_record" and not row["locator"]:
            continue        # 업무 표의 한 줄은 원문 인용이 아니다
        out.append(Proof(
            filename=row["filename"] or "원본 없음", doc_id=row["doc_id"], locator=row["locator"],
            quote=row["exact_quote"], kind=row["kind"],
            current=bool(row["now_hash"] and row["now_hash"] == row["doc_hash"] and not row["missing_since"])))
    return out


def _decision(db, claim_key: str):
    rows = KnowledgeStore(db).decisions_for(claim_key)
    last = latest_decision(rows)
    return last


def _links(db, claim_uuid: str) -> list[dict]:
    out = []
    for link in db.con.execute(
            "SELECT * FROM kg_claim_links WHERE from_claim=? OR to_claim=?", (claim_uuid, claim_uuid)):
        other = link["to_claim"] if link["from_claim"] == claim_uuid else link["from_claim"]
        row = db.con.execute("SELECT * FROM kg_claims WHERE uuid=?", (other,)).fetchone()
        if row is None:
            continue
        out.append({"kind": link["kind"], "predicate": row["predicate"],
                    "value": _claim_value(db, row), "year": _year(row),
                    # 대체 여부는 판정 전까지 확정하지 않는다.
                    "settled": False, "proofs": _proofs(db, other)})
    return out


def items_for_task(db, task_id: int) -> list[Item]:
    """업무 하나의 연결 근거. 거절한 것도 되돌릴 수 있게 함께 돌려준다."""
    partition = f"task:{task_id}"
    rows = db.con.execute(
        "SELECT DISTINCT c.* FROM kg_claims c JOIN kg_claim_evidence s ON s.claim_id=c.uuid "
        "JOIN kg_generations g ON g.id=s.generation_id "
        "WHERE g.partition_key=? AND g.active=1 AND s.active=1 AND c.lifecycle IN ('active','retracted') "
        f"AND c.predicate IN ({','.join('?' * len(REVIEWABLE))}) ORDER BY c.predicate, c.rowid",
        (partition, *REVIEWABLE)).fetchall()
    items = []
    for row in rows:
        last = _decision(db, row["claim_key"])
        proofs = _proofs(db, row["uuid"])
        items.append(Item(
            claim_uuid=row["uuid"], claim_key=row["claim_key"], predicate=row["predicate"],
            title=PREDICATE_LABEL[row["predicate"]], value=_claim_value(db, row), year=_year(row),
            basis_state=row["basis_state"], applicability=row["applicability"], lifecycle=row["lifecycle"],
            decision=last["decision"] if last is not None else None,
            decision_id=last["uuid"] if last is not None else None,
            proofs=proofs, links=_links(db, row["uuid"])))
    return items


# ── 동일 이름 후보 ──────────────────────────────────────────────────

def _pair_decision(db, first: str, second: str):
    """두 개체 사이의 가장 최근 유효 판정. 어느 쪽을 대상으로 눌렀든 같은 쌍으로 본다."""
    rows = []
    for target in (first, second):
        rows += db.con.execute(
            "SELECT rowid AS seq, * FROM kg_decisions WHERE target_kind='identity' AND target_key=? "
            "ORDER BY rowid", (target,)).fetchall()
    rows.sort(key=lambda r: r["seq"])
    other = {first: second, second: first}
    pair = []
    kept = set()
    for row in rows:
        mine = json.loads(row["after_json"]).get("entity_id") == other[row["target_key"]]
        if mine or (row["decision"] == "undo" and row["supersedes_decision"] in kept):
            pair.append(row)
            kept.add(row["uuid"])
    return latest_decision(pair)


def alias_candidates(db) -> list[dict]:
    """같은 유형·같은 이름인데 서로 다른 개체. 자동으로 합치지 않고 나란히 보여 준다."""
    groups = db.con.execute(
        "SELECT e.type,a.normalized_label FROM kg_aliases a JOIN kg_entities e ON e.uuid=a.entity_id "
        "WHERE e.lifecycle='active' AND e.type IN ('Organization','Role','Form') "
        "GROUP BY e.type,a.normalized_label HAVING COUNT(DISTINCT a.entity_id)>1 "
        "ORDER BY e.type,a.normalized_label").fetchall()
    out = []
    for group in groups:
        uuids = [r[0] for r in db.con.execute(
            "SELECT DISTINCT e.uuid FROM kg_aliases a JOIN kg_entities e ON e.uuid=a.entity_id "
            "WHERE e.lifecycle='active' AND e.type=? AND a.normalized_label=? ORDER BY e.rowid",
            (group["type"], group["normalized_label"]))]
        for index, first in enumerate(uuids):
            for second in uuids[index + 1:]:
                decided = _pair_decision(db, first, second)
                if decided is not None:
                    continue
                out.append({"type": group["type"], "first": _entity_info(db, first),
                            "second": _entity_info(db, second)})
    return out


def _entity_info(db, uuid: str) -> dict:
    row = db.con.execute("SELECT * FROM kg_entities WHERE uuid=?", (uuid,)).fetchone()
    tasks = sorted({t[0] for t in db.con.execute(
        "SELECT DISTINCT t.name FROM kg_claims c JOIN kg_bindings b ON b.entity_id=c.subject_id "
        "AND b.source_table='tasks' JOIN tasks t ON CAST(t.id AS TEXT)=b.source_key "
        "WHERE c.object_id=? AND c.lifecycle!='retracted'", (uuid,))})
    proofs = []
    for claim in db.con.execute("SELECT uuid FROM kg_claims WHERE object_id=? AND lifecycle!='retracted'", (uuid,)):
        proofs.extend(_proofs(db, claim[0]))
    return {"uuid": uuid, "label": row["label"], "type": row["type"], "scope": row["scope_key"],
            "tasks": tasks, "proofs": proofs}


# ── 판정 ────────────────────────────────────────────────────────────

def _claim(db, claim_uuid: str):
    row = db.con.execute("SELECT * FROM kg_claims WHERE uuid=?", (claim_uuid,)).fetchone()
    if row is None:
        raise ValueError("판정할 연결을 찾을 수 없습니다.")
    return row


def confirm(db, claim_uuid: str) -> str:
    """내용이 맞다는 기록. 수행 완료나 공식 기한의 승인이 아니다."""
    row = _claim(db, claim_uuid)
    with db.transaction():
        uid = KnowledgeStore(db).record_decision("claim", row["claim_key"], "confirm", actor(db),
                                                 before={"lifecycle": row["lifecycle"]})
        db.audit("knowledge.confirm", claim_uuid)
    return uid


def reject(db, claim_uuid: str) -> str:
    row = _claim(db, claim_uuid)
    with db.transaction():
        uid = KnowledgeStore(db).record_decision("claim", row["claim_key"], "reject", actor(db),
                                                 before={"lifecycle": row["lifecycle"]})
        db.audit("knowledge.reject", claim_uuid)
    return uid


def undo_claim(db, claim_uuid: str) -> str:
    """가장 최근의 유효한 판정을 취소한다. 취소할 판정이 없으면 ValueError."""
    row = _claim(db, claim_uuid)
    last = _decision(db, row["claim_key"])
    if last is None:
        raise ValueError("되돌릴 판정이 없습니다.")
    with db.transaction():
        uid = KnowledgeStore(db).record_decision("claim", row["claim_key"], "undo", actor(db),
                                                 supersedes_decision=last["uuid"])
        if last["decision"] == "reject":
            # 거절의 효과(retracted)는 다음 게시 때 풀리지만, 화면은 바로 되돌아가야 한다.
            db.con.execute("UPDATE kg_claims SET lifecycle='active' WHERE uuid=? AND lifecycle='retracted'",
                           (claim_uuid,))
        db.audit("knowledge.undo", claim_uuid)
    return uid


def merge(db, first: str, second: str) -> str:
    """두 개체를 같은 것으로 판정한다. 원래 UUID는 그대로 두고 읽을 때만 합쳐 본다."""
    with db.transaction():
        uid = KnowledgeStore(db).record_decision("identity", first, "merge", actor(db),
                                                 after={"entity_id": second})
        db.audit("knowledge.merge", f"{first}->{second}")
    return uid


def separate(db, first: str, second: str) -> str:
    """서로 다른 것이라는 판정. 같은 이름이어도 다시 후보로 올리지 않는다."""
    with db.transaction():
        uid = KnowledgeStore(db).record_decision("identity", first, "separate", actor(db),
                                                 after={"entity_id": second})
        db.audit("knowledge.separate", f"{first}|{second}")
    return uid


def undo_identity(db, first: str, second: str) -> str:
    last = _pair_decision(db, first, second)
    if last is None:
        raise ValueError("되돌릴 판정이 없습니다.")
    with db.transaction():
        uid = KnowledgeStore(db).record_decision("identity", last["target_key"], "undo", actor(db),
                                                 supersedes_decision=last["uuid"])
        db.audit("knowledge.undo", f"{first}|{second}")
    return uid


def history(db, limit: int = 30) -> list:
    return db.con.execute("SELECT * FROM kg_decisions ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()


__all__ = ["enabled", "items_for_task", "alias_candidates", "confirm", "reject", "undo_claim",
           "merge", "separate", "undo_identity", "history", "effective_decisions"]
