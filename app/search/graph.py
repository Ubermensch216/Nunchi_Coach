"""Bounded one-hop retrieval of current, scoped original evidence.

No model calls or writes. The default mode is off; shadow computes candidates
without changing retrieval. Source records alone never qualify answer context.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import sqlite3
from time import perf_counter

from ..core.knowledge.evidence import match_quote, parse_fingerprint
from ..core.knowledge.ontology import normalize_label
from .query import QueryScope

VERSION = 1
MAX_SEEDS = 8
MAX_EDGES = 100
MAX_CANDIDATES = 5
BUDGET_MS = 150
MAX_LABEL = 64
WEIGHT = 0.2


def revisions(db):
    return tuple(db.get_meta(key) for key in ("content_revision", "index_revision", "kg_revision"))


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    claim_id: str
    evidence_id: str
    predicate: str
    generation: int
    validation_version: str
    scope_fingerprint: str
    path: tuple[str, ...]
    reason: str = "current_scoped_quote"


@dataclass
class Result:
    hits: list[Hit] = field(default_factory=list)
    visited: int = 0
    elapsed_ms: float = 0
    fallback: str = ""


# Each eligible support belongs to a published generation and a current claim.
# Current claims may also have structural support, so the quote support itself
# must be checked rather than relying on the view alone.
_FROM = """
 FROM kg_current_claims c
 JOIN kg_claim_evidence s ON s.claim_id=c.uuid
 JOIN kg_generations g ON g.id=s.generation_id
 JOIN kg_evidence e ON e.uuid=s.evidence_id
 JOIN documents d ON d.id=e.doc_id
 JOIN chunks ch ON ch.id=e.chunk_id AND ch.doc_id=d.id AND ch.locator=e.locator
 WHERE s.active=1 AND g.active=1 AND g.status='published'
 AND s.support_role='supports' AND s.validation_result IN ('quote_matched','semantic_supported')
 AND e.kind='document_quote' AND e.doc_hash=d.hash
 AND d.parse_status IN ('ok','partial')
 AND c.basis_state IN ('stated','conflict')
 AND instr(ch.text,e.exact_quote)>0
"""


def question_grams(question: str) -> list[str]:
    """Substrings of the question that could equal an alias label (2..MAX_LABEL chars).

    Looking labels up by equality uses the alias index; scanning every alias with
    ``instr(question, label)`` grows with the number of documents. Labels longer
    than MAX_LABEL cannot be found this way, which only matters for a question
    that quotes a whole long filename or sentence verbatim.
    """
    grams = set()
    for start, char in enumerate(question):
        if char == " ":
            continue
        for end in range(start + 2, min(len(question), start + MAX_LABEL) + 1):
            if question[end - 1] != " ":
                grams.add(question[start:end])
    return sorted(grams)


def alias_seeds(db, question: str, predicate: str, params) -> list[str]:
    # A projected alias must be attached to the same current claim source
    # record or its scoped quote. Decision-only/global aliases are withheld
    # until they have scoped provenance; namesakes stay separate UUIDs.
    grams = json.dumps(question_grams(question), ensure_ascii=False)
    # Matching aliases come first (index lookup, a handful of rows); CROSS JOIN
    # keeps SQLite from starting at the document table and probing every alias.
    # Subject and object sides run as separate statements: joined by UNION the
    # planner abandons this order.
    seeds: set[str] = set()
    for column in ("subject_id", "object_id"):
        seeds.update(r[0] for r in db.con.execute(
            "WITH matched AS MATERIALIZED (SELECT entity_id,evidence_id FROM kg_aliases "
            "WHERE normalized_label IN (SELECT value FROM json_each(?))) "
            "SELECT DISTINCT a.entity_id" + _FROM.replace(
                "FROM kg_current_claims c",
                f"FROM matched a CROSS JOIN kg_current_claims c ON c.{column}=a.entity_id")
            + " AND " + predicate
            + " AND EXISTS (SELECT 1 FROM kg_claim_evidence provenance WHERE provenance.claim_id=c.uuid "
              "AND provenance.evidence_id=a.evidence_id AND provenance.generation_id=g.id AND provenance.active=1) "
              "ORDER BY 1 LIMIT ?", (grams, *params, MAX_SEEDS)))
    return sorted(seeds)[:MAX_SEEDS]


def search(db, question: str, scope: QueryScope | None = None, *, seed_chunks=(),
           budget_ms: float = BUDGET_MS) -> Result:
    """Return supporting quotes, never neighboring documents or global labels.

    SQL is bounded before Python materialization. A SQLite progress handler also
    interrupts expensive seed joins; a timeout discards the entire graph result.
    """
    result = Result()
    scope = scope or QueryScope()
    started = perf_counter()
    deadline = started + max(0, budget_ms) / 1000
    snapshot = revisions(db)
    scope_key = scope.fingerprint()
    predicate, params = scope.predicate()

    def expired():
        return perf_counter() >= deadline

    def check():
        if expired():
            raise TimeoutError

    try:
        check()
        db.con.set_progress_handler(lambda: int(expired()), 500)
        seeds = alias_seeds(db, normalize_label(question), predicate, params)
        check()
        if seed_chunks:
            ids = list(dict.fromkeys(seed_chunks))[:MAX_SEEDS]
            marks = ",".join("?" for _ in ids)
            for row in db.con.execute("SELECT c.subject_id,c.object_id" + _FROM + " AND " + predicate
                                      + f" AND ch.id IN ({marks}) ORDER BY c.uuid LIMIT ?",
                                      (*params, *ids, MAX_SEEDS)):
                for entity in row:
                    if entity and entity not in seeds and len(seeds) < MAX_SEEDS:
                        seeds.append(entity)
        if not seeds:
            result.fallback = "no_scoped_seeds"
            return result
        marks = ",".join("?" for _ in seeds)
        # Claims touching a seed are collected through the subject/object indexes
        # first; otherwise SQLite starts at the document table and the cost grows
        # with the number of documents.
        rows = db.con.execute(
            f"WITH touching AS MATERIALIZED (SELECT uuid FROM kg_claims WHERE subject_id IN ({marks}) "
            f"UNION SELECT uuid FROM kg_claims WHERE object_id IN ({marks})) "
            "SELECT c.uuid,c.subject_id,c.object_id,c.predicate,e.uuid evidence_id,e.doc_id,e.doc_hash,"
            "e.parse_fingerprint,e.locator,e.exact_quote,e.chunk_id,g.id generation,s.validation_version"
            + _FROM.replace("FROM kg_current_claims c",
                            "FROM touching t CROSS JOIN kg_current_claims c ON c.uuid=t.uuid")
            + " AND " + predicate + " ORDER BY c.uuid,e.uuid,g.id LIMIT ?",
            (*seeds, *seeds, *params, MAX_EDGES)).fetchall()
        fingerprints, seen = {}, set()
        for row in rows:
            check()
            result.visited += 1
            if row['doc_id'] not in fingerprints:
                fingerprints[row['doc_id']] = parse_fingerprint(db, row['doc_id'])
            if fingerprints[row['doc_id']] != row['parse_fingerprint']:
                continue
            matched = match_quote(db, row['doc_id'], row['doc_hash'], row['locator'], row['exact_quote'])
            if not matched or matched['chunk_id'] != row['chunk_id'] or row['chunk_id'] in seen:
                continue
            seen.add(row['chunk_id'])
            result.hits.append(Hit(row['chunk_id'], row['uuid'], row['evidence_id'], row['predicate'],
                                   row['generation'], row['validation_version'], scope_key, (row['uuid'],)))
            if len(result.hits) == MAX_CANDIDATES:
                break
        check()
        if revisions(db) != snapshot:
            result.hits.clear()
            result.fallback = "revision_changed"
    except TimeoutError:
        result.hits.clear()
        result.fallback = "timeout"
    except sqlite3.Error:
        result.hits.clear()
        result.fallback = "timeout" if expired() else "unavailable"
    except (ValueError, TypeError, KeyError):
        result.hits.clear()
        result.fallback = "invalid_snapshot"
    finally:
        db.con.set_progress_handler(None, 0)
        result.elapsed_ms = round((perf_counter()-started)*1000, 3)
    return result


def augment(db, question, scope, combined, strong_ids):
    """Add a separate weighted graph rank; shadow preserves exact base output."""
    mode = db.get_meta("knowledge_search_mode") or "off"
    if mode not in {"shadow", "on"}:
        return combined, strong_ids, {"mode": "off"}
    result = search(db, question, scope, seed_chunks=[cid for cid, _ in combined[:MAX_SEEDS]])
    diagnostic = {"version": VERSION, "mode": mode, "visited": result.visited,
                  "candidates": len(result.hits), "elapsed_ms": result.elapsed_ms,
                  "fallback": result.fallback,
                  "scope_fingerprint": scope.fingerprint()}
    if mode == "shadow" or result.fallback:
        return combined, strong_ids, diagnostic
    scores = dict(combined)
    for rank, hit in enumerate(result.hits, 1):
        scores[hit.chunk_id] = scores.get(hit.chunk_id, 0) + WEIGHT / (60 + rank)
    diagnostic['paths'] = [asdict(hit) for hit in result.hits]
    return sorted(scores.items(), key=lambda item: (-item[1], item[0])), strong_ids | {h.chunk_id for h in result.hits}, diagnostic
