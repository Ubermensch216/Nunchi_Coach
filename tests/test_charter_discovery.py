"""분장 ↔ 자료 대조 및 업무 발견 통합 시험 (S3).

- 분장 6건 · 폴더 5개 · 분장 밖 폴더 1개 합성 픽스처
- 세 갈래 정확히 분류:
  - matched: 4건 (분장과 폴더 자료 모두 있음)
  - charter_only: 2건 (분장에는 있으나 폴더 자료 없음)
  - data_only: 1건 (폴더 자료는 있으나 분장 밖)
- 분장 이름을 대표 이름으로 채택 확인
- DB 메타 charter_comparison 대조 결과 확인
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ai.discover import discover
from app.core import charter
from app.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    db_file = tmp_path / "project.db"
    database = Database(db_file)
    database.init()
    src_id = database.add_source(tmp_path)

    # 1. 분장표 문서 등록 및 분장 항목 6건 생성
    # 분장 1: 예산편성 (폴더 있음)
    # 분장 2: 규제개혁 (폴더 있음)
    # 분장 3: 의회대응 (폴더 있음)
    # 분장 4: 청사관리 (폴더 있음)
    # 분장 5: 미래전략기획 (자료 없음 -> charter_only)
    # 분장 6: 대외통상협력 (자료 없음 -> charter_only)
    cf = tmp_path / "업무분장표.xlsx"
    cf.write_text("dummy")
    c_doc_id = database.upsert_document(src_id, {
        "path": str(cf), "filename": cf.name, "ext": ".xlsx",
        "hash": "hash_cf", "parse_status": "ok",
    })

    charter_items = [
        charter.ExtractedItem(name="예산편성", details=["본예산", "추경예산"], owner="홍길동", locator="row:2"),
        charter.ExtractedItem(name="규제개혁", details=["규제입증", "규제샌드박스"], owner="홍길동", locator="row:3"),
        charter.ExtractedItem(name="의회대응", details=["행정사무감사", "시정질문"], owner="홍길동", locator="row:4"),
        charter.ExtractedItem(name="청사관리", details=["시설안전", "방역"], owner="홍길동", locator="row:5"),
        charter.ExtractedItem(name="미래전략기획", details=["신산업발굴"], owner="홍길동", locator="row:6"),
        charter.ExtractedItem(name="대외통상협력", details=["해외진출"], owner="홍길동", locator="row:7"),
    ]
    charter.save_charter_items(database, c_doc_id, charter_items)

    # 2. 폴더 5개 생성 (각 폴더에 문서 2건 이상)
    # 매칭 폴더 4개:
    # 폴더 1: 01_예산편성 (2건) -> 분장 1과 매칭
    # 폴더 2: 02_규제개혁추진 (2건) -> 분장 2와 매칭 (폴더명 알맹이 매칭)
    # 폴더 3: 03_의회대응업무 (2건) -> 분장 3과 매칭
    # 폴더 4: 04_청사시설관리 (2건) -> 분장 4와 매칭
    # 분장 밖 폴더 1개:
    # 폴더 5: 05_수질통계관리 (2건) -> data_only (분장 밖 업무)

    folder_specs = [
        ("01_예산편성", "예산안"),
        ("02_규제개혁추진", "규제개혁"),
        ("03_의회대응업무", "의회자료"),
        ("04_청사시설관리", "청사방역"),
        ("05_수질통계관리", "수질통계"),
    ]

    for fld_name, file_prefix in folder_specs:
        folder = tmp_path / fld_name
        folder.mkdir(parents=True, exist_ok=True)
        for i in range(1, 3):
            file_path = folder / f"{file_prefix}_{i}.hwp"
            file_path.write_text(f"{fld_name} 내용 {i}")
            database.upsert_document(src_id, {
                "path": str(file_path), "filename": file_path.name, "ext": ".hwp",
                "hash": f"hash_{fld_name}_{i}", "parse_status": "ok",
                "eff_year": 2024,
            })

    return database


def test_charter_discovery_three_branches(db: Database):
    result = discover(db)

    # 세 갈래 건수 확인
    assert result.charter_matched == 4, f"matched는 4건이어야 함: {result.charter_matched}"
    assert result.charter_only == 2, f"charter_only는 2건이어야 함: {result.charter_only}"
    assert result.data_only == 1, f"data_only는 1건이어야 함: {result.data_only}"

    # DB에 저장된 charter_comparison 메타 확인
    raw_comp = db.get_meta("charter_comparison")
    assert raw_comp is not None
    comp = json.loads(raw_comp)

    assert {item["name"] for item in comp["matched"]} == {"예산편성", "규제개혁", "의회대응", "청사관리"}
    assert {item["name"] for item in comp["charter_only"]} == {"미래전략기획", "대외통상협력"}
    assert any("수질통계" in item["name"] and item["doc_count"] == 2 for item in comp["data_only"])

    # 분장 이름을 대표 이름으로 채택 확인 (폴더명 '02_규제개혁추진' 대신 분장명 '규제개혁')
    tasks = db.tasks()
    task_names = [t["name"] for t in tasks]
    assert "규제개혁" in task_names

    # summary 메시지에 세 갈래 포함 확인
    assert "분장 일치 4개" in result.summary()
    assert "분장 전용 2개" in result.summary()
    assert "분장 밖 후보 1개" in result.summary()


def test_comparison_covers_tasks_from_any_discovery_path_and_ranks_by_volume(tmp_path: Path):
    """본문 군집 등 폴더 계획 밖에서 세운 업무도 견주고, 분장 밖 후보는 자료가 많은 순이다."""
    from app.ai.discover import Discovery, _compare_charter

    database = Database(tmp_path / "p.db")
    database.init()
    source = database.add_source(tmp_path)
    doc = database.upsert_document(source, {
        "path": str(tmp_path / "업무분장표.xlsx"), "filename": "업무분장표.xlsx", "ext": ".xlsx",
        "hash": "c", "parse_status": "ok"})
    charter.save_charter_items(database, doc, [
        charter.ExtractedItem(name="예산편성", weight=30),
        charter.ExtractedItem(name="미래전략기획", weight=10),
        charter.ExtractedItem(name="대외통상협력", weight=50),
    ])
    for name, count in (("예산편성 업무", 2), ("수질 통계", 1), ("민원 응대", 3)):
        task = database.con.execute(
            "INSERT INTO tasks(name, origin, status) VALUES (?, 'ai', 'proposed')", (name,)).lastrowid
        for index in range(count):
            doc_id = database.upsert_document(source, {
                "path": str(tmp_path / f"{name}{index}.hwp"), "filename": f"{name}{index}.hwp",
                "ext": ".hwp", "hash": f"{name}{index}", "parse_status": "ok"})
            database.con.execute("INSERT INTO task_docs(task_id, doc_id, origin) VALUES (?, ?, 'ai')",
                                 (task, doc_id))
    result = Discovery()
    _compare_charter(database, result)
    comp = json.loads(database.get_meta("charter_comparison"))

    assert comp["matched"] == [{"name": "예산편성", "doc_count": 2}]
    assert [item["name"] for item in comp["charter_only"]] == ["대외통상협력", "미래전략기획"]
    assert comp["data_only"] == [{"name": "민원 응대", "doc_count": 3}, {"name": "수질 통계", "doc_count": 1}]
    assert (result.charter_matched, result.charter_only, result.data_only) == (1, 2, 2)


def test_comparison_is_cleared_when_the_charter_disappears(db: Database):
    discover(db)
    assert db.get_meta("charter_comparison")
    db.con.execute("DELETE FROM charter_items")
    discover(db)
    assert not db.get_meta("charter_comparison")
