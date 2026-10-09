"""명령줄 도구가 함께 쓰는 콘솔 처리."""

from __future__ import annotations

import sys


def force_utf8() -> None:
    """윈도우 콘솔이 cp949면 한글 출력에서 죽는다."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
