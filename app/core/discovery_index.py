"""Reuse complete parent embeddings for discovery, with inspectable contributions.

Repeated identical boilerplate gets inverse document-frequency weight. This is
candidate generation, not a calibrated business-identity classifier. Incomplete
indexes retain the legacy prefix vector and explicitly identify that fallback.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from itertools import groupby

import numpy as np

from ..search.vector import DTYPE, VectorStore, normalize, unpack

VERSION = "full-chunks-idf-v1"


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load(db, model: str) -> tuple[VectorStore, dict[int, list[dict]]]:
    """Stream vectors one document at a time; never build a chunk-by-chunk matrix."""
    frequency = Counter()
    for row in db.con.execute(
        "SELECT c.text,COUNT(DISTINCT c.doc_id) n FROM chunks c "
        "JOIN documents d ON d.id=c.doc_id JOIN sources s ON s.id=d.source_id "
        "WHERE d.missing_since IS NULL AND d.parse_status IN ('ok','partial') "
        "AND s.kind!='upload' GROUP BY c.text"):
        frequency[_key(row["text"])] += row["n"]
    records: dict[int, tuple[np.ndarray, list[dict]]] = {}
    for row in db.con.execute(
        "SELECT d.id,d.hash,e.dim,e.vector FROM documents d JOIN sources s ON s.id=d.source_id "
        "JOIN doc_embeddings e ON e.doc_id=d.id WHERE e.model=? "
        "AND d.missing_since IS NULL AND d.parse_status IN ('ok','partial') AND s.kind!='upload' "
        "ORDER BY d.id", (model,)):
        vector = _vector(row)
        if vector is not None:
            records[row["id"]] = (vector, [{"chunk_id": None, "locator": "",
                "doc_hash": row["hash"], "basis": "document_prefix", "score": 1.0}])
    cursor = db.con.execute(
        "SELECT c.id chunk_id,c.doc_id,c.text,c.locator,d.hash,e.dim,e.vector FROM chunks c "
        "JOIN documents d ON d.id=c.doc_id JOIN sources s ON s.id=d.source_id "
        "LEFT JOIN embeddings e ON e.chunk_id=c.id AND e.model=? "
        "WHERE d.missing_since IS NULL AND d.parse_status IN ('ok','partial') "
        "AND s.kind!='upload' ORDER BY c.doc_id,c.ordinal,c.id", (model,))
    for doc_id, group in groupby(cursor, lambda r: r["doc_id"]):
        rows = list(group)
        vectors = [_vector(r) for r in rows]
        if any(v is None for v in vectors) or len({v.size for v in vectors if v is not None}) != 1:
            continue
        weights = [1.0 / max(1, frequency[_key(r["text"])]) for r in rows]
        centroid = np.average(np.stack(vectors), axis=0, weights=weights).astype(DTYPE)
        if not np.isfinite(centroid).all() or not np.linalg.norm(centroid):
            continue
        total_weight = sum(weights)
        records[doc_id] = (centroid, [
            {"chunk_id": r["chunk_id"], "locator": r["locator"], "doc_hash": r["hash"],
             "basis": "full_chunks", "score": w / total_weight}
            for r, w in zip(rows, weights)])
    if not records:
        return VectorStore(np.empty(0, dtype=np.int64), np.empty((0, 0), dtype=DTYPE)), {}
    # Exclude incompatible vectors rather than silently mixing model dimensions.
    dim = Counter(v.size for v, _ in records.values()).most_common(1)[0][0]
    ids = sorted(i for i, (v, _) in records.items() if v.size == dim)
    matrix = normalize(np.stack([records[i][0] for i in ids]))
    return VectorStore(np.asarray(ids, dtype=np.int64), matrix), {i: records[i][1] for i in ids}


def _vector(row):
    if row["vector"] is None or not row["dim"] or len(row["vector"]) != row["dim"] * 4:
        return None
    value = unpack(row["vector"])
    norm = np.linalg.norm(value)
    return value / norm if norm and np.isfinite(value).all() else None


def naming_evidence(db, doc_ids: list[int], contributions: dict, budget: int = 3600):
    """Bounded, evenly spread excerpts. Only actually supplied chunks count as read."""
    selected = []
    # At most six documents per call. The full group remains a candidate requiring review.
    if len(doc_ids) > 6:
        doc_ids = [doc_ids[i * (len(doc_ids) - 1) // 5] for i in range(6)]
    per_document = max(1, budget // max(1, len(doc_ids)))
    for doc_id in doc_ids:
        items = [r for r in contributions.get(doc_id, []) if r["chunk_id"] is not None]
        if not items:
            continue
        positions = sorted({0, len(items) // 2, len(items) - 1})
        allowance = max(1, per_document // len(positions) - 80)
        for index in positions:
            item = items[index]
            row = db.con.execute("SELECT text FROM chunks WHERE id=?", (item["chunk_id"],)).fetchone()
            if row:
                candidate = {"doc_id": doc_id, "chunk_id": item["chunk_id"],
                             "locator": item["locator"][:80], "text": row["text"][:allowance]}
                if len(json.dumps([*selected, candidate], ensure_ascii=False)) <= budget:
                    selected.append(candidate)
    return selected
