"""자료원 안에서의 문서 위치.

문서를 절대 경로로 기억하면 PC나 드라이브가 바뀌는 순간 전부 끊긴다.
'어느 자료 폴더의 어디'로 적어 두면 폴더를 통째로 옮겨도 다시 찾을 수 있고,
다른 사람에게 넘기는 묶음에 개인 PC의 경로가 실리지도 않는다.
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath


def relative_to_source(path: str, root: str) -> str:
    """자료원 폴더 기준 상대 경로('/' 구분). 폴더 밖이면 파일명만 돌려준다."""
    for flavour in (PureWindowsPath, Path):
        try:
            return flavour(path).relative_to(flavour(root)).as_posix()
        except ValueError:
            continue
    return PureWindowsPath(path).name
