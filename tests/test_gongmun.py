"""전임자 폴더의 '[본문]' 표시와 공문 키 읽기."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import docrole, gongmun
from app.core.clustering import _tokenize
from app.core.official import records
from app.db import Database


@pytest.mark.parametrize("name, body, title", [
    ("[본문] 2025년 적극행정 실행계획 제출 요청.pdf", True, "2025년 적극행정 실행계획 제출 요청"),
    ("0123456789ab-[본문] 민원 점검.hwp", True, "민원 점검"),
    ("［본문］민원 점검.pdf", True, "민원 점검"),
    ("본문 참고자료.hwp", False, "본문 참고자료"),
    ("붙임1 계획서.hwp", False, "붙임1 계획서"),
])
def test_marker_and_title(name, body, title):
    assert gongmun.is_body(name) is body
    assert gongmun.title_of(name) == title


def test_marker_and_key_are_not_task_words():
    assert "본문" not in _tokenize("[본문] 적극행정 실행계획.pdf")
    assert "0123456789ab" not in _tokenize("0123456789ab-적극행정 실행계획.hwp")


def test_body_marker_gives_directive_role():
    assert docrole.classify("[본문] 자료.pdf").role == docrole.DIRECTIVE


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    yield database
    database.close()


def _doc(db, source, name, digest):
    return db.con.execute(
        "INSERT INTO documents (source_id, path, filename, ext, hash, size, fs_mtime, parse_status) "
        "VALUES (?,?,?,?,?,?,?, 'parsed')",
        (source, rf"D:\전임자\{name}", name, Path(name).suffix, digest, 1, "")).lastrowid


def test_folder_body_becomes_record_with_same_key_attachments(db):
    source = db.con.execute("INSERT INTO sources (path, kind) VALUES ('D:\전임자', 'local')").lastrowid
    body = _doc(db, source, "0123456789ab-[본문] 적극행정 실행계획 제출 요청.pdf", "h1")
    attach = _doc(db, source, "0123456789ab-붙임1 실행계획서.hwp", "h2")
    _doc(db, source, "ffffffffffff-붙임 다른공문.hwp", "h3")
    found = records(db)
    assert len(found) == 1
    assert found[0].title == "적극행정 실행계획 제출 요청"
    assert found[0].body_ids == [body] and found[0].attachment_ids == [attach]
