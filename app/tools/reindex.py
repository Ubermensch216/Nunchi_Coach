"""색인 전환 상태를 보고, 준비·전환·취소·되돌리기를 손으로 한다.

    python -m app.tools.reindex <project.db>              # 상태만 본다
    python -m app.tools.reindex <project.db> --run        # 준비 → 검증 → 전환
    python -m app.tools.reindex <project.db> --cancel     # 준비하던 새 조각을 버린다
    python -m app.tools.reindex <project.db> --rollback   # 마지막 전환을 되돌린다
    python -m app.tools.reindex <project.db> --resume     # 되돌린 뒤 멈춘 자동 전환을 다시 허용한다
    python -m app.tools.reindex <project.db> --prune      # 보관한 옛 조각을 지운다

평소에는 분석의 '질문 준비' 단계가 알아서 한다(core/reindex.py). 이 도구는 전환이
멈춘 까닭을 보거나, 전환한 뒤 문제가 있어 되돌릴 때 쓴다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..ai.client import OllamaClient
from ..ai.settings import project_client
from ..core import reindex
from ..db import Database
from . import console


def main(argv: list[str] | None = None) -> int:
    console.force_utf8()
    parser = argparse.ArgumentParser(description="조각 규칙이 바뀐 프로젝트의 색인 전환")
    parser.add_argument("project", type=Path, help="project.db 경로")
    action = parser.add_mutually_exclusive_group()
    for name in ("run", "cancel", "rollback", "resume", "prune"):
        action.add_argument(f"--{name}", action="store_true")
    args = parser.parse_args(argv)
    if not args.project.is_file():
        print(f"프로젝트 파일이 없습니다: {args.project}")
        return 2

    db = Database(args.project)
    db.init()
    try:
        client = project_client(db, OllamaClient)
        code = 0
        if args.run:
            result, note = reindex.run(db, client)
            print(note or "바꿀 것이 없습니다.")
            code = 0 if result is None or result.ok else 1
            for problem in (result.problems if result else []):
                print(" -", problem)
        elif args.cancel:
            print(f"준비하던 새 조각 {reindex.cancel(db):,}개를 버렸습니다.")
        elif args.rollback:
            result = reindex.rollback(db)
            print(f"전환 {result.revision}을 되돌렸습니다 · 문서 {result.changed_documents}건"
                  if result.changed_documents else "되돌린 문서가 없습니다.")
            for problem in result.problems:
                print(" -", problem)
            code = 0 if result.changed_documents else 1
        elif args.resume:
            reindex.resume(db)
            print("자동 전환을 다시 허용했습니다. 다음 분석에서 준비합니다.")
        elif args.prune:
            print(f"보관한 옛 조각 {reindex.prune(db):,}개를 지웠습니다.")

        state = reindex.status(db, client.embed_model)
        print(f"조각 규칙: 지금 {state.current or '?'} / 코드 {state.target}"
              + (" · 전환 필요" if state.needed else "") + (" · 되돌린 뒤 자동 전환 멈춤" if state.held else ""))
        print(f"조각 있는 문서 {state.documents:,}건 · 준비한 문서 {state.staged:,}건({state.staged_chunks:,}조각, "
              f"임베딩 빠짐 {state.unembedded:,}) · 전환 {state.revision}회 · 보관한 옛 조각 {state.archived:,}개")
        return code
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
