"""사용자가 입력한 분장이 기준일 때 공문 업무가 분장 항목에 맞춰진다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ai.discover import discover
from app.core import charter, gongmun_facts
from app.db import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    yield database
    database.close()


def _body(db, source, name, text):
    doc = db.con.execute(
        "INSERT INTO documents (source_id, path, filename, ext, hash, eff_year, parse_status) "
        "VALUES (?, ?, ?, '.pdf', ?, 2025, 'ok')", (source, f"D:/전임자/{name}", name, name)).lastrowid
    db.con.execute("INSERT INTO document_sections(doc_id, ordinal, kind, locator, text) VALUES (?, 0, 'paragraph', 'p1', ?)",
                   (doc, text))
    return doc


def seed(db):
    source = db.con.execute("INSERT INTO sources (path, kind) VALUES ('D:/전임자', 'local')").lastrowid
    text = ("제목 일일 상담 현황 점검\n1. 민원응대 품질 향상을 위하여 상담 현황을 점검하고자 합니다.\n"
            "2. 각 부서는 현황을 작성하여 제출하여 주시기 바랍니다.")
    docs = [_body(db, source, "[본문] 일일 상담 현황 점검 요청.pdf", text),
            _body(db, source, "[본문] 일일 상담 현황 점검 결과 보고.pdf", text)]
    gongmun_facts.analyze_bodies(db)
    return docs


def names(db):
    return [r["name"] for r in db.con.execute("SELECT name FROM tasks WHERE not_a_task = 0")]


def test_official_task_takes_user_charter_name_by_body_content(db):
    seed(db)
    charter.save_user_items(db, charter.parse_user_text("민원응대 개선\n예산 편성"))
    discover(db)
    assert "민원응대 개선" in names(db)
    assert "일일 상담 현황 점검" not in names(db)
    cmp = json.loads(db.get_meta("charter_comparison"))
    assert [m["name"] for m in cmp["matched"]] == ["민원응대 개선"]
    assert [c["name"] for c in cmp["charter_only"]] == ["예산 편성"]


def test_without_user_charter_names_stay_as_before(db):
    seed(db)
    discover(db)
    assert "일일 상담 현황 점검" in names(db)


def test_brief_shows_what_the_letter_body_says(db):
    from app.core import notebook_tasks
    seed(db)
    charter.save_user_items(db, charter.parse_user_text("민원응대 개선"))
    discover(db)
    task_id = db.con.execute("SELECT id FROM tasks WHERE name = '민원응대 개선'").fetchone()["id"]
    brief = notebook_tasks.brief_markdown(db, task_id)
    assert "공문 본문에서 읽은 내용" in brief
    assert "해야 할 일" in brief and "[본문] 일일 상담 현황 점검 요청.pdf" in brief
