"""결과 검증 — 실행이 지켜야 할 것을 실행 뒤에 다시 본다.

지금은 원본 지문만 있다. 근거 재확인·모드 표기·누락 목록 검사는 산출물 도구와
함께 들어온다(계획서 V1).
"""

from __future__ import annotations

from pathlib import Path


def fingerprint(db) -> dict[str, tuple[int, int] | None]:
    """분석된 원본 파일의 크기와 수정 시각. 실행 앞뒤로 견준다.

    내용 해시가 아니라 파일 정보만 본다. 문서 1만 건의 해시를 실행마다 두 번
    다시 계산하면 그것만으로 몇 분이 걸린다. 도구에는 원본을 여는 길이 없으므로
    (actions/context.py) 이 검사는 그 약속이 깨졌을 때 알아채는 그물이다.
    사라진 파일은 None으로 적는다 — 실행 도중 사라진 것도 변화다.
    """
    marks: dict[str, tuple[int, int] | None] = {}
    for row in db.con.execute("SELECT path FROM documents WHERE missing_since IS NULL"):
        try:
            stat = Path(row["path"]).stat()
        except OSError:
            marks[row["path"]] = None
        else:
            marks[row["path"]] = (stat.st_size, stat.st_mtime_ns)
    return marks


def changed(before: dict, after: dict) -> int:
    """앞뒤가 다른 파일 수."""
    return sum(1 for path in before.keys() | after.keys() if before.get(path) != after.get(path))
