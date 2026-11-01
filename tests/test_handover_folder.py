"""사본 인계 폴더 만들기 시험 (P4b).

확인할 것:
  1. <출력 폴더>/<업무>/<연도>/공문/ 과 PC자료/ 아래에 사본이 복사되는가
  2. 원본 자료 폴더의 파일이 수정·이동·삭제되지 않는가 (원본 읽기 전용)
  3. 원본 자료 폴더 내부를 출력 경로로 지정하면 UnsafeOutputError로 차단하는가
  4. 00_인계_안내.md 에 미분류 파일 위치가 적히고, <업무>/00_업무안내.md 와 manifest.json 에
     SHA-256 해시·배정 근거·확신도가 남는가
"""

from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ai.discover import discover  # noqa: E402
from app.core import handover_folder  # noqa: E402
from app.core.output_policy import UnsafeOutputError  # noqa: E402
from app.db import Database  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.views.handover import HandoverView  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet())
    yield app


@pytest.fixture
def workspace(tmp_path: Path):
    db_path = tmp_path / "project" / "nunchi.db"
    db_path.parent.mkdir(parents=True)
    db = Database(db_path)
    db.init()

    onnara_root = tmp_path / "onnara_store"
    local_root = tmp_path / "pc_source"
    onnara_root.mkdir()
    local_root.mkdir()

    onnara_src = db.add_source(onnara_root, kind="onnara")
    local_src = db.add_source(local_root)

    db.con.execute(
        "INSERT INTO collection_runs(collection_key, adapter, institution, system_id, scope)"
        " VALUES ('k1', 'saide-package-v1', 'onnara.invalid', 'onnara@onnara.invalid', '{}')"
    )
    run_id = db.con.execute("SELECT id FROM collection_runs").fetchone()["id"]

    def make_doc(src_id: int, rel: str, content: str, digest: str, year: int = 2025) -> tuple[int, Path]:
        base = onnara_root if src_id == onnara_src else local_root
        file_path = base / rel
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        doc_id = db.upsert_document(src_id, {
            "path": str(file_path),
            "filename": file_path.name,
            "ext": file_path.suffix,
            "parse_status": "ok",
            "hash": digest,
            "char_count": len(content),
            "eff_year": year,
            "eff_date": f"{year}-03-10",
            "eff_date_kind": "body",
        })
        db.con.execute(
            "INSERT INTO chunks(doc_id, ordinal, locator, text) VALUES (?, 0, '1쪽', ?)",
            (doc_id, content),
        )
        return doc_id, file_path

    # 공문 2건 (2024, 2025)
    for key, yr, num in (("doc-2024", 2024, "혁신과-101"), ("doc-2025", 2025, "혁신과-202")):
        cur = db.con.execute(
            "INSERT INTO source_records(run_id, institution, system_id, document_id, title, document_number,"
            " authored_at, task_card) VALUES (?, 'onnara.invalid', 'onnara@onnara.invalid', ?, ?, ?, ?, '적극행정')",
            (run_id, key, f"{yr}년 적극행정 실행계획 수립 알림", num, f"{yr}-03-10"),
        )
        rec_id = cur.lastrowid
        doc_id, _ = make_doc(onnara_src, f"{key}/본문.pdf", f"{yr}년 적극행정 공문 본문", f"hash-{key}", year=yr)
        db.con.execute(
            "INSERT INTO source_assets(record_id, asset_id, role, status, doc_id, sha256)"
            " VALUES (?, 'body', 'body', 'downloaded', ?, ?)",
            (rec_id, doc_id, f"hash-{key}"),
        )

    # PC 대응 파일 1건 + 미분류 파일 1건
    _, local_matched = make_doc(
        local_src,
        "업무폴더/혁신과-202_실행계획_초안.hwp",
        "2025년 적극행정 실행계획 초안 내용",
        "hash-local-draft",
        year=2025,
    )
    _, local_unclassified = make_doc(
        local_src,
        "기타/동호회_회비_메모.txt",
        "점심 모임 회비 정산 메모",
        "hash-unclassified",
        year=2025,
    )

    discover(db, client=None)
    yield db, onnara_root, local_root, local_matched, local_unclassified, tmp_path
    db.close()


def test_export_handover_folder_organizes_official_and_local_copies_by_task_and_year(workspace):
    db, onnara_root, local_root, local_matched, local_unclassified, tmp_path = workspace
    out_dir = tmp_path / "handover_out"

    before_local = {p: p.read_bytes() for p in local_root.rglob("*") if p.is_file()}
    before_onnara = {p: p.read_bytes() for p in onnara_root.rglob("*") if p.is_file()}

    res = handover_folder.export_handover_folder(
        db, out_dir, project_name="기획예산과", today=date(2026, 10, 30)
    )

    assert res.tasks_exported == 1
    assert res.files_copied == 3
    assert res.official_copied == 2
    assert res.local_copied == 1
    assert res.unclassified_count == 1

    # 폴더 구조 확인: <출력>/<업무>/<연도>/공문 & PC자료
    task_dir = out_dir / "적극행정"
    assert task_dir.is_dir()
    assert (task_dir / "00_업무안내.md").is_file()
    assert (task_dir / "2024" / "공문").is_dir()
    assert (task_dir / "2025" / "공문").is_dir()
    assert (task_dir / "2025" / "PC자료" / "혁신과-202_실행계획_초안.hwp").is_file()

    # 안내 파일과 manifest 확인
    root_readme = (out_dir / "00_인계_안내.md").read_text(encoding="utf-8-sig")
    assert "기획예산과 — 사본 인계 폴더 안내" in root_readme
    assert "기타/동호회_회비_메모.txt" in root_readme

    task_guide = (task_dir / "00_업무안내.md").read_text(encoding="utf-8-sig")
    assert "이 폴더에 복사된 사본 목록" in task_guide
    assert "파일 이름에 공문 문서번호 (high)" in task_guide

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8-sig"))
    assert manifest["format"] == "nunchi.handover-folder.v1"
    assert manifest["read_only_source_guaranteed"] is True
    assert manifest["summary"]["files_copied"] == 3
    categories = {item["category"] for item in manifest["files"]}
    assert categories == {"official", "local"}
    local_entry = next(item for item in manifest["files"] if item["category"] == "local")
    assert local_entry["evidence"] == "파일 이름에 공문 문서번호"
    assert local_entry["confidence"] == "high"
    assert local_entry["sha256"] == "hash-local-draft"

    # 원본 불변 검증
    after_local = {p: p.read_bytes() for p in local_root.rglob("*") if p.is_file()}
    after_onnara = {p: p.read_bytes() for p in onnara_root.rglob("*") if p.is_file()}
    assert before_local == after_local
    assert before_onnara == after_onnara


def test_export_handover_folder_blocks_writing_inside_source_folders(workspace):
    db, onnara_root, local_root, *_ = workspace

    with pytest.raises(UnsafeOutputError):
        handover_folder.export_handover_folder(db, local_root)

    with pytest.raises(UnsafeOutputError):
        handover_folder.export_handover_folder(db, local_root / "하위폴더")

    with pytest.raises(UnsafeOutputError):
        handover_folder.export_handover_folder(db, onnara_root / "export")


def test_handover_view_triggers_copy_folder_export(workspace, qapp):
    db, _, _, _, _, tmp_path = workspace
    view = HandoverView(db, project_name="인계시험")
    try:
        assert view.folder_button.isEnabled()
        target = tmp_path / "ui_handover_out"
        res = view.write_handover_folder(target)
        assert res.files_copied == 3
        assert (target / "00_인계_안내.md").is_file()
    finally:
        view.setParent(None)

def test_export_handover_folder_with_charter_hierarchy_and_comparison(workspace):
    db, _, local_root, _, _, tmp_path = workspace

    # 분장 계층 등록: 대업무(시설관리) -> 세부업무(청사시설관리)
    p_id = db.con.execute(
        "INSERT INTO charter_items(name, doc_id, weight) VALUES ('시설관리', 1, 10.0)"
    ).lastrowid
    db.con.execute(
        "INSERT INTO charter_items(name, parent_id, doc_id, weight) VALUES ('청사시설관리', ?, 1, 5.0)",
        (p_id,),
    )

    # 3갈래 대조 메타데이터 등록
    comparison_meta = {
        "matched": [{"name": "청사시설관리", "doc_count": 2}],
        "charter_only": [{"name": "예산총괄", "weight": 5.0}],
        "data_only": [{"name": "비공식행사", "doc_count": 1}],
    }
    db.set_meta("charter_comparison", json.dumps(comparison_meta, ensure_ascii=False))

    # task 등록
    task_id = db.con.execute(
        "INSERT INTO tasks(name, origin, status) VALUES ('청사시설관리', 'ai', 'proposed')"
    ).lastrowid
    doc_id = db.con.execute("SELECT id FROM documents LIMIT 1").fetchone()[0]
    db.con.execute(
        "INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, ?, 'ai', 'high')",
        (task_id, doc_id),
    )
    db.con.commit()

    target = tmp_path / "charter_handover_out"
    res = handover_folder.export_handover_folder(db, target, today=date(2026, 10, 31))

    # 폴더 구조 검증: <출력루트>/시설관리/청사시설관리/<연도>/...
    assert (target / "시설관리" / "청사시설관리").is_dir()

    # 00_인계_안내.md에 charter_only와 data_only가 실렸는지 검증
    readme = (target / "00_인계_안내.md").read_text(encoding="utf-8")
    assert "업무분장표 ↔ 자료 대조 현황" in readme
    assert "예산총괄" in readme
    assert "비공식행사" in readme


def test_guide_lists_the_comparison_that_discovery_actually_stores(workspace):
    """discover가 저장하는 꼴 그대로 안내문에 실린다. 예전 프로젝트의 이름 목록도 읽는다."""
    from app.core import charter

    db, _, _, _, _, tmp_path = workspace
    doc_id = db.con.execute("SELECT id FROM documents LIMIT 1").fetchone()[0]
    charter.save_charter_items(db, doc_id, [charter.ExtractedItem(name="미래전략기획")])
    db.con.commit()
    discover(db, client=None)
    data_only = json.loads(db.get_meta("charter_comparison"))["data_only"]
    assert data_only, "공문으로 세운 업무가 분장 밖 후보로 잡혀야 한다"
    first = data_only[0]

    handover_folder.export_handover_folder(db, tmp_path / "out1", today=date(2026, 11, 1))
    text = (tmp_path / "out1" / "00_인계_안내.md").read_text(encoding="utf-8")
    assert "미래전략기획" in text and f"**{first['name']}** ({first['doc_count']}건)" in text

    db.set_meta("charter_comparison", json.dumps({"charter_only": ["예전형식업무"], "data_only": []},
                                                 ensure_ascii=False))
    handover_folder.export_handover_folder(db, tmp_path / "out2", today=date(2026, 11, 1))
    assert "예전형식업무" in (tmp_path / "out2" / "00_인계_안내.md").read_text(encoding="utf-8")
