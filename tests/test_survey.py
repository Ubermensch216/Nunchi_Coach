"""자료 지도(Survey) 단위 시험.

- 빈 폴더
- 평평한 폴더 (루트에만 파일)
- 깊은 폴더 (다단계 계층)
- 중복 해시 비율 및 비문서 비율
- 폴더 트리 문서 수 합계 = 스캔된 문서 수
- 1,000개 파일 10초 내 처리 성능 검증
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.core import survey
from app.db import Database


@pytest.fixture
def test_db(tmp_path: Path) -> Database:
    db_file = tmp_path / "project.db"
    db = Database(db_file)
    db.init()
    return db


def test_empty_folder(test_db: Database, tmp_path: Path):
    src_dir = tmp_path / "empty_src"
    src_dir.mkdir()
    src_id = test_db.add_source(src_dir)

    res = survey.build(test_db)
    assert res.total_files == 0
    assert res.total_docs == 0
    assert res.total_size == 0
    assert res.root_folders == []


def test_flat_folder(test_db: Database, tmp_path: Path):
    src_dir = tmp_path / "flat_src"
    src_dir.mkdir()
    src_id = test_db.add_source(src_dir)

    # 루트에 파일 3개 (문서 2, 비문서 1)
    f1 = src_dir / "report1.hwp"
    f2 = src_dir / "report2.xlsx"
    f3 = src_dir / "image.png"
    for f in (f1, f2, f3):
        f.write_text("dummy")

    test_db.upsert_document(src_id, {
        "path": str(f1), "filename": f1.name, "ext": ".hwp", "size": 100,
        "fs_mtime": "2023-05-01T10:00:00", "fs_ctime": "2023-05-01T10:00:00",
        "hash": "hash_a", "parse_status": "pending",
    })
    test_db.upsert_document(src_id, {
        "path": str(f2), "filename": f2.name, "ext": ".xlsx", "size": 200,
        "fs_mtime": "2024-06-01T10:00:00", "fs_ctime": "2024-06-01T10:00:00",
        "hash": "hash_b", "parse_status": "pending",
    })
    test_db.upsert_document(src_id, {
        "path": str(f3), "filename": f3.name, "ext": ".png", "size": 500,
        "fs_mtime": "2024-01-01T10:00:00", "fs_ctime": "2024-01-01T10:00:00",
        "hash": "hash_c", "parse_status": "skipped",
    })

    res = survey.build(test_db)
    assert res.total_files == 3
    assert res.total_docs == 2
    assert res.total_non_docs == 1
    assert res.total_size == 800
    assert res.duplicate_hash_ratio == 0.0
    assert len(res.root_folders) == 1
    root = res.root_folders[0]
    assert root.doc_count == 2
    assert root.file_count == 3
    assert root.years == {"2023": 1, "2024": 2}


def test_deep_folder(test_db: Database, tmp_path: Path):
    src_dir = tmp_path / "deep_src"
    src_dir.mkdir()
    src_id = test_db.add_source(src_dir)

    sub1 = src_dir / "01_기획" / "2023" / "제1차"
    sub2 = src_dir / "02_예산"
    sub1.mkdir(parents=True)
    sub2.mkdir(parents=True)

    f1 = sub1 / "계획서.hwpx"
    f2 = sub2 / "예산안.xlsx"
    f1.write_text("계획")
    f2.write_text("예산")

    test_db.upsert_document(src_id, {
        "path": str(f1), "filename": f1.name, "ext": ".hwpx", "size": 150,
        "fs_mtime": "2023-03-01T10:00:00", "fs_ctime": "2023-03-01T10:00:00",
        "hash": "hash_1", "parse_status": "pending",
    })
    test_db.upsert_document(src_id, {
        "path": str(f2), "filename": f2.name, "ext": ".xlsx", "size": 250,
        "fs_mtime": "2024-04-01T10:00:00", "fs_ctime": "2024-04-01T10:00:00",
        "hash": "hash_2", "parse_status": "pending",
    })

    res = survey.build(test_db)
    assert res.total_files == 2
    assert res.total_docs == 2
    assert len(res.root_folders) == 1
    root = res.root_folders[0]
    # root children: 01_기획, 02_예산
    child_names = [c.name for c in root.children]
    assert "01_기획" in child_names
    assert "02_예산" in child_names

    # 01_기획 폴더의 round_tags
    plan_node = next(c for c in root.children if c.name == "01_기획")
    assert "2023" in plan_node.round_tags or "제1차" in plan_node.round_tags
    assert plan_node.doc_count == 1


def test_duplicate_hash(test_db: Database, tmp_path: Path):
    src_dir = tmp_path / "dup_src"
    src_dir.mkdir()
    src_id = test_db.add_source(src_dir)

    for i in range(4):
        f = src_dir / f"file_{i}.hwp"
        f.write_text("same content")
        # 4개 파일 중 3개가 동일 해시 "same_hash", 1개가 "other_hash"
        h = "same_hash" if i < 3 else "other_hash"
        test_db.upsert_document(src_id, {
            "path": str(f), "filename": f.name, "ext": ".hwp", "size": 100,
            "fs_mtime": "2023-01-01T10:00:00", "fs_ctime": "2023-01-01T10:00:00",
            "hash": h, "parse_status": "pending",
        })

    res = survey.build(test_db)
    # 총 4개, unique 2개 -> (4-2)/4 = 0.5 (50%)
    assert res.duplicate_hash_ratio == 0.5


def test_doc_count_sum_matches_total(test_db: Database, tmp_path: Path):
    src_dir = tmp_path / "tree_src"
    src_dir.mkdir()
    src_id = test_db.add_source(src_dir)

    # 다양한 폴더에 10개 문서 생성
    for i in range(10):
        folder = src_dir / f"folder_{i % 3}"
        folder.mkdir(exist_ok=True)
        f = folder / f"doc_{i}.pdf"
        f.write_text("content")
        test_db.upsert_document(src_id, {
            "path": str(f), "filename": f.name, "ext": ".pdf", "size": 100,
            "fs_mtime": f"202{i % 4}-01-01T10:00:00", "fs_ctime": "2020-01-01T10:00:00",
            "hash": f"hash_{i}", "parse_status": "pending",
        })

    res = survey.build(test_db)
    assert res.total_docs == 10
    assert len(res.root_folders) == 1
    root = res.root_folders[0]
    assert root.doc_count == 10

    # 각 1단계 자식의 doc_count 합계 = root.doc_count
    child_sum = sum(c.doc_count for c in root.children)
    assert child_sum == 10


def test_performance_1000_files(test_db: Database, tmp_path: Path):
    src_dir = tmp_path / "perf_src"
    src_dir.mkdir()
    src_id = test_db.add_source(src_dir)

    # 1,000개 문서 등록 (DB 메타데이터만)
    for i in range(1000):
        folder_num = i % 20
        folder = src_dir / f"work_{folder_num:02d}"
        path = folder / f"doc_{i:04d}.hwp"
        test_db.upsert_document(src_id, {
            "path": str(path), "filename": path.name, "ext": ".hwp", "size": 1024,
            "fs_mtime": f"202{i % 5}-05-01T10:00:00", "fs_ctime": "2020-01-01T10:00:00",
            "hash": f"hash_{i % 200}",
            "parse_status": "pending" if i % 10 != 0 else "skipped",
        })

    t0 = time.monotonic()
    res = survey.build(test_db)
    elapsed = time.monotonic() - t0

    assert elapsed < 10.0, f"1000파일 조사가 너무 오래 걸림: {elapsed:.2f}초"
    assert res.total_files == 1000
    assert res.total_docs == 900
    assert res.total_non_docs == 100
    assert len(res.folder_weights) == 20
