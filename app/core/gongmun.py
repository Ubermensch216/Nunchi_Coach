"""공문 본문 표시 — 전임자 폴더의 파일 이름이 말해 주는 것.

onNara.sAIde가 문서등록대장에서 내려받은 자료는 공문 본문과 붙임이다. 본문 파일에는 제목 앞에
'[본문]' 표시를 달아 둔다. 같은 공문의 파일에는 '<12자리 공문 키>-' 머리가 붙기도 한다.
이 모듈은 이름에서 그 표시를 읽는다. DB를 고치지 않는다 — 읽기만 한다.

지키는 것
  · 표시가 이름 맨 앞(공문 키 다음)에 있을 때만 본문으로 본다. '본문 참고자료'는 본문이 아니다.
  · 제목은 표시·키·확장자를 뗀 나머지 그대로다. 없는 제목을 지어내지 않는다.
"""

from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath

KEY = re.compile(r"^([0-9a-f]{12})[-_ ]")
_MARK = re.compile(r"^\s*[\[［【(（]\s*본문\s*[\]］】)）]\s*")


def key_of(filename: str) -> str:
    """같은 공문의 파일이 함께 가진 12자리 키. 없으면 빈 값."""
    found = KEY.match(Path(filename or "").name)
    return found.group(1) if found else ""


def _without_key(stem: str) -> str:
    return KEY.sub("", stem, count=1)


def is_body(filename: str) -> bool:
    """제목 앞에 '[본문]' 표시가 있는 파일인가."""
    return bool(_MARK.match(_without_key(Path(filename or "").stem)))


def title_of(filename: str) -> str:
    """표시·공문 키·확장자를 뗀 제목."""
    stem = _MARK.sub("", _without_key(Path(filename or "").stem), count=1)
    return stem.strip(" -_·")


def strip_marks(stem: str) -> str:
    """업무 이름 후보 낱말을 뽑기 전에 표시와 키를 뗀다."""
    return _MARK.sub("", _without_key(stem), count=1)


# 공문 한 건을 담은 폴더. onNara.sAIde는 '<YYYYMMDD>_<공문 제목>' 이름의 폴더에 본문과 붙임을 함께 둔다.
_LETTER_FOLDER = re.compile(r"^((?:19|20)\d{2})(\d{2})(\d{2})[_\s-]+(.+)$")


def letter_folder(name: str) -> tuple[str, str] | None:
    """폴더 이름이 공문 한 건의 폴더면 (작성일 YYYY-MM-DD, 제목). 아니면 None."""
    found = _LETTER_FOLDER.match((name or "").strip())
    if not found:
        return None
    year, month, day, title = found.groups()
    if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
        return None
    return f"{year}-{month}-{day}", title.strip()


def _parent(path: str) -> str:
    return str(PureWindowsPath(path).parent)


def folder_records(db, skip_doc_ids: set[int] | None = None) -> list:
    """전임자 폴더의 공문을 공문 기록으로 만든다. 음수 id는 DB 기록이 아니라는 뜻이다.

    공문 한 건을 이루는 파일은 셋 중 하나로 안다(앞의 것이 먼저다).
      1. '<YYYYMMDD>_제목' 폴더 — 그 폴더(와 하위 폴더)의 모든 파일이 공문 한 건이다.
         '[본문]' 파일이 있으면 본문, 나머지는 붙임이다.
      2. 같은 공문 키('<12자리>-')를 가진 파일 — '[본문]' 파일의 붙임이다.
      3. '[본문]' 파일이 하나뿐인 폴더 — 같은 폴더의 다른 파일이 붙임이다.
    onNara 꾸러미(source_records)로 들어온 문서는 이미 기록이 있어 뺀다.
    """
    from .official import Record

    skip = set(skip_doc_ids or ())
    rows = [row for row in db.con.execute(
        "SELECT d.id, d.path, d.filename, d.hash, d.eff_year FROM documents d "
        "JOIN sources s ON s.id = d.source_id AND s.kind NOT IN ('onnara', 'upload') "
        "WHERE d.missing_since IS NULL "
        "AND d.id NOT IN (SELECT doc_id FROM source_assets WHERE doc_id IS NOT NULL) "
        "ORDER BY d.id") if row["id"] not in skip]
    if not rows:
        return []
    out = []
    taken: set[int] = set()

    def record_of(doc_rows: list, title: str, when: str | None) -> None:
        bodies = [row for row in doc_rows if is_body(row["filename"])]
        first = bodies[0] if bodies else doc_rows[0]
        years = [row["eff_year"] for row in doc_rows if row["eff_year"]]
        record = Record(-first["id"], title, "", when or (str(years[0]) if years else None))
        for row in doc_rows:
            (record.body_ids if row in bodies[:1] else record.attachment_ids).append(row["id"])
            if row["hash"]:
                record.hashes.add(row["hash"])
            taken.add(row["id"])
        out.append(record)

    # 1. 공문 폴더: 경로의 폴더 이름 가운데 가장 깊은 공문 폴더에 묶는다.
    by_letter: dict[str, list] = {}
    letter_meta: dict[str, tuple[str, str]] = {}
    for row in rows:
        folder = PureWindowsPath(row["path"]).parent
        for candidate in (folder, *folder.parents):
            meta = letter_folder(candidate.name)
            if meta:
                by_letter.setdefault(str(candidate), []).append(row)
                letter_meta[str(candidate)] = meta
                break
    for folder, doc_rows in by_letter.items():
        when, folder_title = letter_meta[folder]
        bodies = [row for row in doc_rows if is_body(row["filename"])]
        record_of(doc_rows, title_of(bodies[0]["filename"]) if bodies else folder_title, when)

    rest = [row for row in rows if row["id"] not in taken]
    bodies = [row for row in rest if is_body(row["filename"])]
    by_key: dict[str, list] = {}
    by_dir: dict[str, list] = {}
    for row in rest:
        key = key_of(row["filename"])
        if key:
            by_key.setdefault(key, []).append(row)
        by_dir.setdefault(_parent(row["path"]), []).append(row)
    for body in bodies:
        title = title_of(body["filename"])
        if not title or body["id"] in taken:
            continue
        key = key_of(body["filename"])
        siblings = by_dir.get(_parent(body["path"]), [])
        if key:
            members = [row for row in by_key.get(key, []) if row is body or not is_body(row["filename"])]
        elif sum(1 for row in siblings if is_body(row["filename"])) == 1:
            members = siblings
        else:
            members = [body]
        members = [body] + [row for row in members if row is not body and row["id"] not in taken]
        record_of(members, title, None)
    return out
