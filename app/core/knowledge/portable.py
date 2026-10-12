"""사람의 그래프 판정을 인계본에 싣고 받는 쪽에서 다시 잇는다 (지식그래프 계획 §9.2, P7).

수신 프로젝트의 UUID·정수 ID는 보내는 쪽과 무관하다. 그래서 판정의 대상은 번호가 아니라
**설명자(descriptor)** — 개체의 유형·이름·소속 업무·문서 해시, 주장의 관계·끝점·값·범위 —
로 적고, 받는 쪽에서 같은 설명자를 가진 대상이 **정확히 하나**일 때만 잇는다.

  · 없거나 여럿이면 잇지 않는다. 판정은 'pending'으로 보존하고, 그래프가 새로 투영된 뒤
    다시 시도한다(apply_pending). 추측으로 합치지 않는다.
  · 받는 쪽에 더 나중의 사용자 판정이 이미 있으면 그것을 지키고 'kept_local'로 기록한다.
  · 같은 판정을 두 번 가져와도 한 번만 적용한다(source_uuid).
  · 되돌리기(undo)는 가져온 판정이 실제로 적용된 경우에만 따라간다.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable

from ...db.knowledge import KnowledgeStore
from .ontology import canonical_json, fingerprint, load, normalize_label, normalize_literal
from .resolution import latest_decision

MAX_DECISIONS = 50_000
DECISION_KINDS = ("confirm", "reject", "merge", "separate", "alias", "correct", "undo", "recheck")
TARGET_KINDS = ("entity", "alias", "claim", "identity", "evidence")
_LOCAL_ID = re.compile(r"\b(task|case|action_run|action_step|artifact|document):[^:]+")


def normalize_scope(scope: str) -> str:
    """정수 ID·UUID를 걷어 낸 범위. 'task:7:item:deadline' → 'task:item:deadline'."""
    return _LOCAL_ID.sub(r"\1", scope or "")


# ── 설명자 ──────────────────────────────────────────────────────────

_TASK_OF = {
    "tasks": "SELECT name, NULL AS year FROM tasks WHERE id=?",
    "work_cases": "SELECT t.name, c.period_year AS year FROM work_cases c JOIN tasks t ON t.id=c.task_id WHERE c.id=?",
    "work_steps": "SELECT t.name, NULL AS year FROM work_steps s JOIN tasks t ON t.id=s.task_id WHERE s.id=?",
    "task_steps": "SELECT t.name, s.year AS year FROM task_steps s JOIN tasks t ON t.id=s.task_id WHERE s.id=?",
    "action_steps": ("SELECT t.name, NULL AS year FROM action_steps s JOIN action_runs r ON r.id=s.run_id "
                     "LEFT JOIN work_cases c ON c.id=r.case_id JOIN tasks t ON t.id=COALESCE(r.task_id,c.task_id) "
                     "WHERE s.id=?"),
    "artifacts": ("SELECT t.name, NULL AS year FROM artifacts a LEFT JOIN work_cases c ON c.id=a.case_id "
                  "LEFT JOIN action_runs r ON r.id=a.run_id JOIN tasks t ON t.id=COALESCE(c.task_id,r.task_id) "
                  "WHERE a.id=?"),
}


def entity_descriptor(db, uuid: str) -> dict | None:
    entity = db.con.execute("SELECT type,label FROM kg_entities WHERE uuid=?", (uuid,)).fetchone()
    if entity is None:
        return None
    desc = {"type": entity["type"], "label": normalize_label(entity["label"]), "task": None,
            "year": None, "doc_hash": None}
    binding = db.con.execute("SELECT source_table,source_key FROM kg_bindings WHERE entity_id=? AND tombstone=0 "
                             "ORDER BY rowid LIMIT 1", (uuid,)).fetchone()
    if binding is None:
        return desc
    table, key = binding["source_table"], binding["source_key"]
    if table == "document_versions":
        desc["doc_hash"] = key.rsplit(":", 1)[-1]
    elif table == "task_item_values":
        row = db.con.execute("SELECT t.name FROM task_items i JOIN tasks t ON t.id=i.task_id WHERE i.id=?",
                             (key.split(":", 1)[0],)).fetchone()
        desc["task"] = row[0] if row else None
    elif table in _TASK_OF:
        row = db.con.execute(_TASK_OF[table], (key,)).fetchone()
        if row:
            desc["task"], desc["year"] = row["name"], row["year"]
    return desc


def claim_descriptor(db, claim) -> dict | None:
    subject = entity_descriptor(db, claim["subject_id"])
    if subject is None:
        return None
    obj = entity_descriptor(db, claim["object_id"]) if claim["object_id"] else None
    value = normalize_literal(json.loads(claim["value_json"]), claim["value_type"]) if claim["value_json"] else None
    return {"predicate": claim["predicate"], "subject": subject, "object": obj, "value": value,
            "value_type": claim["value_type"], "scope": normalize_scope(claim["scope_key"]),
            "from": claim["valid_from"], "to": claim["valid_to"],
            "conditions": json.loads(claim["conditions"] or "{}")}


def _sig(descriptor: dict) -> str:
    return fingerprint(descriptor)


class _Index:
    """수신 프로젝트의 설명자 → 대상 목록. 가져오는 동안 한 번만 만든다."""

    def __init__(self, db):
        self.db = db
        self._entities: dict[str, list[str]] | None = None
        self._claims: dict[str, list[str]] | None = None

    def entities(self, descriptor: dict) -> list[str]:
        if self._entities is None:
            self._entities = {}
            for row in self.db.con.execute("SELECT uuid FROM kg_entities WHERE lifecycle!='retracted'"):
                desc = entity_descriptor(self.db, row[0])
                if desc is not None:
                    self._entities.setdefault(_sig(desc), []).append(row[0])
        return self._entities.get(_sig(descriptor), [])

    def claims(self, descriptor: dict) -> list[str]:
        if self._claims is None:
            self._claims = {}
            for row in self.db.con.execute("SELECT * FROM kg_claims"):
                desc = claim_descriptor(self.db, row)
                if desc is not None:
                    self._claims.setdefault(_sig(desc), []).append(row["claim_key"])
        return self._claims.get(_sig(descriptor), [])


# ── 내보내기 ────────────────────────────────────────────────────────

def export_decisions(db) -> dict:
    """인계본에 싣는 판정 이력. 대상은 설명자로 적고, 대상이 사라졌으면 설명자 없이 보존한다."""
    rows = db.con.execute("SELECT rowid AS seq,* FROM kg_decisions ORDER BY rowid").fetchall()
    decisions = []
    for row in rows:
        after = json.loads(row["after_json"])
        item = {"uuid": row["uuid"], "kind": row["target_kind"], "decision": row["decision"],
                "actor": row["actor"], "at": row["created_at"], "supersedes": row["supersedes_decision"],
                "target": None, "other": None, "before": json.loads(row["before_json"]),
                "after": {k: v for k, v in after.items() if k != "entity_id"}}
        if row["target_kind"] == "claim":
            claim = db.con.execute("SELECT * FROM kg_claims WHERE claim_key=?", (row["target_key"],)).fetchone()
            item["target"] = claim_descriptor(db, claim) if claim else None
        elif row["target_kind"] == "identity":
            item["target"] = entity_descriptor(db, row["target_key"])
            if after.get("entity_id"):
                item["other"] = entity_descriptor(db, after["entity_id"])
        decisions.append(item)
    return {"ontology_version": load()["version"], "project_uuid": db.get_meta("project_uuid") or "",
            "decisions": decisions}


def summarize(block: dict) -> dict:
    counts = {"decisions": 0, "confirm": 0, "reject": 0, "merge": 0, "separate": 0, "undo": 0}
    for item in block.get("decisions", []):
        counts["decisions"] += 1
        if item["decision"] in counts:
            counts[item["decision"]] += 1
    return counts


def validate(block) -> dict:
    """묶음 속 판정 블록의 모양을 검사한다. 이상하면 ValueError(메시지는 한글)."""
    if not isinstance(block, dict) or not isinstance(block.get("decisions"), list):
        raise ValueError("연결 근거 판정 블록의 형식이 맞지 않습니다.")
    if len(block["decisions"]) > MAX_DECISIONS:
        raise ValueError("연결 근거 판정이 지나치게 많습니다.")
    seen = set()
    for item in block["decisions"]:
        if (not isinstance(item, dict) or not isinstance(item.get("uuid"), str) or not item["uuid"]
                or item.get("kind") not in TARGET_KINDS or item.get("decision") not in DECISION_KINDS
                or not isinstance(item.get("actor"), str) or not isinstance(item.get("at"), str)
                or not isinstance(item.get("before", {}), dict) or not isinstance(item.get("after", {}), dict)
                or not all(item.get(k) is None or isinstance(item.get(k), dict) for k in ("target", "other"))):
            raise ValueError("연결 근거 판정의 항목 형식이 맞지 않습니다.")
        if item["uuid"] in seen:
            raise ValueError("연결 근거 판정에 중복된 식별자가 있습니다.")
        seen.add(item["uuid"])
    return block


# ── 가져오기 ────────────────────────────────────────────────────────

def import_decisions(db, block: dict) -> dict:
    """판정 블록을 받아 보존하고, 대상을 정확히 찾은 것만 적용한다. 호출자 트랜잭션 안에서 쓴다."""
    validate(block)
    source = str(block.get("project_uuid") or "")
    base = db.con.execute("SELECT coalesce(max(seq),0) FROM kg_imported_decisions").fetchone()[0]
    for offset, item in enumerate(block["decisions"], start=1):
        db.con.execute("INSERT OR IGNORE INTO kg_imported_decisions(source_uuid,source_project,seq,payload) "
                       "VALUES (?,?,?,?)", (item["uuid"], source, base + offset, canonical_json(item)))
    result = apply_pending(db)
    result["received"] = len(block["decisions"])
    return result


def pending_count(db) -> int:
    return db.con.execute("SELECT COUNT(*) FROM kg_imported_decisions WHERE state='pending'").fetchone()[0]


def apply_pending(db) -> dict:
    """아직 적용하지 못한 판정을 순서대로 다시 시도한다. 몇 번을 불러도 결과가 같다."""
    result = {"applied": 0, "kept_local": 0, "pending": 0}
    rows = db.con.execute("SELECT * FROM kg_imported_decisions WHERE state='pending' ORDER BY seq").fetchall()
    if not rows:
        return result
    index = _Index(db)
    store = KnowledgeStore(db)
    for row in rows:
        item = json.loads(row["payload"])
        outcome = _apply_one(db, store, index, row, item)
        result[outcome] += 1
    return result


def _set(db, row, state: str, note: str = "", local: str | None = None) -> None:
    db.con.execute("UPDATE kg_imported_decisions SET state=?,note=?,local_decision=? WHERE source_uuid=?",
                   (state, note, local, row["source_uuid"]))


def _local_newer(db, kind: str, key: str, at: str) -> bool:
    """받는 쪽에서 사용자가 직접 남긴 더 나중의 판정이 있는가. 가져온 판정은 세지 않는다."""
    rows = db.con.execute(
        "SELECT d.* FROM kg_decisions d WHERE d.target_kind=? AND d.target_key=? "
        "AND d.uuid NOT IN (SELECT local_decision FROM kg_imported_decisions WHERE local_decision IS NOT NULL) "
        "ORDER BY d.rowid", (kind, key)).fetchall()
    last = latest_decision(rows)
    return last is not None and last["created_at"] > at


def _apply_one(db, store, index: _Index, row, item: dict) -> str:
    decision, kind = item["decision"], item["kind"]
    actor = f"가져옴:{item['actor']}"
    if kind not in ("claim", "identity"):
        _set(db, row, "pending", "이 종류의 판정은 아직 대상 연결을 지원하지 않아 보존만 합니다.")
        return "pending"
    if item.get("target") is None:
        _set(db, row, "pending", "보낸 쪽에서 대상이 이미 사라져 설명자가 없습니다.")
        return "pending"

    if decision == "undo":
        original = db.con.execute("SELECT * FROM kg_imported_decisions WHERE source_uuid=?",
                                  (item.get("supersedes"),)).fetchone()
        if original is None or original["state"] != "applied":
            _set(db, row, "pending" if original is None or original["state"] == "pending" else "kept_local",
                 "되돌릴 판정이 아직 적용되지 않았습니다.")
            return "pending" if original is None or original["state"] == "pending" else "kept_local"
        target = db.con.execute("SELECT * FROM kg_decisions WHERE uuid=?", (original["local_decision"],)).fetchone()
        uid = store.record_decision(target["target_kind"], target["target_key"], "undo", actor,
                                    supersedes_decision=target["uuid"])
        if target["decision"] == "reject":
            db.con.execute("UPDATE kg_claims SET lifecycle='active' WHERE claim_key=? AND lifecycle='retracted'",
                           (target["target_key"],))
        _set(db, row, "applied", "", uid)
        return "applied"

    if kind == "claim":
        found = index.claims(item["target"])
        key, after = (found[0] if len(found) == 1 else None), {}
    else:
        first = index.entities(item["target"])
        second = index.entities(item["other"]) if item.get("other") else []
        found = first if len(first) != 1 else second
        key = first[0] if len(first) == 1 and len(second) == 1 else None
        after = {"entity_id": second[0]} if key is not None else {}
    if key is None:
        _set(db, row, "pending", "받는 쪽에서 같은 대상을 찾지 못했습니다." if not found
             else "같은 설명의 대상이 여럿이라 고르지 않았습니다.")
        return "pending"
    if _local_newer(db, "claim" if kind == "claim" else "identity", key, item["at"]):
        _set(db, row, "kept_local", "받는 쪽에 더 나중의 사용자 판정이 있어 그대로 두었습니다.")
        return "kept_local"
    try:
        uid = store.record_decision(kind, key, decision, actor, before=item.get("before") or {},
                                    after={**(item.get("after") or {}), **after})
    except ValueError as exc:       # 유형 불일치·순환 같은 동일성 규칙 위반
        _set(db, row, "pending", str(exc))
        return "pending"
    _set(db, row, "applied", "", uid)
    return "applied"


def imported_state(db) -> Iterable:
    return db.con.execute("SELECT * FROM kg_imported_decisions ORDER BY seq").fetchall()
