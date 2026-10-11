"""Read-only graph diagnostics for an existing v13 project (no migrations).

python -m app.tools.knowledge_search --project PATH --query QUESTION --docs 1 2
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

from ..db import Database
from ..search.graph import search
from ..search.query import QueryScope, infer_scope
from .console import force_utf8


def main(argv=None):
    force_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--query', required=True)
    parser.add_argument('--docs', nargs='*', type=int, default=None, help='omit for all; empty for no documents')
    parser.add_argument('--task', type=int)
    parser.add_argument('--years', nargs='*', type=int, default=[])
    parser.add_argument('--year-basis', choices=('document_date', 'performance', 'evaluation', 'submitted', 'case'), default='document_date')
    parser.add_argument('--case', type=int)
    parser.add_argument('--budget-ms', type=float, default=150)
    args = parser.parse_args(argv)
    path = args.project / 'project.db'
    if not path.is_file():
        parser.error('기존 project.db를 찾을 수 없습니다.')
    try:
        scope = infer_scope(args.query).merged_with(QueryScope(doc_ids=args.docs, task_id=args.task,
                  years=args.years, year_basis=args.year_basis, case_id=args.case))
    except ValueError as exc:
        parser.error(str(exc))
    db = Database(path, readonly=True)
    try:
        result = search(db, args.query, scope, budget_ms=args.budget_ms)
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return int(result.fallback not in ('', 'no_scoped_seeds'))
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
