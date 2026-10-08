"""저장소에 올라가면 안 되는 파일이 추적되고 있는지 검사한다.

    python -m app.tools.repo_guard

`.gitignore`는 이미 추적 중인 파일을 빼 주지 않는다. 프로젝트 DB와 시험 산출물이
한 번 올라가면 그 뒤로는 조용히 따라다닌다 — 분석 대상 자료가 들어 있을 수 있는
파일이라, 추적되는 순간 CI가 멈추게 한다.

경로만 본다. 파일 내용은 열지 않고, 출력에도 경로만 적는다.

    0  추적 중인 금지 파일 없음
    1  금지 파일이 추적되고 있음
    2  검사하지 못함(git이 없거나 저장소가 아님) — 통과가 아니다
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]

# 폴더째 올라가면 안 되는 곳. .gitignore와 같은 목록이어야 한다.
FORBIDDEN_DIRS = (
    ".analysis_audit", ".test_runs", ".venv", ".venv-ci", "build", "dist",
    "data", "outputs", "wheelhouse", "htmlcov", ".pytest_cache", "__pycache__",
)
# 어디에 있든 올라가면 안 되는 파일. SQLite는 WAL·SHM·이행 전 백업까지 본다.
FORBIDDEN_SUFFIXES = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".pyc")
FORBIDDEN_MARKERS = (".db.bak", ".bak-v")


def violations(paths: list[str]) -> list[tuple[str, str]]:
    """(경로, 사유) 목록. 비어 있으면 깨끗하다."""
    found: list[tuple[str, str]] = []
    for raw in paths:
        path = PurePosixPath(raw.replace("\\", "/"))
        name = path.name.lower()
        folder = next((part for part in path.parts[:-1] if part in FORBIDDEN_DIRS), None)
        if folder:
            found.append((raw, f"추적하지 않는 폴더 {folder}/"))
        elif name.endswith(FORBIDDEN_SUFFIXES) or any(m in name for m in FORBIDDEN_MARKERS):
            found.append((raw, "데이터베이스·실행 산출물"))
    return found


def tracked(root: Path = ROOT) -> list[str] | None:
    """git이 추적하는 경로. git을 쓸 수 없으면 None."""
    try:
        done = subprocess.run(
            ["git", "-c", "core.quotepath=off", "ls-files", "-z"],
            cwd=root, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return [item for item in done.stdout.decode("utf-8", "replace").split("\0") if item]


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    paths = tracked()
    if paths is None:
        print("검사하지 못했습니다 — git을 실행할 수 없거나 저장소가 아닙니다.", file=sys.stderr)
        return 2
    found = violations(paths)
    if not found:
        print(f"추적 중인 금지 파일 없음 — {len(paths):,}개 경로 확인")
        return 0
    by_reason: dict[str, int] = {}
    for _path, reason in found:
        by_reason[reason] = by_reason.get(reason, 0) + 1
    print(f"추적하면 안 되는 파일 {len(found):,}개 (전체 {len(paths):,}개 중)", file=sys.stderr)
    for reason, count in sorted(by_reason.items()):
        print(f"  {reason}: {count:,}개", file=sys.stderr)
    for path, _reason in found[:20]:
        print(f"    {path}", file=sys.stderr)
    if len(found) > 20:
        print(f"    … 외 {len(found) - 20:,}개", file=sys.stderr)
    print("로컬 파일은 두고 추적만 풀려면:  git rm -r --cached <경로>", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
