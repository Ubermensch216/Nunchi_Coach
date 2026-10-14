"""압축파일 — 안에 든 문서의 본문까지 읽는다. 원본 옆에 풀지 않는다 (규제개혁 비교연구 R08).

전임자 폴더의 압축파일은 대개 부서가 보낸 증빙 묶음이나 제출본이다. 예전에는 이름만 읽었다.
실제 자료에서 압축 15개 안에 따로 없는 문서 104개가 있었고, 평가 제출 묶음 안에는 실제 평가
서식이 들어 있었다. 이름만으로는 "있다"까지만 알 수 있다.

지키는 것
  · **원본 옆에 풀지 않는다.** 구성원은 격리된 임시 폴더에 하나씩 꺼내 읽고 바로 지운다.
    임시 파일 이름은 순번이다 — 압축 안의 경로('../../x')가 디스크 경로가 되지 않는다.
  · **실행하지 않는다.** 실행 파일·스크립트는 꺼내지 않는다. 암호가 걸린 구성원은 풀지 않는다.
  · **한도를 넘으면 멈추고 적는다.** 깊이(압축 속 압축) 2단, 읽는 구성원 수, 구성원 하나와
    전체의 풀린 크기, 압축률(압축 폭탄)을 본다. 넘은 것은 이름만 남기고 사유를 기록한다.
  · 같은 이름의 풀린 폴더가 옆에 있으면 거기 같은 경로·크기로 있는 파일은 이미 따로 읽혔으므로
    다시 싣지 않는다(중복). 압축에만 있는 파일은 읽는다.
  · 압축 문서 하나의 조각으로 싣는다. 조각의 위치는 '구성원 경로 · 원래 위치'다. 구성원을 따로
    문서로 만들지 않으므로 업무 값(기한·금액·절차)을 읽는 쪽은 압축 문서를 받은 자료로 다룬다.
"""

from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .base import EMPTY, MAX_CHARS, OK, PARTIAL, ParseResult, Section, failed

PARSER = "archive"
MAX_ENTRIES = 500             # 이름 목록에 싣는 수
MAX_READ = 200                # 본문까지 읽는 구성원 수
MAX_MEMBER_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_RATIO = 100               # 풀린 크기 / 압축 크기
MAX_DEPTH = 2                 # 압축 속 압축까지
NOTE = "압축파일입니다. 안에 든 파일의 이름만 읽었고 내용은 읽지 않았습니다."
_EXECUTABLE = frozenset({".exe", ".dll", ".com", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse",
                         ".wsf", ".msi", ".scr", ".jar", ".lnk", ".hta", ".cpl", ".sys", ".reg"})


class _Budget:
    def __init__(self) -> None:
        self.members = 0
        self.bytes = 0
        self.chars = 0


def parse(path: Path) -> ParseResult:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            names = [_name(info) for info in infos]
            if not names:
                return ParseResult(status=EMPTY, parser=PARSER, note="빈 압축파일입니다.")
            listing = _listing(names)
            # 풀어 둔 폴더가 옆에 있으면 거기 같은 파일(같은 경로·크기)은 이미 따로 읽혔다. 그것만
            # 건너뛴다 — 압축에만 남은 파일이 있을 수 있다(실측: 풀린 폴더 옆 압축의 평가 서식).
            unpacked = path.with_suffix("")
            budget = _Budget()
            sections, skipped, read = [listing], [], 0
            todo = []
            for info, name in zip(infos, names):
                twin = unpacked / Path(*PurePosixPath(name).parts) if unpacked.is_dir() else None
                if twin is not None and _same_file(twin, unpacked, info.file_size):
                    skipped.append((name, "풀린 폴더에 같은 파일"))
                else:
                    todo.append(info)
            if todo:
                with tempfile.TemporaryDirectory(prefix="nunchi_zip_") as scratch:
                    read = _read_members(archive, todo, "", 1, Path(scratch), budget, sections, skipped)
    except zipfile.BadZipFile as exc:
        return failed(PARSER, f"ZIP 구조 손상: {exc}")

    unread = len(names) - read - sum(1 for _, reason in skipped if reason == "풀린 폴더에 같은 파일")
    note = (f"압축파일 안의 파일 {len(names):,}개 가운데 {read:,}개의 본문을 읽었습니다(원본 옆에 풀지 않음)."
            if read else NOTE)
    if len(names) > MAX_ENTRIES:
        note += f" 이름은 앞 {MAX_ENTRIES:,}개만 실었습니다."
    if skipped:
        reasons: dict[str, int] = {}
        for _, reason in skipped:
            reasons[reason] = reasons.get(reason, 0) + 1
        note += " 읽지 않은 것: " + ", ".join(f"{reason} {count}개" for reason, count in reasons.items()) + "."
    status = OK if read and not unread else PARTIAL
    return ParseResult(
        status=status, parser=PARSER, note=note,
        sections=[Section(s.kind, index, s.locator, s.text, s.meta) for index, s in enumerate(sections, start=1)],
        coverage={"version": 2, "scope": "archive_members", "entries_total": len(names),
                  "entries_listed": min(len(names), MAX_ENTRIES), "entries_read": read,
                  "skipped": [{"member": member, "reason": reason} for member, reason in skipped[:MAX_ENTRIES]],
                  "limits": {"members": MAX_READ, "member_bytes": MAX_MEMBER_BYTES,
                             "total_bytes": MAX_TOTAL_BYTES, "ratio": MAX_RATIO, "depth": MAX_DEPTH},
                  "limitations": [] if status == OK else ["some_members_not_read"]})


def _listing(names: list[str]) -> Section:
    shown = names[:MAX_ENTRIES]
    lines = [f"압축파일에 든 파일 {len(names):,}개"] + [f"- {name}" for name in shown]
    return Section("paragraph", 1, "파일 목록", "\n".join(lines))


def _same_file(twin: Path, folder: Path, size: int) -> bool:
    """풀린 폴더 안의 같은 경로에 같은 크기의 파일이 있는가. 폴더 밖을 가리키는 경로는 아니다."""
    try:
        resolved = twin.resolve()
        if folder.resolve() not in resolved.parents:
            return False
        return resolved.is_file() and resolved.stat().st_size == size
    except OSError:
        return False


def _read_members(archive: zipfile.ZipFile, infos, prefix: str, depth: int, scratch: Path,
                  budget: _Budget, sections: list[Section], skipped: list) -> int:
    from . import parse as dispatch     # 순환 가져오기를 피한다 — 디스패처가 이 모듈을 등록한다

    read = 0
    for info in infos:
        member = prefix + _name(info)
        suffix = PurePosixPath(member).suffix.lower()
        reason = _refuse(info, suffix, budget)
        if reason:
            skipped.append((member, reason))
            continue
        try:
            with archive.open(info) as handle:
                data = handle.read(MAX_MEMBER_BYTES + 1)
        except (RuntimeError, zipfile.BadZipFile, NotImplementedError, OSError) as exc:
            skipped.append((member, "풀 수 없음" if "encrypt" not in str(exc).lower() else "암호"))
            continue
        if len(data) > MAX_MEMBER_BYTES:
            skipped.append((member, "크기 상한"))
            continue
        budget.bytes += len(data)
        budget.members += 1
        if suffix == ".zip":
            if depth >= MAX_DEPTH:
                skipped.append((member, "압축 깊이 상한"))
                continue
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as inner:
                    inner_infos = [i for i in inner.infolist() if not i.is_dir()]
                    read += _read_members(inner, inner_infos, member + "/", depth + 1, scratch,
                                          budget, sections, skipped)
            except zipfile.BadZipFile:
                skipped.append((member, "ZIP 구조 손상"))
            continue
        # 순번 이름으로 꺼낸다. 압축 안의 경로가 디스크 경로가 되지 않게.
        target = scratch / f"m{budget.members:05d}{suffix}"
        target.write_bytes(data)
        try:
            result = dispatch(target)
        finally:
            target.unlink(missing_ok=True)
        if result.status not in (OK, PARTIAL) or not result.sections:
            skipped.append((member, _status_word(result.status)))
            continue
        read += 1
        for section in result.sections:
            text = section.text or ""
            if budget.chars + len(text) > MAX_CHARS:
                skipped.append((member, "글자 상한"))
                break
            budget.chars += len(text)
            meta = dict(section.meta or {})
            meta["member"] = member
            locator = f"{member} · {section.locator}" if section.locator else member
            sections.append(Section(section.kind, 0, locator, text, meta))
    return read


def _refuse(info: zipfile.ZipInfo, suffix: str, budget: _Budget) -> str:
    if suffix in _EXECUTABLE:
        return "실행 파일"
    if info.flag_bits & 0x1:
        return "암호"
    if budget.members >= MAX_READ:
        return "구성원 수 상한"
    if info.file_size > MAX_MEMBER_BYTES:
        return "크기 상한"
    if budget.bytes + info.file_size > MAX_TOTAL_BYTES:
        return "전체 크기 상한"
    if info.compress_size and info.file_size / info.compress_size > MAX_RATIO and info.file_size > 1_000_000:
        return "압축률 이상(압축 폭탄 의심)"
    return ""


def _status_word(status: str) -> str:
    return {"unsupported": "지원하지 않는 형식", "encrypted": "암호·보호", "empty": "글 없음",
            "failed": "읽기 실패", "too_large": "크기 상한"}.get(status, status)


def _name(info: zipfile.ZipInfo) -> str:
    """한글 윈도우에서 만든 ZIP은 이름이 CP949인데 UTF-8 표시가 없다."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename
