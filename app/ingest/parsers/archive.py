"""압축파일 — 풀지 않고 안에 든 파일의 이름만 읽는다.

전임자 폴더의 압축파일은 대개 부서가 보낸 증빙 묶음이나 제출본이다. 예전에는 대상이
아니라서 그런 파일이 있다는 사실조차 업무에 남지 않았다. 이름 목록만 있어도 "이
업무에 재무과 증빙자료가 있었다", "제출본에 무엇이 들어갔다"를 알 수 있고 검색된다.

내용은 읽지 않는다. 압축을 풀어 디스크에 쓰지 않고, 안의 파일을 열지도 않는다 —
원본은 읽기만 한다는 약속과, 출처를 알 수 없는 파일을 실행 경로에 올리지 않는다는
원칙 때문이다. 그래서 항상 '일부만 읽음'이다.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from .base import EMPTY, PARTIAL, ParseResult, Section, failed

PARSER = "archive"
MAX_ENTRIES = 500
NOTE = "압축파일입니다. 안에 든 파일의 이름만 읽었고 내용은 읽지 않았습니다."


def parse(path: Path) -> ParseResult:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
    except zipfile.BadZipFile as exc:
        return failed(PARSER, f"ZIP 구조 손상: {exc}")

    names = [_name(info) for info in infos]
    if not names:
        return ParseResult(status=EMPTY, parser=PARSER, note="빈 압축파일입니다.")
    shown = names[:MAX_ENTRIES]
    lines = [f"압축파일에 든 파일 {len(names):,}개"] + [f"- {name}" for name in shown]
    note = NOTE
    if len(names) > len(shown):
        note += f" 이름은 앞 {len(shown):,}개만 실었습니다."
    # 풀어 둔 폴더가 옆에 있으면 그 안의 문서는 이미 따로 읽혔다.
    if path.with_suffix("").is_dir():
        note += " 같은 이름의 풀린 폴더가 옆에 있습니다 — 그 안의 문서는 따로 읽었습니다."
    return ParseResult(
        status=PARTIAL, parser=PARSER, note=note,
        sections=[Section("paragraph", 1, "파일 목록", "\n".join(lines))],
        coverage={"version": 1, "scope": "archive_file_names", "entries_total": len(names),
                  "entries_listed": len(shown), "limitations": ["contents_not_read"]})


def _name(info: zipfile.ZipInfo) -> str:
    """한글 윈도우에서 만든 ZIP은 이름이 CP949인데 UTF-8 표시가 없다."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename
