"""모델 없는 정본 투영. 문서·업무 파티션 하나를 트랜잭션으로 게시한다."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from uuid import uuid4

from ...db.knowledge import KnowledgeStore
from .evidence import match_quote, parse_fingerprint
from .ontology import canonical_json, fingerprint, normalize_label

PROJECTION_VERSION = "1"


@dataclass
class Result:
    partition: str
    generation: int = 0
    claims: int = 0
    reused: bool = False
    diagnostics: list[str] = field(default_factory=list)


def _document(db, doc_id):
    row = db.document(doc_id)
    if row is None:
        return None
    fields = ("id", "source_id", "path", "filename", "hash", "parser", "parser_version", "parse_status",
              "extraction_coverage", "eff_year", "missing_since")
    value = {key: row[key] for key in fields}
    value["parse_fingerprint"] = parse_fingerprint(db, doc_id)
    value["chunk_fingerprint"] = fingerprint([tuple(r) for r in db.con.execute(
        "SELECT id,ordinal,locator FROM chunks WHERE doc_id=? ORDER BY id", (doc_id,))])
    return value


def _task_input(db, task_id):
    task = db.task(task_id)
    if task is None:
        raise ValueError("업무를 찾을 수 없습니다.")
    data = {"task": dict(task)}
    for table in ("task_docs", "work_cases", "work_steps", "task_steps", "task_items"):
        data[table] = [dict(r) for r in db.con.execute(f"SELECT * FROM {table} WHERE task_id=? ORDER BY rowid", (task_id,))]
    data["step_runs"] = [dict(r) for r in db.con.execute("SELECT s.* FROM step_runs s JOIN work_steps w ON w.id=s.step_id WHERE w.task_id=? ORDER BY s.step_id", (task_id,))]
    data["action_runs"] = [dict(r) for r in db.con.execute("SELECT r.* FROM action_runs r LEFT JOIN work_cases c ON c.id=r.case_id WHERE r.task_id=? OR c.task_id=? ORDER BY r.id", (task_id, task_id))]
    data["action_steps"] = [dict(r) for r in db.con.execute("SELECT s.* FROM action_steps s JOIN action_runs r ON r.id=s.run_id LEFT JOIN work_cases c ON c.id=r.case_id WHERE r.task_id=? OR c.task_id=? ORDER BY s.id", (task_id, task_id))]
    for table in ("artifacts", "checklist_items"):
        data[table] = [dict(r) for r in db.con.execute(f"SELECT a.* FROM {table} a LEFT JOIN work_cases c ON c.id=a.case_id LEFT JOIN action_runs r ON r.id=a.run_id WHERE c.task_id=? OR r.task_id=? ORDER BY a.id", (task_id, task_id))]
    data["evidence"] = [dict(r) for r in db.con.execute(
        "SELECT owner_kind,owner_id,doc_id,doc_hash,locator,quote,doc_name,doc_relpath FROM evidence "
        "WHERE (owner_kind='item' AND owner_id IN (SELECT id FROM task_items WHERE task_id=?)) "
        "OR (owner_kind='step' AND owner_id IN (SELECT id FROM work_steps WHERE task_id=?)) ORDER BY owner_kind,owner_id,doc_id,locator,quote", (task_id, task_id))]
    doc_ids = {r["doc_id"] for r in data["task_docs"] + data["task_steps"] + data["evidence"] if r["doc_id"] is not None}
    doc_ids.update(r["basis_doc_id"] for r in data["work_cases"] if r["basis_doc_id"] is not None)
    data["documents"] = [_document(db, doc_id) for doc_id in sorted(doc_ids)]
    return data


class _Projection:
    def __init__(self, db, partition, data):
        self.db, self.store, self.data = db, KnowledgeStore(db), data
        self.result = Result(partition)
        self.signature = fingerprint({"projection": PROJECTION_VERSION, "data": data,
            "index_revision": db.get_meta("index_revision"), "decisions": self.store.decision_revision()})
        previous = self.store.published(partition, self.signature)
        if previous:
            self.result.generation, self.result.claims, self.result.reused = previous["id"], previous["processed"], True
        else:
            self.result.generation = self.store.begin_generation(partition, self.signature)
        self.documents = {}

    def record(self, table, row, *, human=False, doc_id=None):
        snapshot = {"source_table": table, "source_key": str(row.get("id", row.get("step_id", ""))),
                    "record": {k: v for k, v in row.items() if k != "source_snapshot"}}
        if doc_id is None:
            doc_id = row.get("doc_id", row.get("basis_doc_id"))
        document = self.documents.get(doc_id)
        return self.store.put_evidence("human_statement" if human else "source_record",
            doc_id=doc_id, doc_uuid=document[1] if document else None,
            doc_hash=document[2]["hash"] if document else None,
            source_record_snapshot=snapshot)

    def entity(self, table, row, kind, label, scope):
        uid = self.store.bind(table, str(row["id"]), kind, label, scope)
        evidence = self.record(table, row, human=row.get("origin") == "user")
        self.store.add_alias(uid, label, scope_key=scope, source_key=f"{table}:{row['id']}", evidence_id=evidence)
        return uid

    def document(self, row):
        if row is None or not row["hash"]:
            self.result.diagnostics.append("document:missing_hash_or_record")
            return None
        doc_id = row["id"]
        key = f"kg_document_uuid:{doc_id}"
        doc_uuid = self.db.get_meta(key)
        if not doc_uuid:
            doc_uuid = f"{self.db.get_meta('project_uuid')}:{uuid4()}"
            self.db.set_meta(key, doc_uuid)
        uid = self.store.bind("document_versions", f"{doc_uuid}:{row['hash']}", "DocumentVersion", row["filename"], f"document:{doc_uuid}")
        evidence = self.store.put_evidence("source_record", doc_uuid=doc_uuid, doc_id=doc_id, doc_hash=row["hash"],
            parse_fingerprint=row["parse_fingerprint"], source_record_snapshot={k: v for k, v in row.items() if k != "chunk_fingerprint"})
        self.store.add_alias(uid, row["filename"], scope_key=f"document:{doc_uuid}", source_key=f"document:{doc_uuid}:{row['hash']}", evidence_id=evidence)
        value = (uid, doc_uuid, row)
        self.documents[doc_id] = value
        return value

    def claim(self, subject, predicate, row, *, obj=None, value=None, value_type=None,
              scope="", active=True, table="", evidence_rows=()):
        try:
            uid = self.store.put_claim(subject, predicate, object_id=obj, value=value, value_type=value_type,
                scope_key=scope, origin="projection", basis_state=row.get("basis_state", "stated"),
                applicability=row.get("applicability", "unknown"), human_state=row.get("human_state", "none"), generation_id=self.result.generation)
        except ValueError:
            self.result.diagnostics.append(f"{table}:{row.get('id','')}:invalid_claim_contract")
            return None
        except sqlite3.IntegrityError as exc:
            if "kg relation cycle" not in str(exc):
                raise
            self.result.diagnostics.append(f"{table}:{row.get('id','')}:relation_cycle")
            return None
        self.store.inherit_states(uid, basis_state=row.get("basis_state", "stated"),
            applicability=row.get("applicability", "unknown"), human_state=row.get("human_state", "none"),
            execution_state=row.get("execution_state"), generation_id=self.result.generation)
        human = row.get("origin") == "user"
        original_support = self.record(table, row, human=human)
        matched = False
        for original in evidence_rows:
            document = self.documents.get(original["doc_id"])
            if document is None:
                self.result.diagnostics.append(f"{table}:{row.get('id','')}:missing_document")
                continue
            _, doc_uuid, doc = document
            quote = match_quote(self.db, doc["id"], original["doc_hash"], original["locator"], original["quote"])
            matched = matched or quote is not None
            evidence = self.store.put_evidence("document_quote" if original["doc_hash"] and original["locator"] and original["quote"] else "source_record",
                doc_uuid=doc_uuid, doc_id=doc["id"], doc_hash=original["doc_hash"], parse_fingerprint=doc["parse_fingerprint"],
                locator=original["locator"], exact_quote=quote["exact_quote"] if quote else original["quote"],
                offsets=quote["offsets"] if quote else {}, chunk_id=quote["chunk_id"] if quote else None)
            self.store.support(uid, evidence, self.result.generation, "quote_matched" if quote else "invalid", active and quote is not None)
            if quote is None:
                self.result.diagnostics.append(f"{table}:{row.get('id','')}:quote_or_version_mismatch")
        self.store.support(uid, original_support, self.result.generation, active=active and (human or not evidence_rows or matched))
        self.result.claims += 1
        return uid

    def finish(self):
        self.store.finish_generation(self.result.generation, self.result.diagnostics)
        return self.result


def project_document(db, doc_id: int) -> Result:
    with db.transaction():
        row = _document(db, doc_id)
        if row is None:
            raise ValueError("문서를 찾을 수 없습니다.")
        projection = _Projection(db, f"document:{doc_id}", row)
        if projection.result.reused:
            return projection.result
        projection.document(row)
        return projection.finish()


def project_task(db, task_id: int) -> Result:
    with db.transaction():
        data = _task_input(db, task_id)
        p = _Projection(db, f"task:{task_id}", data)
        if p.result.reused:
            return p.result
        scope = f"task:{task_id}"
        task = p.entity("tasks", data["task"], "Task", data["task"]["name"], scope)
        if data["task"]["not_a_task"]:
            return p.finish()
        for doc in data["documents"]:
            p.document(doc)
        for row in data["task_docs"]:
            document = p.documents.get(row["doc_id"])
            if document:
                record = row | {"basis_state": "stated" if row["origin"] == "user" else "inferred"}
                p.claim(task, "HAS_DOCUMENT", record, obj=document[0], scope=scope, table="task_docs", active=document[2]["missing_since"] is None)
        cases, steps, artifacts = {}, {}, {}
        for row in data["work_cases"]:
            case_scope = f"case:{row['id']}:year:{row['period_year']}"
            cases[row["id"]] = p.entity("work_cases", row, "Case", row["label"] or str(row["period_year"]), case_scope)
            record = row | {"execution_state": row["state"], "basis_state": "stated" if row["basis_state"] in ("official", "user") else "inferred"}
            p.claim(task, "HAS_CASE", record, obj=cases[row["id"]], scope=case_scope, table="work_cases")
            if row["due_date"]:
                p.claim(cases[row["id"]], "due_date", record | {"origin": "user" if row["due_state"] == "user" else "auto",
                    "basis_state": "stated" if row["due_state"] in ("official", "user") else "inferred"},
                    value=row["due_date"], value_type="date", scope=case_scope, table="work_cases")
            document = p.documents.get(row["basis_doc_id"])
            if document:
                p.claim(document[0], "DOCUMENTS_CASE", record, obj=cases[row["id"]], scope=case_scope, table="work_cases", active=document[2]["missing_since"] is None)
                p.claim(cases[row["id"]], "BASED_ON", record, obj=document[0], scope=case_scope, table="work_cases", active=document[2]["missing_since"] is None)
        runs = {r["step_id"]: r for r in data["step_runs"]}
        for table in ("work_steps", "task_steps", "action_steps"):
            for row in data[table]:
                step_scope = scope if table == "work_steps" else f"{scope}:historical:{row['year']}" if table == "task_steps" else f"action_run:{row['run_id']}"
                steps[(table, row["id"])] = p.entity(table, row, "Step", row.get("label", row.get("step_key") or row.get("tool", "")), step_scope)
                record = row | {"execution_state": runs.get(row["id"], {}).get("state") if table == "work_steps" else row.get("state"),
                                "basis_state": row.get("basis_state", "inferred" if row.get("is_inferred") else "stated"),
                                "applicability": row.get("applicability", "past" if table == "task_steps" else "unknown")}
                if not row.get("excluded"):
                    p.claim(task, "HAS_STEP", record, obj=steps[(table, row["id"])], scope=step_scope, table=table)
        for row in data["work_steps"]:
            before = steps.get(("work_steps", row["after_step_id"]))
            if before and not row["excluded"] and not next(r for r in data["work_steps"] if r["id"] == row["after_step_id"])["excluded"]:
                p.claim(before, "PRECEDES", row, obj=steps[("work_steps", row["id"])], scope=scope, table="work_steps")
            elif row["after_step_id"] is not None and before is None:
                p.result.diagnostics.append(f"work_steps:{row['id']}:cross_task_predecessor")
        for row in data["checklist_items"]:
            step = steps.get(("work_steps", row["work_step_id"]))
            if step and row["case_id"] in cases:
                p.claim(cases[row["case_id"]], "HAS_STEP", row, obj=step, scope=f"case:{row['case_id']}", table="checklist_items")
        for row in data["action_steps"]:
            run = next(r for r in data["action_runs"] if r["id"] == row["run_id"])
            if run["case_id"] in cases:
                p.claim(cases[run["case_id"]], "HAS_STEP", row | {"execution_state": row["state"]}, obj=steps[("action_steps", row["id"])], scope=f"case:{run['case_id']}", table="action_steps")
        for row in data["artifacts"]:
            artifacts[row["id"]] = p.entity("artifacts", row, "Artifact", row["title"], f"artifact:{row['id']}")
            if row["case_id"] in cases:
                p.claim(cases[row["case_id"]], "PRODUCES", row | {"execution_state": row["state"]}, obj=artifacts[row["id"]], scope=f"case:{row['case_id']}", table="artifacts")
            if ("action_steps", row["step_id"]) in steps:
                p.claim(steps[("action_steps", row["step_id"])], "PRODUCES", row | {"execution_state": row["state"]}, obj=artifacts[row["id"]], scope=f"action_step:{row['step_id']}", table="artifacts")
        for row in data["artifacts"]:
            if row["supersedes_id"] in artifacts:
                p.claim(artifacts[row["id"]], "SUPERSEDES", row, obj=artifacts[row["supersedes_id"]], scope=scope, table="artifacts")
        for row in data["task_items"]:
            if row["excluded"] or row["basis_state"] == "missing":
                continue
            original = [e for e in data["evidence"] if e["owner_kind"] == "item" and e["owner_id"] == row["id"]]
            if row["kind"] == "deadline":
                p.claim(task, "due_date", row, value=row["content"], value_type="date", scope=scope, table="task_items", evidence_rows=original)
            elif row["kind"] == "amount":
                amount = re.fullmatch(r"\s*(-?(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)(?:\.[0-9]+)?)\s*(원|천원|만원|억원|KRW)\s*", row["content"])
                if amount:
                    value = {"amount": amount[1].replace(",", ""), "currency" if amount[2] == "KRW" else "unit": amount[2]}
                    p.claim(task, "amount", row, value=value, value_type="decimal_amount", scope=scope, table="task_items", evidence_rows=original)
                else:
                    p.result.diagnostics.append(f"task_items:{row['id']}:ambiguous_amount")
            elif row["kind"] in ("recipient", "contact", "form"):
                kind, predicate = {"recipient": ("Organization", "SUBMITTED_TO"), "contact": ("Role", "OWNED_BY"), "form": ("Form", "REQUIRES_FORM")}[row["kind"]]
                entity = p.store.bind("task_item_values", f"{row['id']}:{fingerprint(normalize_label(row['content']))}", kind, row["content"], scope)
                provenance = p.record("task_items", row, human=row["origin"] == "user")
                p.store.add_alias(entity, row["content"], scope_key=scope, source_key=f"task_item:{row['id']}", evidence_id=provenance)
                p.claim(task, predicate, row, obj=entity, scope=scope, table="task_items", evidence_rows=original)
        return p.finish()
