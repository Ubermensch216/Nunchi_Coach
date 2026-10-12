"""그래프 구축·조회의 규모 시험 (지식그래프 계획 §12.4). 모델을 쓰지 않는다.

python -m app.tools.knowledge_scale --documents 1000 --json 결과.json

합성 업무·문서를 직접 DB에 넣고 규칙 추출기와 투영을 돌린다. 파서·임베딩·모델 시간은
포함하지 않는다 — 그래프 비용만 따로 잰다. 실제 기관 자료의 관계 밀도·문서 길이는
다르므로 이 수치는 실제 자료의 성능 보증이 아니다. 문서 하나가 한 업무의 한 해
제출 공문인 가장 단순한 모양이고, 업무당 문서 수를 늘려 관계 차수를 키운다.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics
import sys
import tempfile
from datetime import date
from pathlib import Path
from time import perf_counter

from ..db import Database
from ..search.fts import search_chunk_ids
from .console import force_utf8

FILLER = ("본 문서는 해당 업무의 연간 추진 과정에서 작성된 공문으로 관련 부서에 시행한다. "
          "세부 내용은 붙임 자료를 참고하고 기한 내에 제출하여 주시기 바랍니다. ")


def peak_rss_mb() -> float | None:
    """프로세스의 최대 작업 집합. 읽을 수 없으면 None — 추정으로 채우지 않는다."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if ctypes.windll.kernel32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return counters.PeakWorkingSetSize / 1024 / 1024
        return None
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / 1024 / 1024 if sys.platform == "darwin" else peak / 1024
    except ImportError:
        return None


def ms(values: list[float], q: float) -> float | None:
    value = pct(values, q)
    return None if value is None else round(value, 2)


def pct(values: list[float], q: float) -> float | None:
    return sorted(values)[max(0, min(len(values) - 1, int(len(values) * q) - 1))] if values else None


def build(db, root: Path, documents: int, per_task: int, seed: int) -> list[tuple[int, str, list[int]]]:
    """(task_id, 업무명, 문서 ID들). 업무마다 서식·제출처·기한·금액을 문서 본문에 적는다."""
    rng = random.Random(seed)
    source = db.add_source(root)
    tasks, made = [], 0
    task_id = 0
    with db.transaction():
        while made < documents:
            task_id += 1
            name = f"합성업무{task_id:05d}"
            db.con.execute("INSERT INTO tasks(id,name) VALUES (?,?)", (task_id, name))
            ids = []
            for year_index in range(min(per_task, documents - made)):
                year = 2020 + year_index
                text = (f"서식: 서식{task_id % 97}\n제출처: {task_id % 31}과\n"
                        f"제출기한: {year}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}\n"
                        f"금액: {rng.randint(1, 900) * 1000:,} 원\n" + FILLER * 3)
                doc = db.upsert_document(source, {
                    "path": str(root / f"{task_id}_{year}.txt"), "filename": f"{task_id}_{year}.txt",
                    "ext": ".txt", "hash": f"h{task_id}-{year}", "eff_year": year,
                    "parse_status": "ok", "parser": "text", "parser_version": "1"})
                db.replace_sections(doc, [("paragraph", 0, "본문", text)])
                db.con.execute("INSERT INTO chunks(doc_id,ordinal,locator,text) VALUES (?,0,'본문',?)", (doc, text))
                db.con.execute("INSERT INTO task_docs(task_id,doc_id,origin) VALUES (?,?,'user')", (task_id, doc))
                ids.append(doc)
                made += 1
            tasks.append((task_id, name, ids))
    return tasks


def run(documents: int = 1000, per_task: int = 5, questions: int = 200, seed: int = 7) -> dict:
    from ..core.knowledge.projection import project_task
    from ..core.reconstruction import extract_task
    from ..search.graph import augment
    from ..search.query import QueryScope

    rss_start = peak_rss_mb()
    with tempfile.TemporaryDirectory(prefix="nunchi-knowledge-scale-") as temp:
        db = Database(Path(temp) / "project.db")
        try:
            db.init()
            t0 = perf_counter()
            tasks = build(db, Path(temp) / "corpus", documents, per_task, seed)
            insert_s = perf_counter() - t0
            rss_after_insert = peak_rss_mb()
            extract_ms, project_ms = [], []
            basis = date(2026, 1, 1)
            for task_id, _name, _ids in tasks:
                t = perf_counter()
                db.sync_task_items(task_id, extract_task(db, task_id, basis))
                extract_ms.append((perf_counter() - t) * 1000)
                t = perf_counter()
                project_task(db, task_id)
                project_ms.append((perf_counter() - t) * 1000)
            rss_after_project = peak_rss_mb()
            counts = {name: db.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for name, table in (
                ("entities", "kg_entities"), ("claims", "kg_claims"), ("current_claims", "kg_current_claims"),
                ("evidence", "kg_evidence"), ("supports", "kg_claim_evidence"), ("generations", "kg_generations"))}
            degree = [r[0] for r in db.con.execute("SELECT COUNT(*) FROM kg_claims GROUP BY subject_id")]
            db.set_meta("knowledge_search_mode", "on")
            rng = random.Random(seed + 1)
            timings = {}
            for label in ("task_scope", "project_wide"):
                base_ms, graph_ms, added, fallbacks, visited = [], [], 0, 0, []
                for _ in range(questions):
                    task_id, name, ids = rng.choice(tasks)
                    question = rng.choice((f"{name} 제출기한", f"{name} 제출처", f"{name} 서식", f"{name} 금액"))
                    # 업무 범위는 후보 문서가 몇 건뿐이고, 프로젝트 전체는 모든 문서가 후보다.
                    docs = ids if label == "task_scope" else None
                    t = perf_counter()
                    found = search_chunk_ids(db, question, limit=10, doc_ids=docs)
                    base_ms.append((perf_counter() - t) * 1000)
                    t = perf_counter()
                    combined, _, diagnostic = augment(db, question, QueryScope(doc_ids=docs),
                                                      [(cid, 1 / (60 + rank)) for rank, cid in enumerate(found, 1)], set(found))
                    graph_ms.append((perf_counter() - t) * 1000)
                    added += [cid for cid, _ in combined[:10]] != found
                    fallbacks += bool(diagnostic["fallback"])
                    visited.append(diagnostic.get("visited", 0))
                timings[label] = {
                    "fts_p50": ms(base_ms, .5), "fts_p95": ms(base_ms, .95),
                    "graph_added_p50": ms(graph_ms, .5), "graph_added_p95": ms(graph_ms, .95),
                    "graph_changed_results": added, "graph_fallbacks": fallbacks,
                    "visited_max": max(visited) if visited else 0}
            rss_after_query = peak_rss_mb()
            size_mb = Path(db.path).stat().st_size / 1024 / 1024
        finally:
            db.close()
    return {
        "documents": documents, "tasks": len(tasks), "per_task": per_task, "questions": questions,
        "counts": counts, "claim_degree_max": max(degree) if degree else 0,
        "claim_degree_mean": round(statistics.mean(degree), 2) if degree else 0,
        "seconds": {"insert": round(insert_s, 2), "extract_total": round(sum(extract_ms) / 1000, 2),
                    "projection_total": round(sum(project_ms) / 1000, 2)},
        "projection_ms": {"p50": ms(project_ms, .5), "p95": ms(project_ms, .95)},
        "query_ms": timings,
        "db_size_mb": round(size_mb, 1),
        "peak_rss_mb": {"start": rss_start and round(rss_start), "after_insert": rss_after_insert and round(rss_after_insert),
                        "after_projection": rss_after_project and round(rss_after_project),
                        "after_query": rss_after_query and round(rss_after_query)},
        "hardware": {"os": platform.platform(), "cpu": platform.processor(), "logical_cpus": os.cpu_count(),
                     "python": platform.python_version(), "sqlite": __import__("sqlite3").sqlite_version},
        "unmeasured": ["파서·임베딩·모델 시간", "벡터 행렬 로딩 비용", "실제 기관 문서의 관계 밀도",
                       "cold cache(OS 캐시를 비우지 않았다)"],
    }


def main(argv=None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=int, default=1000)
    parser.add_argument("--per-task", type=int, default=5)
    parser.add_argument("--questions", type=int, default=200)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    result = run(args.documents, args.per_task, args.questions, args.seed)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
