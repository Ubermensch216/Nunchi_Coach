"""동결 합성 질문의 모델 OFF FTS 기준선과 구조 그래프 비교.

python -m app.tools.knowledge_eval --json 결과.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import tempfile
from pathlib import Path
from time import perf_counter

from ..db import Database
from ..search.fts import search_chunk_ids
from .console import force_utf8

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "knowledge"


def load_fixture(folder=FIXTURES):
    # Git/Windows의 CRLF 변환이 동결 내용의 변경으로 오인되지 않도록 LF로 읽는다.
    payload = (folder / "corpus.json").read_text(encoding="utf-8").encode("utf-8")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if hashlib.sha256(payload).hexdigest() != manifest["sha256"]:
        raise ValueError("동결 평가 자료의 해시가 다릅니다.")
    corpus = json.loads(payload)
    return corpus, manifest


def run(split: str = "all", graph_mode: str = "off", folder: Path = FIXTURES) -> dict:
    if graph_mode not in ('off', 'shadow', 'on'):
        raise ValueError('올바르지 않은 그래프 모드입니다.')
    corpus, manifest = load_fixture(folder)
    rows, timings, graph_timings = [], [], []
    graph_claims = 0
    with tempfile.TemporaryDirectory(prefix="nunchi-knowledge-eval-") as temp:
        db = Database(Path(temp) / "project.db")
        try:
            db.init()
            source_id = db.add_source(Path(temp) / "corpus")
            documents, chunks = {}, {}
            for task_id, group in enumerate(corpus["groups"], 1):
                if graph_mode != 'off':
                    db.con.execute('INSERT INTO tasks(id,name) VALUES (?,?)', (task_id, group['task']))
                for doc in group["documents"]:
                    doc_id = db.upsert_document(source_id, {"path": str(Path(temp) / doc["path"]),
                        "filename": Path(doc["path"]).name, "ext": ".txt", "hash": doc["sha256"],
                        "eff_year": doc["document_year"]})
                    chunk_id = db.con.execute("INSERT INTO chunks(doc_id,ordinal,locator,text) VALUES (?,0,?,?)",
                                               (doc_id, doc["locator"], doc["text"])).lastrowid
                    documents[doc["key"]] = doc_id
                    chunks[chunk_id] = doc["key"]
                    if graph_mode != 'off':
                        db.con.execute("UPDATE documents SET parse_status='ok',parser='text',parser_version='1' WHERE id=?", (doc_id,))
                        db.replace_sections(doc_id, [('paragraph', 0, doc['locator'], doc['text'])])
                        db.con.execute("INSERT INTO task_docs(task_id,doc_id,origin) VALUES (?,?,'user')", (task_id, doc_id))
                if graph_mode != 'off':
                    # Build from input documents using the product extractor.
                    # Expected evidence/allowed relations never feed projection.
                    from ..core.reconstruction import extract_task
                    from ..core.knowledge.projection import project_task
                    from datetime import date
                    db.sync_task_items(task_id, extract_task(db, task_id, date.fromisoformat(corpus['basis_date'])))
                    project_task(db, task_id)
            if graph_mode != 'off':
                graph_claims = db.con.execute('SELECT COUNT(*) FROM kg_current_claims').fetchone()[0]
                db.set_meta('knowledge_search_mode', graph_mode)
            for q in corpus["questions"]:
                if split != "all" and q["split"] != split:
                    continue
                allowed = [documents[key] for key in q["allowed_documents"]]
                begin = perf_counter()
                found = search_chunk_ids(db, q["question"], limit=manifest["top_k"], doc_ids=allowed)
                baseline = list(found)
                graph_diagnostic = {}
                if graph_mode != 'off':
                    from ..search.graph import augment
                    from ..search.query import QueryScope
                    combined, _, graph_diagnostic = augment(db, q['question'], QueryScope(doc_ids=allowed),
                        [(cid, 1/(60+rank)) for rank,cid in enumerate(found, 1)], set(found))
                    found = [cid for cid,_ in combined[:manifest['top_k']]]
                    graph_timings.append(graph_diagnostic['elapsed_ms'])
                timings.append((perf_counter() - begin) * 1000)
                retrieved = list(dict.fromkeys(chunks[c] for c in found))
                expected = {e["document"] for e in q["expected_evidence"]}
                rows.append({"id": q["id"], "category": q["category"], "split": q["split"],
                             "expected": sorted(expected), "retrieved": retrieved,
                             "recall_at_10": len(expected.intersection(retrieved)) / len(expected) if expected else None,
                             "scope_leaks": len(set(retrieved) - set(q["allowed_documents"])),
                             **({'baseline_retrieved': list(dict.fromkeys(chunks[c] for c in baseline)),
                                 'graph': graph_diagnostic} if graph_mode != 'off' else {})})
        finally:
            db.close()
    metrics = {}
    for category in ("simple", "alias", "linked", "temporal", "missing"):
        scored = [r for r in rows if r["category"] == category and r["recall_at_10"] is not None]
        metrics[category] = {"questions": sum(r["category"] == category for r in rows),
                             "scored_questions": len(scored),
                             "all_evidence_found": sum(r["recall_at_10"] == 1 for r in scored),
                             "recall_at_10": statistics.mean(r["recall_at_10"] for r in scored) if scored else None}
    return {"comparison": 'B' if graph_mode == 'on' else 'A',
            "retrieval": 'FTS + structure graph, model OFF' if graph_mode == 'on' else 'existing FTS, model OFF',
            "graph_mode": graph_mode, "graph_current_claims": graph_claims,
            **({'graph_p95_ms': sorted(graph_timings)[max(0,int(len(graph_timings)*.95)-1)],
                'graph_added_questions': sum(r['retrieved'] != r['baseline_retrieved'] for r in rows),
                'graph_fallbacks': sum(bool(r['graph']['fallback']) for r in rows)} if graph_timings else {}),
            "fixture_sha256": manifest["sha256"],
            "source": corpus["source"], "review": corpus["review"], "split": split,
            "hardware": {"os": platform.platform(), "cpu": platform.processor(), "logical_cpus": os.cpu_count(),
                         "python": platform.python_version(), "sqlite": __import__("sqlite3").sqlite_version},
            "questions": len(rows), "categories": metrics,
            "scope_leaks": sum(r["scope_leaks"] for r in rows),
            "retrieval_p95_ms": sorted(timings)[max(0, int(len(timings) * .95) - 1)],
            "unmeasured": ["LLM 답변 정확도", "의미 지지", "벡터 RAG A/B", "모델 관계 후보 C", "그래프 RSS/규모 성능", "실제 자료 정확도"],
            "results": rows}


def main(argv=None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("all", "tuning", "holdout"), default="all")
    parser.add_argument("--json", type=Path)
    parser.add_argument('--graph', choices=('off', 'shadow', 'on'), default='off')
    parser.add_argument('--fixture', type=Path, default=FIXTURES, help='다른 평가 묶음 폴더(corpus.json·manifest.json)')
    args = parser.parse_args(argv)
    result = run(args.split, args.graph, args.fixture)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "results"}, ensure_ascii=False, indent=2))
    return int(result["scope_leaks"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())
