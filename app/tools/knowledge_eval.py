"""동결 합성 질문의 모델 OFF 기존 FTS 검색 기준선. B/C는 아직 실행하지 않는다.

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


def run(split: str = "all") -> dict:
    corpus, manifest = load_fixture()
    rows, timings = [], []
    with tempfile.TemporaryDirectory(prefix="nunchi-knowledge-eval-") as temp:
        db = Database(Path(temp) / "project.db")
        try:
            db.init()
            source_id = db.add_source(Path(temp) / "corpus")
            documents, chunks = {}, {}
            for group in corpus["groups"]:
                for doc in group["documents"]:
                    doc_id = db.upsert_document(source_id, {"path": str(Path(temp) / doc["path"]),
                        "filename": Path(doc["path"]).name, "ext": ".txt", "hash": doc["sha256"],
                        "eff_year": doc["document_year"]})
                    chunk_id = db.con.execute("INSERT INTO chunks(doc_id,ordinal,locator,text) VALUES (?,0,?,?)",
                                               (doc_id, doc["locator"], doc["text"])).lastrowid
                    documents[doc["key"]] = doc_id
                    chunks[chunk_id] = doc["key"]
            for q in corpus["questions"]:
                if split != "all" and q["split"] != split:
                    continue
                allowed = [documents[key] for key in q["allowed_documents"]]
                begin = perf_counter()
                found = search_chunk_ids(db, q["question"], limit=manifest["top_k"], doc_ids=allowed)
                timings.append((perf_counter() - begin) * 1000)
                retrieved = list(dict.fromkeys(chunks[c] for c in found))
                expected = {e["document"] for e in q["expected_evidence"]}
                rows.append({"id": q["id"], "category": q["category"], "split": q["split"],
                             "expected": sorted(expected), "retrieved": retrieved,
                             "recall_at_10": len(expected.intersection(retrieved)) / len(expected) if expected else None,
                             "scope_leaks": len(set(retrieved) - set(q["allowed_documents"]))})
        finally:
            db.close()
    metrics = {}
    for category in ("simple", "alias", "linked", "temporal", "missing"):
        scored = [r for r in rows if r["category"] == category and r["recall_at_10"] is not None]
        metrics[category] = {"questions": sum(r["category"] == category for r in rows),
                             "scored_questions": len(scored),
                             "all_evidence_found": sum(r["recall_at_10"] == 1 for r in scored),
                             "recall_at_10": statistics.mean(r["recall_at_10"] for r in scored) if scored else None}
    return {"comparison": "A", "retrieval": "existing FTS, model OFF", "fixture_sha256": manifest["sha256"],
            "source": corpus["source"], "review": corpus["review"], "split": split,
            "hardware": {"os": platform.platform(), "cpu": platform.processor(), "logical_cpus": os.cpu_count(),
                         "python": platform.python_version(), "sqlite": __import__("sqlite3").sqlite_version},
            "questions": len(rows), "categories": metrics,
            "scope_leaks": sum(r["scope_leaks"] for r in rows),
            "retrieval_p95_ms": sorted(timings)[max(0, int(len(timings) * .95) - 1)],
            "unmeasured": ["LLM 답변 정확도", "의미 지지", "B/C 개선", "그래프 RSS/지연", "실제 자료 정확도"],
            "results": rows}


def main(argv=None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("all", "tuning", "holdout"), default="all")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    result = run(args.split)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "results"}, ensure_ascii=False, indent=2))
    return int(result["scope_leaks"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())
