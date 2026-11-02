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
from pathlib import Path

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


def folder_records(db, skip_doc_ids: set[int] | None = None) -> list:
    """폴더에 있는 '[본문]' 파일을 공문 기록으로 만든다.

    onNara 꾸러미(source_records)로 들어온 문서는 이미 기록이 있어 뺀다. 같은 공문 키를 가진
    다른 파일은 그 공문의 붙임이다. 키가 없으면 붙임은 잇지 않는다 — 이름으로 짐작하지 않는다.
    음수 id는 DB 기록이 아니라는 뜻이다.
    """
    from .official import Record

    skip = set(skip_doc_ids or ())
    rows = db.con.execute(
        "SELECT d.id, d.filename, d.hash, d.eff_year FROM documents d "
        "JOIN sources s ON s.id = d.source_id AND s.kind <> 'onnara' "
        "WHERE d.missing_since IS NULL AND d.filename LIKE '%본문%' "
        "AND d.id NOT IN (SELECT doc_id FROM source_assets WHERE doc_id IS NOT NULL) "
        "ORDER BY d.id").fetchall()
    bodies = [row for row in rows if row["id"] not in skip and is_body(row["filename"])]
    if not bodies:
        return []
    by_key: dict[str, list] = {}
    for row in db.con.execute(
            "SELECT d.id, d.filename, d.hash FROM documents d "
            "JOIN sources s ON s.id = d.source_id AND s.kind <> 'onnara' "
            "WHERE d.missing_since IS NULL ORDER BY d.id"):
        key = key_of(row["filename"])
        if key:
            by_key.setdefault(key, []).append(row)
    body_ids = {row["id"] for row in bodies}
    out = []
    for row in bodies:
        title = title_of(row["filename"])
        if not title:
            continue
        record = Record(-row["id"], title, "", str(row["eff_year"]) if row["eff_year"] else None)
        record.body_ids.append(row["id"])
        if row["hash"]:
            record.hashes.add(row["hash"])
        key = key_of(row["filename"])
        for other in by_key.get(key, ()) if key else ():
            if other["id"] in body_ids:
                continue
            record.attachment_ids.append(other["id"])
            if other["hash"]:
                record.hashes.add(other["hash"])
        out.append(record)
    return out
