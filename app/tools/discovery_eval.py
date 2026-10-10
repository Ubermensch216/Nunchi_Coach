"""Reproduce late-body and singleton counterexamples without user documents.

--live uses the local embedding model. The default uses controlled vectors to
test plumbing only. Neither mode estimates institution-wide discovery quality.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import time
from pathlib import Path
from uuid import uuid4

from ..ai.client import OllamaClient
from ..ai.discover import discover
from ..core.clustering import cluster
from ..core.coverage import snapshot
from ..core.output_policy import safe_text_writer
from ..db import Database
from ..jobs.pipeline import _embed_text
from ..search.vector import VectorStore, pack
from .cluster_report import evaluate
from .console import force_utf8

TOPICS = (
    ("보조금정산", ["보조금 정산 업무. 지원사업의 증빙 영수증과 집행내역을 대조하고 잔액을 반납한다.",
                   "보조금 정산 업무. 보조사업자가 제출한 지출 증빙을 검토하고 미집행 보조금을 반환한다.",
                   "보조금 정산 업무. 집행 명세와 증빙을 확인한 뒤 정산 결과보고서와 반납 고지서를 작성한다."]),
    ("정보공개", ["정보공개 청구 업무. 공개 대상 기록물을 조회하고 개인정보를 가린 뒤 공개 여부를 결정한다.",
                  "정보공개 청구 업무. 청구인이 요구한 기록을 찾고 비공개 사유를 검토하여 결정 통지서를 보낸다.",
                  "정보공개 청구 업무. 기록물의 공개 가능 범위를 판단하고 제삼자 의견 조회 후 열람을 제공한다."]),
    ("소방점검", ["소방 안전점검 업무. 소화기와 피난 통로를 점검하고 불량 시설의 보수 결과를 기록한다."]),
)


def run(folder: Path, live: bool = False, embed_model: str = "bge-m3") -> dict:
    started = time.perf_counter()
    folder.mkdir(parents=True, exist_ok=False)
    source = folder / "synthetic-input"
    source.mkdir()
    db = Database(folder / "project.db")
    db.init()
    sid = db.add_source(source)
    model = embed_model if live else "controlled-vectors"
    db.set_meta("embed_model", model)
    client = OllamaClient(embed_model=embed_model) if live else None
    if client and not client.health().embedding_ready:
        db.close()
        raise RuntimeError("로컬 임베딩 모델이 준비되지 않았습니다.")
    cache = {}
    def vector(text, axis):
        if not live:
            return [float(i == axis) for i in range(4)]
        if text not in cache:
            values, error = client.embed([text])
            if error or not values:
                raise RuntimeError(error or "empty embedding")
            cache[text] = values[0]
        return cache[text]
    prefix = "공통 표지입니다. 이 자료는 기관 내부의 인수인계 참고자료이며 문서 보관 안내와 공통 양식을 포함합니다.\n" * 40
    truth = {}
    try:
        for axis, (label, texts) in enumerate(TOPICS, 1):
            for tail in texts:
                name = f"자료_{len(truth) + 1:03}.txt"
                path = source / name
                body = prefix + "\n" + tail
                path.write_text(body, encoding="utf-8")
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                doc = db.upsert_document(sid, {"path": str(path), "filename": name, "ext": ".txt",
                    "hash": digest, "parse_status": "ok", "char_count": len(body)})
                truth[doc] = label
                db.replace_sections(doc, [("paragraph", 1, "공통 표지", prefix),
                                          ("paragraph", 2, "후반부 업무", tail)])
                # Keep the same two evidence units in both comparisons.
                chunks = db.replace_document_chunks(doc, [(1, "공통 표지", prefix), (2, "후반부 업무", tail)])
                representative = vector(_embed_text(name, body), 0)
                db.save_doc_embedding(doc, model, len(representative), pack(representative), 1200)
                for (cid, _), text, direction in zip(chunks, [prefix, tail], [0, axis]):
                    v = vector(text, direction)
                    db.save_chunk_embedding(cid, model, len(v), pack(v))
        before_bytes = {p.name: p.read_bytes() for p in source.iterdir()}
        old = cluster(VectorStore.load(db.con, model))
        baseline = {doc: str(i) for i, group in enumerate(old.clusters) for doc in group.doc_ids}
        result = discover(db)
        candidate = {r["doc_id"]: str(r["task_id"]) for r in db.con.execute("SELECT * FROM task_docs")}
        report = {
            "fixture": "identical-prefix-7-documents-v1", "mode": "local_embedding" if live else "controlled_vectors",
            "model": model, "python": platform.python_version(), "platform": platform.platform(),
            "baseline_method": "prefix vectors + threshold 0.82 + minimum 3 (no model naming)",
            "candidate_method": "complete chunk vectors + inverse duplicate frequency + weak singleton",
            "baseline": evaluate(truth, baseline), "candidate": evaluate(truth, candidate),
            "singleton_candidates": result.singleton_candidates,
            "original_bytes_unchanged": before_bytes == {p.name: p.read_bytes() for p in source.iterdir()},
            "coverage": snapshot(db, model), "elapsed_seconds": round(time.perf_counter() - started, 3),
            "limitations": ["7 synthetic documents only; not a representative evaluation set",
                            "Pairwise metrics are not task-discovery precision/recall",
                            "No generation-model validation or real institution documents",
                            "Correlated pairs do not support a binomial confidence interval"],
        }
        with safe_text_writer(folder / "result.json", db) as handle:
            handle.write(json.dumps(report, ensure_ascii=False, indent=2))
        return report
    finally:
        db.close()


def main(argv=None):
    force_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(".test_runs") / f"discovery-{uuid4().hex[:10]}")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--embed-model", default="bge-m3")
    args = parser.parse_args(argv)
    result = run(args.output_dir.resolve(), args.live, args.embed_model)
    print(json.dumps({k: result[k] for k in ("mode", "baseline", "candidate", "elapsed_seconds")}, ensure_ascii=False, indent=2))
    print(args.output_dir / "result.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
