"""확장자 → 파서 디스패처.

파서는 예외를 던지지 않는다는 계약을 여기서 최종 보증한다. 어떤 파일이
무슨 이유로 실패하든 ParseResult로 돌아와야 조사 파이프라인이 멈추지 않는다.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import archive as _archive
from . import docx as _docx
from . import hwp as _hwp
from . import hwpx as _hwpx
from . import odt as _odt
from . import pdf as _pdf
from . import pptx as _pptx
from . import text as _text
from . import xlsx as _xlsx
from .base import (
    FAILED,
    LOCKED,
    MAX_BYTES,
    OK,
    PARTIAL,
    TOO_LARGE,
    UNSUPPORTED,
    ENCRYPTED,
    DocMeta,
    ParseResult,
    Section,
    failed,
    is_lock_error,
)

__all__ = ["parse", "supported_extensions", "ParseResult", "Section", "DocMeta"]

# 파서가 예외를 삼키고 문자열로만 돌려주는 경우를 위한 보조 판별.
_LOCK_HINTS = ("Permission denied", "being used by another process",
               "errno 13", "다른 프로세스가", "액세스", "sharing violation")


def _looks_locked(error: str | None) -> bool:
    if not error:
        return False
    lowered = error.lower()
    return any(hint.lower() in lowered for hint in _LOCK_HINTS)


def _is_locked_now(path: Path) -> bool:
    """파일을 지금 읽을 수 있는지 직접 확인한다.

    파싱이 실패한 뒤에만 부르므로 비용은 문제되지 않는다. 라이브러리가
    어떤 예외를 던지든, 파일 자체를 못 여는 상황이면 잠김으로 본다.
    """
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        return is_lock_error(exc)
    return False

_REGISTRY = {
    ".hwp": _hwp.parse,
    ".hwpx": _hwpx.parse,
    ".odt": _odt.parse,
    ".pdf": _pdf.parse,
    ".xlsx": _xlsx.parse,
    ".xlsm": _xlsx.parse,
    ".docx": _docx.parse,
    ".pptx": _pptx.parse,
    ".txt": _text.parse,
    ".csv": _text.parse,
    ".md": _text.parse,
    ".log": _text.parse,
    # 압축파일은 풀지 않고 안의 파일 이름만 읽는다(parsers/archive.py).
    ".zip": _archive.parse,
    # 구형 XLS는 olefile로 레코드를 직접 읽는다(parsers/xls.py).
    ".xls": _xlsx.parse,
    # 나머지 구형 포맷은 명시적으로 '지원 안 함'을 돌려준다. 조용히 빠지면
    # 사용자가 왜 검색에 안 걸리는지 알 수 없다.
    ".doc": _docx.parse,
    ".ppt": _pptx.parse,
}


# 문서 보안(DRM) 솔루션이 감싼 파일. 확장자는 .hwp·.xlsx 그대로인데 내용은 암호화돼 있어
# 파서마다 'OLE 복합문서가 아님', 'File is not a zip file' 같은 형식 오류를 낸다. 그러면
# 사용자는 파일이 깨졌다고 여긴다. 앞부분의 표시로 가려 '보호 문서'라고 알린다. 해제를 시도하지
# 않는다 — 권한 있는 PC에서 열어 확인할 일이다(규제개혁 비교연구 R08, C23).
_DRM_MARKERS = (b"DRMONE", b"SCDSA00", b"<## Fasoo", b"FSN_DRM", b"MarkAny", b"DOCUMENT SAFER")
_DRM_HEAD = 1024
DRM_NOTE = "보호(DRM) 문서 — 이 PC에서는 내용을 읽을 수 없습니다. 권한 있는 PC에서 열어 확인하세요."


def _drm_protected(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(_DRM_HEAD)
    except OSError:
        return False
    return any(marker in head for marker in _DRM_MARKERS)


# 파일은 큰데 읽은 글이 거의 없으면 본문이 그림이거나 읽지 못한 부분이 있다는 뜻이다.
# 그런 문서를 '읽음'으로 두면 좌석배치도·상장·사건위치도 같은 것이 다 읽은 문서로
# 보인다(실측: 본문 200자 미만인 '읽음' 58건, 그중 7MB에 17자인 것도 있었다).
THIN_CHARS = 200
THIN_BYTES = 50_000
THIN_NOTE = "파일 크기에 비해 읽은 글이 거의 없습니다 — 그림이나 읽지 못한 부분이 있을 수 있습니다."


def _flag_thin(result: ParseResult, size: int) -> None:
    if result.status == OK and result.char_count < THIN_CHARS and size >= THIN_BYTES:
        result.status = PARTIAL
        result.note = f"{result.note}; {THIN_NOTE}" if result.note else THIN_NOTE


# 글 파일은 본문에 그 낱말이 나올 수 있다. 보호 솔루션은 이진 문서만 감싼다.
_NO_DRM_CHECK = (".txt", ".csv", ".md", ".log")


def supported_extensions() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def parse(path: str | Path) -> ParseResult:
    path = Path(path)
    parser = _REGISTRY.get(path.suffix.lower())
    if parser is None:
        return failed("none", f"지원하지 않는 확장자: {path.suffix or '(없음)'}", UNSUPPORTED)

    try:
        size = path.stat().st_size
    except OSError as exc:
        return failed("none", f"파일 접근 실패: {exc}")

    if size > MAX_BYTES:
        return failed("none", f"크기 상한 초과 ({size:,}바이트)", TOO_LARGE)

    if path.suffix.lower() not in _NO_DRM_CHECK and _drm_protected(path):
        return failed(path.suffix.lower().lstrip("."), DRM_NOTE, ENCRYPTED)

    started = time.perf_counter()
    try:
        result = parser(path)
    except OSError as exc:
        # 잠긴 파일은 영구 실패가 아니다. 사용자가 문서를 열어 둔 채
        # 스캔하는 일이 흔하고, 닫으면 그대로 읽힌다 (PRD §19).
        kind = LOCKED if is_lock_error(exc) else FAILED
        result = failed(path.suffix.lower().lstrip("."), f"{type(exc).__name__}: {exc}", kind)
    except Exception as exc:  # 파서가 놓친 예외의 최종 방어선
        result = failed(path.suffix.lower().lstrip("."), f"{type(exc).__name__}: {exc}")

    # 실패했다면 '지금 잠겨 있어서'인지 다시 판정한다. 오류 메시지에만
    # 의존하면 놓친다 — PyMuPDF처럼 OSError가 아닌 자체 예외를 던지면서
    # 잠김이라는 단서를 남기지 않는 라이브러리가 있다. 그래서 메시지를
    # 뒤지는 대신 파일을 실제로 열어 보고 판단한다.
    if result.status == FAILED and (_looks_locked(result.error) or _is_locked_now(path)):
        result.status = LOCKED
    _flag_thin(result, size)
    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return result
