"""사용자가 직접 입력한 업무 분장 — 파일에서 뽑은 분장보다 앞선 기준."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import charter
from app.db import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    yield database
    database.close()


TEXT = """1. 적극행정 실행계획 (30%)
  - 계획 수립
  - 결과 보고
예산 편성: 요구서 작성, 부서협의
○ 민원 점검
"""


def test_parse_user_text():
    items = charter.parse_user_text(TEXT)
    assert [i.name for i in items] == ["적극행정 실행계획", "예산 편성", "민원 점검"]
    assert items[0].weight == 30.0 and items[0].details == ["계획 수립", "결과 보고"]
    assert items[1].details == ["요구서 작성", "부서협의"]


def test_save_roundtrip_and_replace(db):
    assert charter.save_user_items(db, charter.parse_user_text(TEXT)) == 3
    assert [r["name"] for r in charter.current_items(db)] == ["적극행정 실행계획", "예산 편성", "민원 점검"]
    again = charter.parse_user_text(charter.user_text(db))
    assert [i.name for i in again] == ["적극행정 실행계획", "예산 편성", "민원 점검"]
    charter.save_user_items(db, charter.parse_user_text("인사 업무"))
    assert [r["name"] for r in charter.current_items(db)] == ["인사 업무"]
    charter.save_user_items(db, [])
    assert charter.current_items(db) == []


def test_user_input_beats_file_charter(db):
    source = db.con.execute("INSERT INTO sources (path) VALUES ('D:/x')").lastrowid
    doc = db.con.execute(
        "INSERT INTO documents (source_id, path, filename, ext) VALUES (?, 'D:/x/분장.xlsx', '분장.xlsx', '.xlsx')",
        (source,)).lastrowid
    charter.save_charter_items(db, doc, [charter.ExtractedItem(name="파일 속 업무")])
    assert [r["name"] for r in charter.current_items(db)] == ["파일 속 업무"]
    charter.save_user_items(db, charter.parse_user_text("내가 적은 업무"))
    assert [r["name"] for r in charter.current_items(db)] == ["내가 적은 업무"]
    found = charter.items_for_task(db, "내가 적은 업무")
    assert found and found[0]["document"] == charter.USER_SOURCE_LABEL
    # 입력을 지우면 파일 분장으로 돌아간다.
    charter.save_user_items(db, [])
    assert [r["name"] for r in charter.current_items(db)] == ["파일 속 업무"]
