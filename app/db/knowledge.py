"""그래프 저장·설치 경계. 원본 업무/문서/수행 표를 수정하지 않는다."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

from ..core.knowledge.ontology import canonical_json, claim_key, fingerprint, load, normalize_label, normalize_literal
from ..core.knowledge.validation import validate_claim

SCHEMA_PATH = Path(__file__).with_name("knowledge_schema.sql")


def install(con: sqlite3.Connection) -> None:
    """호출자 트랜잭션 안에서 실행한다. executescript의 암묵 COMMIT 금지."""
    statement = ""
    for line in SCHEMA_PATH.read_text(encoding="utf-8").splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            con.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("완결되지 않은 그래프 DDL입니다.")
    ontology = load()
    stored = con.execute("SELECT value FROM meta WHERE key='kg_ontology_fingerprint'").fetchone()
    if stored and stored[0] != fingerprint(ontology):
        raise ValueError("온톨로지 변경에는 명시적 스키마 이행이 필요합니다.")
    for kind in ontology["entities"]:
        con.execute("INSERT OR IGNORE INTO kg_entity_types(name) VALUES (?)", (kind,))
    for name, rule in ontology["predicates"].items():
        con.execute("INSERT OR IGNORE INTO kg_predicates(name,definition) VALUES (?,?)",
                    (name, canonical_json(rule)))
    for key, value in (("project_uuid", str(uuid4())), ("kg_revision", "0"),
                       ("kg_ontology_version", ontology["version"]),
                       ("kg_ontology_fingerprint", fingerprint(ontology))):
        con.execute("INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)", (key, value))
    # 마이그레이션은 파생 그래프를 만들지 않는다. 후속 작업 대상으로만 등록한다.
    if not con.execute("SELECT 1 FROM meta WHERE key='kg_bootstrap_registered'").fetchone():
        con.execute("INSERT OR IGNORE INTO kg_jobs(partition_key,reason) "
                    "SELECT 'document:'||id,'bootstrap' FROM documents")
        con.execute("INSERT OR IGNORE INTO kg_jobs(partition_key,reason) "
                    "SELECT 'task:'||id,'bootstrap' FROM tasks")
        con.execute("INSERT INTO meta(key,value) VALUES ('kg_bootstrap_registered','1')")
    _install_dirty_triggers(con)


def _install_dirty_triggers(con: sqlite3.Connection) -> None:
    """여러 DB 연결과 직접 SQL 쓰기도 같은 변경 원장에 남긴다. 작업 실행은 P3."""
    sources = {"documents": "'document:'||{row}.id", "tasks": "'task:'||{row}.id"}
    for table in ("task_docs", "task_items", "work_steps", "task_steps", "work_cases", "action_runs"):
        sources[table] = "coalesce('task:'||{row}.task_id,'project')"
    for table in ("document_sections", "document_index", "chunks", "doc_roles", "doc_years", "evidence"):
        sources[table] = "coalesce('document:'||{row}.doc_id,'project')"
    for table in ("artifacts", "checklist_items"):
        sources[table] = "coalesce('task:'||(SELECT task_id FROM work_cases WHERE id={row}.case_id),'project')"
    sources["action_steps"] = "coalesce('task:'||(SELECT task_id FROM action_runs WHERE id={row}.run_id),'project')"
    sources["step_runs"] = "coalesce('task:'||(SELECT task_id FROM work_steps WHERE id={row}.step_id),'project')"
    existing = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, expression in sources.items():
        # 일부 초기 DB에는 보충 스키마 표가 없다. repo.init이 표를 만든 뒤 다시 설치한다.
        if table not in existing:
            continue
        for operation, refs in (("INSERT", ("new",)), ("UPDATE", ("old", "new")), ("DELETE", ("old",))):
            # 하나의 UPDATE는 이전 소속과 새 소속 모두 dirty로 만든다. 중복 키는 한 번만.
            partitions = " UNION ".join(f"SELECT {expression.format(row=ref)} AS partition_key" for ref in refs)
            sql = (f"CREATE TRIGGER IF NOT EXISTS kg_dirty_{table}_{operation.lower()} AFTER {operation} ON {table} BEGIN "
                   f"INSERT INTO kg_jobs(partition_key,reason) SELECT partition_key,'{table}:{operation.lower()}' "
                   f"FROM ({partitions}) WHERE true ON CONFLICT(partition_key,job_kind) DO UPDATE SET "
                   "wanted_revision=kg_jobs.wanted_revision+1,"
                   "state=CASE WHEN kg_jobs.state='running' THEN 'running' ELSE 'dirty' END,"
                   "reason=excluded.reason; END")
            con.execute(sql)
    con.execute("""CREATE TRIGGER IF NOT EXISTS kg_dirty_meta AFTER UPDATE OF value ON meta
        WHEN new.key IN ('content_revision','index_revision') AND new.value!=old.value BEGIN
        INSERT INTO kg_jobs(partition_key,reason) VALUES ('project','revision')
        ON CONFLICT(partition_key,job_kind) DO UPDATE SET wanted_revision=kg_jobs.wanted_revision+1,
        state=CASE WHEN kg_jobs.state='running' THEN 'running' ELSE 'dirty' END,reason=excluded.reason;
        END""")


class KnowledgeStore:
    """P1 내부 API. 사람 판정 UI와 검색 활성화는 인계 왕복 구현 뒤에 제공한다."""

    def __init__(self, db):
        self.db = db

    def new_id(self) -> str:
        project = self.db.get_meta("project_uuid")
        if not project:
            raise RuntimeError("그래프 스키마를 먼저 초기화해야 합니다.")
        return f"{project}:{uuid4()}"

    def bind(self, source_table: str, source_key: str, entity_type: str,
             label: str, scope_key: str = "") -> str:
        with self.db.transaction():
            row = self.db.con.execute(
                "SELECT e.uuid,e.type FROM kg_bindings b JOIN kg_entities e ON e.uuid=b.entity_id "
                "WHERE source_table=? AND source_key=?", (source_table, str(source_key))).fetchone()
            if row:
                if row["type"] != entity_type:
                    raise ValueError("기존 binding의 유형은 바꿀 수 없습니다.")
                self.db.con.execute("UPDATE kg_entities SET label=? WHERE uuid=?", (label, row["uuid"]))
                return row["uuid"]
            uid = self.new_id()
            self.db.con.execute("INSERT INTO kg_entities(uuid,type,label,scope_key) VALUES (?,?,?,?)",
                                (uid, entity_type, label, scope_key))
            self.db.con.execute("INSERT INTO kg_bindings(source_table,source_key,entity_id) VALUES (?,?,?)",
                                (source_table, str(source_key), uid))
            return uid

    def put_claim(self, subject_id: str, predicate: str, *, object_id: str | None = None,
                  value=None, value_type: str | None = None, scope_key: str = "",
                  valid_from: str | None = None, valid_to: str | None = None,
                  conditions: dict | None = None, origin: str = "projection",
                  basis_state: str = "stated", applicability: str = "unknown",
                  human_state: str = "none", generation_id: int | None = None) -> str:
        subject = self.db.con.execute("SELECT type FROM kg_entities WHERE uuid=?", (subject_id,)).fetchone()
        obj = self.db.con.execute("SELECT type FROM kg_entities WHERE uuid=?", (object_id,)).fetchone() if object_id else None
        if subject is None or (object_id is not None and obj is None):
            raise ValueError("주장 끝점이 존재하지 않습니다.")
        if object_id == subject_id:
            raise ValueError("자기 관계를 저장할 수 없습니다.")
        if conditions is not None and not isinstance(conditions, dict):
            raise ValueError("조건은 객체여야 합니다.")
        validate_claim(subject[0], predicate, object_type=obj[0] if obj else None,
                       value=value, value_type=value_type, valid_from=valid_from, valid_to=valid_to)
        value = normalize_literal(value, value_type)
        key = claim_key(subject_id, predicate, object_id=object_id, value=value,
                        value_type=value_type, scope_key=scope_key, valid_from=valid_from,
                        valid_to=valid_to, conditions=conditions)
        with self.db.transaction():
            found = self.db.con.execute("SELECT uuid FROM kg_claims WHERE claim_key=?", (key,)).fetchone()
            if found:
                # 같은 논리 주장의 자동 재발견이 사람 상태를 덮어쓰지 않는다.
                return found[0]
            uid = self.new_id()
            self.db.con.execute(
                "INSERT INTO kg_claims(uuid,claim_key,subject_id,predicate,object_id,value_json,value_type,"
                "scope_key,valid_from,valid_to,conditions,origin,basis_state,applicability,human_state,generation_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, key, subject_id, predicate, object_id, canonical_json(value) if value is not None else None,
                 value_type, scope_key, valid_from, valid_to, canonical_json(conditions or {}), origin,
                 basis_state, applicability, human_state, generation_id))
            return uid

    def put_evidence(self, kind: str, *, doc_uuid: str | None = None,
                     doc_id: int | None = None, doc_hash: str | None = None,
                     parse_fingerprint: str | None = None, locator: str = "",
                     exact_quote: str = "", offsets: dict | None = None,
                     chunk_id: int | None = None, source_record_snapshot: dict | None = None) -> str:
        # 정수 doc/chunk ID는 현재 참조일 뿐이다. 재색인해도 anchor는 유지한다.
        snapshot = {"kind": kind, "doc_uuid": doc_uuid, "doc_hash": doc_hash,
                    "parse_fingerprint": parse_fingerprint, "locator": locator,
                    "exact_quote": exact_quote, "offsets": offsets or {},
                    "source_record_snapshot": source_record_snapshot or {}}
        key = fingerprint(snapshot)
        with self.db.transaction():
            found = self.db.con.execute("SELECT uuid FROM kg_evidence WHERE anchor_key=?", (key,)).fetchone()
            if found:
                self.db.con.execute("UPDATE kg_evidence SET doc_id=?,chunk_id=? WHERE uuid=?",
                                    (doc_id, chunk_id, found[0]))
                return found[0]
            uid = self.new_id()
            self.db.con.execute(
                "INSERT INTO kg_evidence(uuid,anchor_key,kind,doc_uuid,doc_id,doc_hash,parse_fingerprint,"
                "locator,exact_quote,offsets,chunk_id,source_record_snapshot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, key, kind, doc_uuid, doc_id, doc_hash, parse_fingerprint, locator, exact_quote,
                 canonical_json(offsets or {}), chunk_id, canonical_json(source_record_snapshot or {})))
            return uid

    def add_alias(self, entity_id: str, label: str, *, scope_key: str = "",
                  origin: str = "projection", source_key: str,
                  evidence_id: str | None = None, decision_id: str | None = None) -> None:
        self.db.con.execute(
            "INSERT INTO kg_aliases(entity_id,label,normalized_label,scope_key,origin,source_key,evidence_id,decision_id) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(entity_id,normalized_label,scope_key,origin,source_key) DO NOTHING",
            (entity_id, label, normalize_label(label), scope_key, origin,
                                       source_key, evidence_id, decision_id))

    def record_decision(self, target_kind: str, target_key: str, decision: str, actor: str,
                        *, before=None, after=None, supersedes_decision: str | None = None) -> str:
        uid = self.new_id()
        self.db.con.execute(
            "INSERT INTO kg_decisions(uuid,target_kind,target_key,decision,actor,before_json,after_json,supersedes_decision) "
            "VALUES (?,?,?,?,?,?,?,?)", (uid, target_kind, target_key, decision, actor,
                                        canonical_json(before or {}), canonical_json(after or {}), supersedes_decision))
        return uid

    def link_claims(self, from_claim: str, kind: str, to_claim: str, support_ref: str) -> None:
        if kind == "conflicts":
            from_claim, to_claim = sorted((from_claim, to_claim))
        self.db.con.execute("INSERT INTO kg_claim_links VALUES (?,?,?,?) ON CONFLICT(from_claim,kind,to_claim) DO NOTHING",
                            (from_claim, kind, to_claim, support_ref))

    def claim_links(self, claim_id: str) -> list:
        return self.db.con.execute("SELECT * FROM kg_claim_links WHERE from_claim=? OR to_claim=?",
                                   (claim_id, claim_id)).fetchall()
