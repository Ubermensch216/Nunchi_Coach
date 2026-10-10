"""Reuse complete parent embeddings for discovery, with inspectable contributions.

Repeated identical boilerplate gets inverse document-frequency weight. This is
candidate generation, not a calibrated business-identity classifier. Incomplete
indexes retain the legacy prefix vector and explicitly identify that fallback.

조각의 비중은 '글의 양(글자 수의 제곱근) ÷ 흔함'이다. 흔함은 그 조각과 거의 같은 조각을 가진 다른 문서의
수다. 글자가 똑같은 조각만 흔하다고 보면 표의 행이 빠져나간다 — "머리글: 항목 | 계획 |
실적 / 1열: … | 2열: 373"은 숫자만 다를 뿐 수백 문서에 있고, 표를 행마다 조각으로 내는
형식(HWPX·ODT)의 문서는 조각 대부분이 이런 행이다. 실측(합성 1,092건)에서 조각을 똑같이
세면 HWPX·ODT 문서 361건이 업무와 무관하게 한 덩어리로 묶였다(쌍 정밀도 0.16 → 0.97).

글의 양을 글자 수 그대로 쓰지 않는다. 긴 공통 서식 뒤에 짧게 적힌 업무 내용이 서식에
묻힌다(tests/test_discovery_coverage.py의 '뒤쪽 본문' 반례).
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from itertools import groupby

import numpy as np

from ..search.vector import DTYPE, VectorStore, normalize, unpack

VERSION = "full-chunks-softidf-v2"
# 이 유사도 이상이면 같은 틀의 조각으로 본다.
NEAR_DUPLICATE = 0.9
# 흔함을 재는 기준 조각의 수. 조각끼리 전부 견주지 않고, 이 수를 넘으면 문서를 고르게
# 걸러 그 문서들의 조각과만 견준다. 조각이 아니라 문서를 거른다 — 조각을 거르면 한 문서에
# 같은 틀의 행이 여럿일 때 그 문서가 거의 늘 걸려, 배율을 곱한 값이 부풀려진다.
REFERENCE_CHUNKS = 8192


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
    reference, reference_docs, scale = _reference(db, model)
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
        stacked = np.stack(vectors)
        common = _commonness(stacked, doc_id, reference, reference_docs, scale)
        weights = [max(1, len(r["text"])) ** 0.5 / max(1, frequency[_key(r["text"])]) / c
                   for r, c in zip(rows, common)]
        centroid = np.average(stacked, axis=0, weights=weights).astype(DTYPE)
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


def _reference(db, model: str):
    """흔함을 잴 기준 조각. (행렬, 조각마다의 문서 ID, 전체 대비 배율)을 돌려준다."""
    scope = ("FROM chunks c JOIN embeddings e ON e.chunk_id=c.id AND e.model=? "
             "JOIN documents d ON d.id=c.doc_id JOIN sources s ON s.id=d.source_id "
             "WHERE d.missing_since IS NULL AND d.parse_status IN ('ok','partial') AND s.kind!='upload'")
    total = db.con.execute("SELECT COUNT(*) " + scope, (model,)).fetchone()[0]
    stride = max(1, -(-total // REFERENCE_CHUNKS))
    vectors, owners = [], []
    for row in db.con.execute(
            "SELECT c.doc_id,e.dim,e.vector " + scope + " AND c.doc_id % ? = 0 ORDER BY c.id", (model, stride)):
        vector = _vector(row)
        if vector is not None and (not vectors or vector.size == vectors[0].size):
            vectors.append(vector)
            owners.append(row["doc_id"])
    if not vectors:
        return np.empty((0, 0), dtype=DTYPE), np.empty(0, dtype=np.int64), 1.0
    return np.stack(vectors).astype(DTYPE), np.asarray(owners, dtype=np.int64), float(stride)


def _commonness(vectors: np.ndarray, doc_id: int, reference: np.ndarray,
                reference_docs: np.ndarray, scale: float) -> np.ndarray:
    """조각마다, 거의 같은 조각을 가진 문서의 수(자기 문서 포함, 1 이상)."""
    if not reference.size or reference.shape[1] != vectors.shape[1]:
        return np.ones(len(vectors))
    near = (vectors.astype(DTYPE) @ reference.T) >= NEAR_DUPLICATE
    near[:, reference_docs == doc_id] = False
    return np.asarray([1.0 + scale * len(set(reference_docs[row].tolist())) for row in near])


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
