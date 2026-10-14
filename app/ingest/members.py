"""압축 안의 문서를 따로 문서로 다룬다 (규제개혁 비교연구 P1-2 남은 일).

압축 하나에 부서가 보낸 증빙 묶음, 지난 회차 제출본, 평가 서식이 함께 든다. 압축 문서 하나의
조각으로만 실으면 구성원마다 다른 갈래(공문·서식·목록), 시점, 건 목록을 가를 수 없다. 그래서
구성원을 문서 행으로 싣는다.

  · **디스크에 따로 없다.** 경로는 '압축 경로\\압축 안 경로'로 짓지만 그 파일은 없다. 자료원 훑기는
    구성원을 보지 않고(부모를 따른다), 다시 읽기는 부모 압축을 다시 읽어서 한다.
  · **원본은 압축이다.** 구성원을 열거나 다시 읽을 때는 압축 원본의 해시가 분석한 판과 같은지,
    꺼낸 구성원의 해시가 기록과 같은지 확인한다. 꺼낸 파일은 앱의 임시 폴더에만 둔다(원본 옆에
    풀지 않는다). 열람용 사본은 읽기 전용으로 둔다 — 고쳐도 원본에 반영되지 않는다.
  · **업무는 부모를 따른다.** 업무 찾기(폴더·본문 묶기)에 끼지 않는다. 압축 이름이 업무 폴더로
    잡히지 않게 하려는 것이다. 부모 압축이 붙은 업무에 함께 붙고, 사람이 고친 배정은 그대로 둔다.
  · 같은 구성원(해시가 같음)은 다시 실을 때 분석 결과를 지우지 않는다. 압축에서 빠진 구성원은
    지우지 않고 '원본 없음'으로 둔다.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

ORIGIN_NOTE = "압축파일 안의 문서"
_VIEW_DIR = "nunchi_members"


def child_path(parent_path: str, member: str) -> str:
    return parent_path + "\\" + member.replace("/", "\\")


def is_member(db, doc_id: int) -> bool:
    row = db.con.execute("SELECT parent_id FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return bool(row and row["parent_id"])


# ── 싣기 ────────────────────────────────────────────────────────────

def explode(db, parent: dict, result) -> dict[str, int]:
    """압축 문서를 읽은 결과의 구성원을 문서로 싣는다. 부모를 쓰는 묶음 안에서 부른다.

    parent는 id·path·source_id를 가진 문서 행이다. 돌려주는 값: added·updated·kept·gone.
    """
    counts = {"added": 0, "updated": 0, "kept": 0, "gone": 0}
    existing = {row["member"]: row for row in db.con.execute(
        "SELECT id, member, hash, parse_status, parser_version, missing_since FROM documents WHERE parent_id = ?",
        (parent["id"],))}
    seen = set()
    for member in result.members if result.ok else []:
        seen.add(member.name)
        sub = member.result
        old = existing.get(member.name)
        if old is not None and old["hash"] == member.digest and old["parse_status"] == sub.status \
                and old["parser_version"] == sub.parser_version:
            # 같은 구성원을 같은 판으로 읽었다 — 분석 결과(시점·갈래·업무)를 지우지 않는다.
            if old["missing_since"]:
                db.con.execute("UPDATE documents SET missing_since = NULL WHERE id = ?", (old["id"],))
            counts["kept"] += 1
            continue
        if old is not None:
            db.invalidate_document_analysis(old["id"])
        name = PurePosixPath(member.name).name
        fields = {
            "path": child_path(parent["path"], member.name), "filename": name,
            "ext": PurePosixPath(name).suffix.lower(), "size": member.size, "fs_mtime": member.modified,
            "hash": member.digest, "parse_status": sub.status, "parse_error": sub.error, "parse_note": sub.note,
            "parser": sub.parser, "parser_version": sub.parser_version, "char_count": sub.char_count,
            "author": sub.meta.author, "doc_title": sub.meta.title, "meta_created": sub.meta.created,
            "meta_modified": sub.meta.modified,
            "extraction_coverage": json.dumps(sub.coverage, ensure_ascii=False),
            "parent_id": parent["id"], "member": member.name,
        }
        doc_id = db.upsert_document(parent["source_id"], fields)
        if sub.ok:
            db.replace_sections(doc_id, [(s.kind, s.ordinal, s.locator, s.text, s.meta) for s in sub.sections])
            db.index_document(doc_id, name, fields["path"], sub.meta.author or "", sub.text)
        else:
            db.replace_sections(doc_id, [])
        counts["updated" if old is not None else "added"] += 1
    gone = [row["id"] for key, row in existing.items() if key not in seen and row["missing_since"] is None]
    if gone:
        marks = ", ".join("?" * len(gone))
        db.con.execute(f"UPDATE documents SET missing_since = datetime('now') WHERE id IN ({marks})", gone)
    counts["gone"] = len(gone)
    return counts


def refresh_outdated(db) -> int:
    """구성원을 읽은 파서의 판이 바뀌었으면 그것이 든 압축을 다시 읽게 한다. 다시 읽게 한 압축 수."""
    from .parsers.base import outdated_clause

    parents = [row[0] for row in db.con.execute(
        f"SELECT DISTINCT c.parent_id FROM documents c WHERE c.parent_id IS NOT NULL "
        f"AND c.missing_since IS NULL AND c.parse_status IN ('ok', 'partial') AND {outdated_clause('c')}")]
    for parent in parents:
        db.con.execute("UPDATE documents SET parse_status = 'pending' WHERE id = ? AND missing_since IS NULL",
                       (parent,))
    return len(parents)


def parent_sections(result) -> list:
    """압축 문서 자신에게 남길 조각 — 파일 목록. 구성원의 본문은 구성원 문서가 가진다."""
    return [s for s in result.sections if not (s.meta or {}).get("member")]


def inherit_tasks(db) -> int:
    """구성원을 부모 압축이 붙은 업무에 붙인다. 사람이 정한 배정은 건드리지 않는다.

    사람이 업무에서 뺀 구성원(corrections), 사람이 직접 업무를 정한 구성원은 다시 붙이지 않는다.
    부모가 업무를 옮기면 자동으로 붙인 배정도 따라 옮긴다.
    """
    decided = {row[0] for row in db.con.execute(
        "SELECT doc_id FROM task_docs WHERE origin = 'user' UNION "
        "SELECT doc_id FROM corrections WHERE target = 'task_docs' AND doc_id IS NOT NULL")}
    rows = db.con.execute(
        "SELECT c.id AS child, p.filename, td.task_id, td.confidence, td.round_key "
        "FROM documents c JOIN documents p ON p.id = c.parent_id JOIN task_docs td ON td.doc_id = p.id "
        "WHERE c.missing_since IS NULL").fetchall()
    wanted: dict[int, set] = {}
    changed = 0
    for row in rows:
        if row["child"] in decided:
            continue
        wanted.setdefault(row["child"], set()).add(row["task_id"])
        changed += db.con.execute(
            "INSERT OR IGNORE INTO task_docs(task_id, doc_id, origin, confidence, evidence, round_key) "
            "VALUES (?, ?, 'ai', ?, ?, ?)",
            (row["task_id"], row["child"], row["confidence"], f"{ORIGIN_NOTE}: {row['filename']}",
             row["round_key"])).rowcount
    for row in db.con.execute(
            "SELECT td.task_id, td.doc_id FROM task_docs td JOIN documents c ON c.id = td.doc_id "
            "WHERE c.parent_id IS NOT NULL AND td.origin = 'ai'").fetchall():
        if row["doc_id"] in decided or row["task_id"] in wanted.get(row["doc_id"], set()):
            continue
        db.con.execute("DELETE FROM task_docs WHERE task_id = ? AND doc_id = ?", (row["task_id"], row["doc_id"]))
        changed += 1
    return changed


# ── 꺼내기 ──────────────────────────────────────────────────────────

def _file_digest(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError:
        return None


def _read_member(archive: zipfile.ZipFile, name: str, depth: int = 0) -> bytes | None:
    """압축 안 경로로 구성원을 읽는다. 압축 속 압축('a/안.zip/b.hwp')도 따라간다."""
    from .parsers.archive import MAX_DEPTH, MAX_MEMBER_BYTES, _name

    infos = {_name(info): info for info in archive.infolist() if not info.is_dir()}
    if name in infos:
        info = infos[name]
        if info.flag_bits & 0x1 or info.file_size > MAX_MEMBER_BYTES:
            return None
        with archive.open(info) as handle:
            return handle.read(MAX_MEMBER_BYTES + 1)
    if depth + 1 >= MAX_DEPTH:
        return None
    parts = name.split("/")
    for cut in range(len(parts) - 1, 0, -1):
        inner = "/".join(parts[:cut])
        if inner.lower().endswith(".zip") and inner in infos:
            with archive.open(infos[inner]) as handle:
                data = handle.read(MAX_MEMBER_BYTES + 1)
            with zipfile.ZipFile(io.BytesIO(data)) as nested:
                return _read_member(nested, "/".join(parts[cut:]), depth + 1)
    return None


def member_bytes(db, doc_id: int) -> tuple[bytes | None, str]:
    """구성원의 내용. 압축 원본이 분석한 판과 같고 꺼낸 내용의 해시가 기록과 같을 때만.

    돌려주는 값: (내용, 못 꺼낸 까닭).
    """
    row = db.con.execute(
        "SELECT c.member, c.hash, p.path AS parent_path, p.hash AS parent_hash FROM documents c "
        "JOIN documents p ON p.id = c.parent_id WHERE c.id = ?", (doc_id,)).fetchone()
    if row is None:
        return None, "압축 안의 문서가 아닙니다"
    digest = _file_digest(row["parent_path"])
    if digest is None:
        return None, "압축 원본을 열 수 없습니다"
    if row["parent_hash"] and digest != row["parent_hash"]:
        return None, "압축 원본이 분석 뒤에 바뀌었습니다"
    try:
        with zipfile.ZipFile(row["parent_path"]) as archive:
            data = _read_member(archive, row["member"])
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
        return None, "압축을 풀 수 없습니다"
    if data is None:
        return None, "압축 안에서 찾지 못했습니다"
    if row["hash"] and hashlib.sha256(data).hexdigest() != row["hash"]:
        return None, "구성원이 분석한 판과 다릅니다"
    return data, ""


@contextmanager
def extracted(db, doc_id: int):
    """구성원을 격리된 임시 폴더에 꺼내 경로를 준다(못 꺼내면 None). 나오면 지운다."""
    data, _ = member_bytes(db, doc_id)
    if data is None:
        yield None
        return
    suffix = PurePosixPath(db.con.execute("SELECT member FROM documents WHERE id = ?",
                                          (doc_id,)).fetchone()["member"]).suffix
    with tempfile.TemporaryDirectory(prefix="nunchi_member_") as scratch:
        # 순번 이름 — 압축 안의 경로가 디스크 경로가 되지 않게.
        target = Path(scratch) / f"member{suffix.lower()}"
        target.write_bytes(data)
        yield target


def viewing_copy(db, path: str) -> tuple[Path | None, str]:
    """사용자가 열 구성원의 읽기 전용 사본. 앱의 임시 폴더에 원래 파일 이름으로 둔다."""
    row = db.con.execute("SELECT id, filename, hash FROM documents WHERE path = ? AND parent_id IS NOT NULL",
                         (path,)).fetchone()
    if row is None:
        return None, "압축 안의 문서가 아닙니다"
    data, reason = member_bytes(db, row["id"])
    if data is None:
        return None, reason
    folder = Path(tempfile.gettempdir()) / _VIEW_DIR / (row["hash"] or str(row["id"]))[:16]
    folder.mkdir(parents=True, exist_ok=True)
    # 파일 이름의 경로 구분자는 버린다 — 이름만 쓴다.
    target = folder / Path(row["filename"].replace("/", "_").replace("\\", "_")).name
    if target.exists():
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
    target.write_bytes(data)
    os.chmod(target, stat.S_IREAD)
    return target, ""


def on_disk(db, path: str) -> str:
    """사용자에게 보일 디스크 위치. 구성원이면 그것이 든 압축 파일."""
    row = db.con.execute("SELECT p.path FROM documents c JOIN documents p ON p.id = c.parent_id "
                         "WHERE c.path = ?", (path,)).fetchone()
    return row["path"] if row else path


def current(db, doc_id: int) -> bool:
    """구성원의 근거가 지금 원본과 같은가 — 부모 압축이 분석한 판 그대로인가."""
    row = db.con.execute("SELECT p.path, p.hash FROM documents c JOIN documents p ON p.id = c.parent_id "
                         "WHERE c.id = ?", (doc_id,)).fetchone()
    return bool(row and row["hash"] and _file_digest(row["path"]) == row["hash"])

