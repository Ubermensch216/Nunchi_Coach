"""압축 안의 문서를 따로 문서로 다룬다 (규제개혁 비교연구 P1-2 남은 일).

압축 하나에 요청 공문, 부서가 낸 건 목록, 증빙이 함께 든다. 구성원을 문서 행으로 실어 갈래·
시점·업무·건 목록을 따로 정하게 하되, 원본은 압축이고 디스크에 따로 없다는 점을 지키는지 본다.
기관·과제 이름은 지어낸 것이다.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from openpyxl import Workbook  # noqa: E402
from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core import matters  # noqa: E402
from app.db import Database  # noqa: E402
from app.ingest import members, scanner  # noqa: E402
from app.jobs.pipeline import Pipeline  # noqa: E402

LETTER = "가나구 기획실\n제출기한 : 2025. 4. 30.\n규제개선 과제를 제출하여 주시기 바랍니다.".encode("utf-8")
HEADER = ["연번", "기초\n(시군구)", "제    목", "소관부처1"]


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QCoreApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_ollama(monkeypatch):
    from app.ai import OllamaClient
    import app.jobs.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "OllamaClient", lambda *a, **k: OllamaClient(base_url="http://127.0.0.1:1"))


def _xlsx_bytes(rows) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "건의과제"
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _inner(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buffer.getvalue()


LIST = _xlsx_bytes([HEADER, [1, "가나구", "도서관 사용허가 기한 완화", "문화체육관광부"],
                    [2, "다라시", "남의 과제", "국토교통부"]])


def _bundle(path: Path, files: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def _tree(root: Path) -> Path:
    _bundle(root / "2025 과제 제출" / "제출 묶음(가나구).zip", {
        "1. 요청 공문.txt": LETTER,
        "목록/건의과제 목록(가나구).xlsx": LIST,
        "속압축.zip": _inner({"증빙.txt": "증빙 본문입니다".encode("utf-8")}),
        "실행.exe": b"MZ",
    })
    (root / "2025 과제 제출" / "메모.txt").write_text("과제 제출 메모", encoding="utf-8")
    return root


def _run(db_path: Path, root: Path | None = None) -> Database:
    if root is not None:
        db = Database(db_path)
        db.init()
        db.add_source(root)
        db.close()
    Pipeline(db_path).run()
    return Database(db_path)


def _children(db, parent_name="제출 묶음(가나구).zip"):
    parent = db.con.execute("SELECT id FROM documents WHERE filename = ?", (parent_name,)).fetchone()["id"]
    return parent, {row["member"]: row for row in db.con.execute(
        "SELECT * FROM documents WHERE parent_id = ? ORDER BY member", (parent,))}


def _fingerprint(root: Path) -> dict:
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def test_members_become_documents_of_their_own(tmp_path):
    root = _tree(tmp_path / "자료")
    before = _fingerprint(root)
    db = _run(tmp_path / "p.db", root)
    try:
        parent, children = _children(db)
        assert set(children) == {"1. 요청 공문.txt", "목록/건의과제 목록(가나구).xlsx", "속압축.zip/증빙.txt"}
        letter = children["1. 요청 공문.txt"]
        zip_path = str(root / "2025 과제 제출" / "제출 묶음(가나구).zip")
        assert letter["path"] == zip_path + "\\1. 요청 공문.txt" and letter["filename"] == "1. 요청 공문.txt"
        assert letter["hash"] == hashlib.sha256(LETTER).hexdigest() and letter["parse_status"] == "ok"
        assert children["목록/건의과제 목록(가나구).xlsx"]["parser"] == "xlsx"
        text = " ".join(r["text"] for r in db.sections(letter["id"]))
        assert "제출기한" in text
        # 압축 자신에게는 파일 목록만 남는다 — 같은 본문이 두 번 검색되지 않게.
        own = db.sections(parent)
        assert [s["locator"] for s in own] == ["파일 목록"]
        found = db.con.execute("SELECT rowid FROM document_fts WHERE document_fts MATCH ?",
                               ('body:"본문입니다"',)).fetchall()
        assert {r[0] for r in found} == {children["속압축.zip/증빙.txt"]["id"]}
        # 시점은 구성원 자신의 것(본문의 날짜)이다.
        assert letter["eff_year"] == 2025
        # 원본 폴더는 그대로다 — 풀지 않았다.
        assert _fingerprint(root) == before
    finally:
        db.close()


def test_a_rescan_keeps_members_and_a_changed_archive_updates_them(tmp_path):
    root = _tree(tmp_path / "자료")
    db = _run(tmp_path / "p.db", root)
    try:
        parent, children = _children(db)
        kept = children["1. 요청 공문.txt"]["id"]
        db.con.execute("INSERT OR REPLACE INTO doc_roles(doc_id, role, basis, confidence, decided_by) "
                       "VALUES (?, 'directive', '사람이 정함', 'high', 'user')", (kept,))
    finally:
        db.close()
    db = _run(tmp_path / "p.db")                 # 원본이 그대로면 다시 실어도 구성원이 사라지지 않는다
    try:
        _, again = _children(db)
        assert set(again) == set(children) and all(r["missing_since"] is None for r in again.values())
    finally:
        db.close()

    # 압축을 고친다: 요청 공문은 그대로, 증빙은 빠지고, 목록은 바뀐다.
    zip_path = root / "2025 과제 제출" / "제출 묶음(가나구).zip"
    _bundle(zip_path, {"1. 요청 공문.txt": LETTER,
                       "목록/건의과제 목록(가나구).xlsx": _xlsx_bytes([HEADER, [1, "가나구", "새 과제", "행정안전부"]])})
    os.utime(zip_path, (1_800_000_000, 1_800_000_000))
    db = _run(tmp_path / "p.db")
    try:
        _, after = _children(db)
        assert after["1. 요청 공문.txt"]["id"] == kept
        assert db.con.execute("SELECT decided_by FROM doc_roles WHERE doc_id = ?", (kept,)).fetchone()[0] == "user"
        assert after["목록/건의과제 목록(가나구).xlsx"]["hash"] != children["목록/건의과제 목록(가나구).xlsx"]["hash"]
        assert after["속압축.zip/증빙.txt"]["missing_since"] is not None      # 지우지 않고 '원본 없음'
    finally:
        db.close()


def test_members_follow_the_archive_when_it_disappears_and_returns(tmp_path):
    root = _tree(tmp_path / "자료")
    db = _run(tmp_path / "p.db", root)
    zip_path = root / "2025 과제 제출" / "제출 묶음(가나구).zip"
    saved = zip_path.read_bytes()
    stamp = zip_path.stat().st_mtime
    try:
        source = db.sources()[0]["id"]
        zip_path.unlink()
        scanner.scan_source(db, source, root)
        _, gone = _children(db)
        assert all(r["missing_since"] for r in gone.values())
        zip_path.write_bytes(saved)
        os.utime(zip_path, (stamp, stamp))
        scanner.scan_source(db, source, root)
        _, back = _children(db)
        assert all(r["missing_since"] is None for r in back.values())
    finally:
        db.close()


def test_members_follow_the_task_of_their_archive_unless_a_person_decided(tmp_path):
    root = _tree(tmp_path / "자료")
    db = _run(tmp_path / "p.db", root)
    try:
        parent, children = _children(db)
        db.con.execute("DELETE FROM task_docs")
        db.con.execute("INSERT INTO tasks(id, name) VALUES (101, '과제 제출'), (102, '다른 업무')")
        db.assign_document(101, parent)
        members.inherit_tasks(db)
        assigned = {r["doc_id"] for r in db.con.execute("SELECT doc_id FROM task_docs WHERE task_id = 101")}
        assert {r["id"] for r in children.values()} <= assigned
        # 사람이 업무에서 뺀 구성원은 다시 붙이지 않는다.
        letter = children["1. 요청 공문.txt"]["id"]
        db.con.execute("DELETE FROM task_docs WHERE doc_id = ?", (letter,))
        db.con.execute("INSERT INTO corrections(target, doc_id, field, before_val, after_val) "
                       "VALUES ('task_docs', ?, 'task_id', '101', NULL)", (letter,))
        # 부모가 다른 업무로 옮기면 자동으로 붙인 구성원도 따라간다.
        db.con.execute("UPDATE task_docs SET task_id = 102 WHERE doc_id = ?", (parent,))
        members.inherit_tasks(db)
        rows = db.con.execute("SELECT task_id, doc_id FROM task_docs WHERE doc_id != ?", (parent,)).fetchall()
        assert {r["task_id"] for r in rows} == {102} and letter not in {r["doc_id"] for r in rows}
    finally:
        db.close()


def test_a_list_inside_an_archive_becomes_matters(tmp_path):
    root = _tree(tmp_path / "자료")
    db = _run(tmp_path / "p.db", root)
    try:
        matters.set_own_locality(db, "가나구")
        totals = matters.rebuild(db)
        assert totals["matters"] == 1 and totals["other_rows"] == 1
        row = db.con.execute("SELECT m.title, s.sheet, s.row, d.member FROM matters m "
                             "JOIN matter_sources s ON s.matter_id = m.id JOIN documents d ON d.id = s.doc_id").fetchone()
        assert (row["title"], row["sheet"], row["row"]) == ("도서관 사용허가 기한 완화", "건의과제", 2)
        assert row["member"] == "목록/건의과제 목록(가나구).xlsx"
    finally:
        db.close()


def test_opening_a_member_gives_a_read_only_copy_and_refuses_a_changed_archive(tmp_path):
    root = _tree(tmp_path / "자료")
    db = _run(tmp_path / "p.db", root)
    try:
        _, children = _children(db)
        letter = children["1. 요청 공문.txt"]
        zip_path = str(root / "2025 과제 제출" / "제출 묶음(가나구).zip")
        assert members.on_disk(db, letter["path"]) == zip_path
        copy, reason = members.viewing_copy(db, letter["path"])
        assert copy.read_bytes() == LETTER and copy.name == "1. 요청 공문.txt" and not reason
        assert not os.stat(copy).st_mode & stat.S_IWRITE
        assert not str(copy).startswith(str(root))                      # 원본 옆에 풀지 않는다
        nested = members.viewing_copy(db, children["속압축.zip/증빙.txt"]["path"])[0]
        assert nested.read_text(encoding="utf-8") == "증빙 본문입니다"
        _bundle(Path(zip_path), {"1. 요청 공문.txt": b"changed"})
        assert members.viewing_copy(db, letter["path"]) == (None, "압축 원본이 분석 뒤에 바뀌었습니다")
        assert not members.current(db, letter["id"])
    finally:
        db.close()
