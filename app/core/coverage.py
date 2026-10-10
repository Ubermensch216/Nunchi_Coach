"""Read-only coverage views. Counts describe processing, never understanding.

File observations and directory boundaries have different denominators. An
excluded/unreadable directory says nothing about the number of files inside it.
Parser 'ok' is deliberately not interpreted as complete extraction.
"""
from __future__ import annotations

import json
from collections import Counter

from ..ingest.parsers.base import MAX_BYTES, MAX_CHARS, MAX_CELLS_PER_SHEET


def issue(db, doc_id: int, stage: str, reason: str) -> None:
    # Store reason codes only, never model replies or document body.
    db.con.execute(
        "INSERT INTO processing_issues(doc_id,stage,reason) VALUES(?,?,?) "
        "ON CONFLICT(doc_id,stage) DO UPDATE SET reason=excluded.reason, "
        "attempts=processing_issues.attempts+1,updated_at=datetime('now')",
        (doc_id, stage, reason))


def clear_issue(db, doc_id: int, stage: str) -> None:
    db.con.execute("DELETE FROM processing_issues WHERE doc_id=? AND stage=?", (doc_id, stage))


def documents(db, model: str | None = None, source_id: int | None = None) -> list[dict]:
    """One row per document; embedding counts are scoped to the selected model."""
    model = model or db.get_meta("embed_model") or "bge-m3"
    rows = db.con.execute(
        """WITH sections AS (
               SELECT doc_id, COUNT(*) n FROM document_sections WHERE trim(text)!='' GROUP BY doc_id
           ), pieces AS (
               SELECT c.doc_id,COUNT(*) n,
                      SUM(CASE WHEN e.model=? THEN 1 ELSE 0 END) embedded
               FROM chunks c LEFT JOIN embeddings e ON e.chunk_id=c.id GROUP BY c.doc_id
           ), assignments AS (
               SELECT td.doc_id,COUNT(*) n FROM task_docs td JOIN tasks t ON t.id=td.task_id
               WHERE t.not_a_task=0 AND t.merged_into IS NULL GROUP BY td.doc_id
           )
           SELECT d.id,d.source_id,d.path,d.filename,d.ext,d.hash,d.parse_status,
                  d.parse_error,d.parse_note,d.parser_version,d.char_count,d.missing_since,
                  s.kind source_kind,COALESCE(ds.n,0) sections,COALESCE(c.n,0) chunks,
                  COALESCE(c.embedded,0) embedded_chunks,
                  CASE WHEN e.model=? THEN 1 ELSE 0 END document_embedding,
                  COALESCE(a.n,0) assignments
           FROM documents d JOIN sources s ON s.id=d.source_id
           LEFT JOIN sections ds ON ds.doc_id=d.id LEFT JOIN pieces c ON c.doc_id=d.id
           LEFT JOIN doc_embeddings e ON e.doc_id=d.id
           LEFT JOIN assignments a ON a.doc_id=d.id
           WHERE (? IS NULL OR d.source_id=?) ORDER BY d.id""",
        (model, model, source_id, source_id)).fetchall()
    issues: dict[int, list[dict]] = {}
    for row in db.con.execute("SELECT doc_id,stage,reason,attempts FROM processing_issues"):
        issues.setdefault(row["doc_id"], []).append(dict(row))
    measured = {}
    if "extraction_coverage" in {r[1] for r in db.con.execute("PRAGMA table_info(documents)")}:
        for r in db.con.execute("SELECT id,extraction_coverage FROM documents"):
            try:
                value = json.loads(r[1] or "{}")
                measured[r[0]] = value if isinstance(value, dict) else {}
            except (ValueError, TypeError):
                measured[r[0]] = {}
    result = []
    for raw in rows:
        row = dict(raw)
        row["extraction_coverage"] = measured.get(row["id"], {})
        row["hash_success"] = bool(row.pop("hash"))
        row["classifiable"] = (
            row["source_kind"] != "upload" and row["missing_since"] is None
            and row["parse_status"] in ("ok", "partial") and row["sections"] > 0)
        row["extraction_completeness"] = (
            "known_partial" if row["parse_status"] == "partial" else "unverified")
        row["chunk_index_complete"] = bool(
            row["chunks"] and row["chunks"] == row["embedded_chunks"])
        row["issues"] = issues.get(row["id"], [])
        result.append(row)
    return result


def snapshot(db, model: str | None = None, *, include_documents: bool = False) -> dict:
    files = documents(db, model)
    current = [r for r in files if r["missing_since"] is None]
    eligible = [r for r in current if r["classifiable"]]
    assigned = sum(r["assignments"] > 0 for r in eligible)
    unassigned = sum(r["assignments"] == 0 for r in eligible)
    scans = []
    for source in db.work_sources():
        run = db.con.execute("SELECT * FROM scan_runs WHERE source_id=? ORDER BY id DESC LIMIT 1",
                             (source["id"],)).fetchone()
        view = {"source_id": source["id"], "state": "not_recorded", "run_id": None}
        if run:
            counts = {(r["kind"], r["state"]): r["n"] for r in db.con.execute(
                "SELECT kind,state,COUNT(*) n FROM scan_entries WHERE run_id=? GROUP BY kind,state",
                (run["id"],))}
            view.update(state=run["state"], run_id=run["id"],
                        started_at=run["started_at"], finished_at=run["finished_at"])
            for state in ("included", "unsupported", "excluded", "error"):
                view[state + "_files"] = counts.get(("file", state), 0)
            view["observed_files"] = sum(view[s + "_files"] for s in (
                "included", "unsupported", "excluded", "error"))
            view["excluded_directories"] = counts.get(("directory", "excluded"), 0)
            view["unreadable_directories"] = counts.get(("directory", "error"), 0)
            view["uninspected_directory_contents"] = bool(
                view["excluded_directories"] or view["unreadable_directories"])
        scans.append(view)
    by_format = {}
    for row in current:
        counts = by_format.setdefault(row["ext"], Counter())
        counts["total"] += 1
        counts[row["parse_status"]] += 1
    result = {
        "schema_version": db.get_meta("schema_version"),
        "model": model or db.get_meta("embed_model") or "bge-m3",
        "registered_files": len(files), "present_files": len(current),
        "missing_files": len(files) - len(current),
        "parse_states": dict(Counter(r["parse_status"] for r in current)),
        "hash_success": sum(r["hash_success"] for r in current),
        "document_embeddings": sum(r["document_embedding"] for r in current),
        "chunks": sum(r["chunks"] for r in current),
        "embedded_chunks": sum(r["embedded_chunks"] for r in current),
        "fully_indexed_documents": sum(r["chunk_index_complete"] for r in current),
        "classifiable_documents": len(eligible), "assigned_documents": assigned,
        "unassigned_documents": unassigned,
        "assignment_invariant": assigned + unassigned == len(eligible),
        "extraction_scope_measured_documents": sum(bool(r["extraction_coverage"]) for r in current),
        "extraction_known_omission_documents": sum(bool(r["extraction_coverage"].get("omissions")) for r in current),
        "extraction_complete_documents": None,
        "extraction_completeness_note": "파서 성공은 완전 추출을 보장하지 않습니다.",
        "limits": {"max_bytes": MAX_BYTES, "max_chars": MAX_CHARS,
                   "max_cells_per_sheet": MAX_CELLS_PER_SHEET},
        "scans": scans, "by_format": by_format,
        "stages": [dict(r) for r in db.con.execute("SELECT * FROM pipeline_stage_results")],
        "holds": [dict(r) for r in db.con.execute("SELECT * FROM discovery_holds")],
        "classification_evidence_documents": db.con.execute(
            "SELECT COUNT(DISTINCT e.doc_id) FROM discovery_evidence e JOIN documents d ON d.id=e.doc_id "
            "JOIN tasks t ON t.id=e.task_id JOIN task_docs td ON td.task_id=e.task_id AND td.doc_id=e.doc_id "
            "WHERE t.not_a_task=0 AND t.merged_into IS NULL AND e.doc_hash=d.hash "
            "AND d.parse_status IN ('ok','partial') AND d.missing_since IS NULL AND e.model=? "
            "AND (e.basis='document_prefix' OR e.chunk_id IS NOT NULL)",
            (model or db.get_meta("embed_model") or "bge-m3",)).fetchone()[0],
        "model_reviewed_excerpt_documents": db.con.execute(
            "SELECT COUNT(DISTINCT e.doc_id) FROM discovery_evidence e JOIN documents d ON d.id=e.doc_id "
            "JOIN tasks t ON t.id=e.task_id JOIN task_docs td ON td.task_id=e.task_id AND td.doc_id=e.doc_id "
            "WHERE t.not_a_task=0 AND t.merged_into IS NULL AND e.reviewed_by_model=1 "
            "AND d.parse_status IN ('ok','partial') AND e.doc_hash=d.hash AND d.missing_since IS NULL "
            "AND e.chunk_id IS NOT NULL AND e.model=?",
            (model or db.get_meta("embed_model") or "bge-m3",)).fetchone()[0],
    }
    if include_documents:
        result["documents"] = files
        result["classification_evidence"] = [dict(r) for r in db.con.execute("SELECT * FROM discovery_evidence")]
    return result


def save_stage(db, report, state: str) -> None:
    db.con.execute(
        "INSERT INTO pipeline_stage_results(stage,done,total,state,note,errors) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(stage) DO UPDATE SET done=excluded.done,total=excluded.total,"
        "state=excluded.state,note=excluded.note,errors=excluded.errors,updated_at=datetime('now')",
        (report.stage, report.done, report.total, state, report.note,
         json.dumps(report.errors, ensure_ascii=False)))
