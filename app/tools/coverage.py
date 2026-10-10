"""Read-only inspection: python -m app.tools.coverage project.db [--output report.json]."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from ..core.coverage import snapshot
from ..core.output_policy import safe_text_writer
from ..db import Database


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="파일별 처리 범위와 누락 이유를 읽기 전용으로 조사합니다.")
    parser.add_argument("database", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    path = args.database.resolve(strict=True)
    db = Database(path)
    db._con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    db._con.row_factory = sqlite3.Row
    try:
        if int(db.get_meta("schema_version") or 0) < 9:
            parser.error("이 진단은 v9 이상이 필요합니다. 앱에서 백업을 포함한 승격을 먼저 진행하세요.")
        payload = json.dumps(snapshot(db, args.model, include_documents=True), ensure_ascii=False, indent=2)
        if args.output:
            with safe_text_writer(args.output, db) as handle:
                handle.write(payload + "\n")
        else:
            print(payload)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
