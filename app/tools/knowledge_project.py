"""개발용 구조 투영. UI 자동 실행·큐 lease 작업자는 P3에서 연결한다."""
import argparse
import json
from dataclasses import asdict
from pathlib import Path

from ..core.knowledge.projection import project_document, project_task
from ..db import Database
from .console import force_utf8


def main(argv=None):
    force_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True, help="project.db가 있는 프로젝트 폴더")
    parser.add_argument("--task", type=int)
    args = parser.parse_args(argv)
    path = args.project / "project.db"
    if not path.is_file():
        parser.error("기존 project.db를 찾을 수 없습니다.")
    with Database(path) as db:
        db.init()
        results = []
        if args.task is not None:
            results.append(project_task(db, args.task))
        else:
            # 문서/업무 하나씩 읽어 처리한다. 전체 본문을 메모리에 올리지 않는다.
            for doc_id in [r[0] for r in db.con.execute("SELECT id FROM documents ORDER BY id")]:
                results.append(project_document(db, doc_id))
            for task_id in [r[0] for r in db.con.execute("SELECT id FROM tasks ORDER BY id")]:
                results.append(project_task(db, task_id))
        print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
        return int(any(result.diagnostics for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
