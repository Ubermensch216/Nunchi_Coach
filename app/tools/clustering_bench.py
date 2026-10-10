"""Synthetic clustering-only performance measurement; no parsing/model calls.

python -m app.tools.clustering_bench --documents 10000 --dimension 1024
Outputs JSON to stdout. Does not read or write a user source or project database.
"""
from __future__ import annotations

import argparse
import json
import platform
import time

import numpy as np

from ..core.clustering import BLOCK, cluster
from ..search.vector import VectorStore, normalize


def run(n=10000, dim=1024, groups=24):
    if not 1 <= groups <= dim or n < 3 * groups or n > 100000 or dim > 4096:
        raise ValueError('require 3 * groups <= documents <= 100000 and groups <= dimension <= 4096')
    rng = np.random.default_rng(20261010)
    matrix = rng.normal(0, .005, size=(n, dim)).astype(np.float32)
    expected = np.arange(n) % groups
    matrix[np.arange(n), expected] += 1
    store = VectorStore(np.arange(1, n + 1, dtype=np.int64), normalize(matrix))
    started = time.perf_counter()
    result = cluster(store)
    elapsed = time.perf_counter() - started
    ids = [doc for c in result.clusters for doc in c.doc_ids] + result.unassigned
    # Check preservation independently of the algorithm's reported counts.
    partition = len(ids) == n and len(set(ids)) == n and set(ids) == set(range(1, n + 1))
    pure = all(len({int(expected[d - 1]) for d in c.doc_ids}) == 1 for c in result.clusters)
    return {'scope': 'synthetic_clustering_only', 'documents': n, 'dimension': dim,
            'expected_groups': groups, 'clusters': len(result.clusters), 'unassigned': len(result.unassigned),
            'elapsed_seconds': elapsed, 'partition_invariant': partition, 'controlled_groups_pure': pure,
            'vector_matrix_bytes': store.matrix.nbytes,
            'similarity_block_bytes_estimate': min(n, BLOCK) * n * 4,
            'memory_note': 'array sizes only; not measured peak process memory',
            'python': platform.python_version(), 'platform': platform.platform(), 'numpy': np.__version__,
            'limitations': ['excludes_io_parsing_embedding_naming_and_recovery',
                            'controlled_vectors_do_not_estimate_business_accuracy']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--documents', type=int, default=10000)
    parser.add_argument('--dimension', type=int, default=1024)
    parser.add_argument('--groups', type=int, default=24)
    args = parser.parse_args(argv)
    try:
        result = run(args.documents, args.dimension, args.groups)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['partition_invariant'] and result['controlled_groups_pure'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
