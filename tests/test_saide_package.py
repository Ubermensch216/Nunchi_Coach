"""sAIde 꾸러미 — 읽기·수집·완전성 판정·경로 방어·노트북 가져오기."""

import json
from types import SimpleNamespace

import pytest

from app.connectors.onnara import SaidePackageConnector
from app.connectors.onnara.saide_package import iso_date
from app.core import notebook, onnara_import, provenance
from app.db import Database
from app.jobs.onnara_collect import Collection


class Offline:
    embed_model = "none"

    def health(self):
        return SimpleNamespace(embedding_ready=False, generation_ready=False, message="offline")

    def _supports(self, capability):
        return False


def make_package(folder, count=3, **changes):
    """합성 꾸러미. 실제 온나라 자료가 아니다."""
    folder.mkdir(parents=True)
    documents = []
    for index in range(1, count + 1):
        sub = folder / f"2025090{index}_실적보고 {index}"
        sub.mkdir()
        (sub / "본문.txt").write_text(f"실적 보고 {index}. 제출기한: 2025-09-30. 예산 82{index}백만원.", encoding="utf-8")
        (sub / "붙임.csv").write_text(f"사업,금액\n시범사업 {index},82{index}\n", encoding="utf-8")
        documents.append({
            "document_id": f"row-{index}", "id_basis": "list-row", "title": f"실적보고 {index}",
            "author_name": "합성담당자", "department": "합성부서", "authored_at": f"2025. 9. {index}.",
            "assets": [
                {"asset_id": "a1", "role": "body", "filename": "본문.txt", "path": f"{sub.name}/본문.txt",
                 "status": "complete", "fidelity": "rendered-text"},
                {"asset_id": "a2", "role": "attachment", "filename": "붙임.csv", "path": f"{sub.name}/붙임.csv",
                 "status": "complete", "fidelity": "original"},
            ]})
    data = {"format": "saide-onnara-package", "version": 1, "run_id": "20261012-153000-test",
            "created_at": "2026-10-12T06:30:00.000Z", "exporter": {"name": "onNara.sAIde", "version": "0.0.0"},
            "system": {"origin": "http://onnara.invalid", "institution": ""},
            "scope": {"kind": "selection", "list_name": "기록물등록대장"},
            "listing": {"total": None, "finished": False}, "documents": documents}
    data.update(changes)
    write_package(folder, data)
    return data


def write_package(folder, data):
    (folder / "package.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def collect(folder, output):
    connector = SaidePackageConnector(folder)
    return Collection(connector, connector.scope(), output).run()


def test_dates_written_the_way_lists_show_them_are_read():
    assert iso_date("2025. 9. 1.") == "2025-09-01"
    assert iso_date("2025-09-01 14:20") == "2025-09-01"
    assert iso_date("20250901") == "2025-09-01"
    assert iso_date("2025년 9월 1일") == "2025-09-01"
    assert iso_date("어제") == "" and iso_date("2025.13.1") == "" and iso_date(None) == ""


def test_name_based_package_is_collected_but_never_called_complete(tmp_path):
    make_package(tmp_path / "pkg")
    state = collect(tmp_path / "pkg", tmp_path / "collected")
    summary = state["summary"]
    assert state["status"] == "finished"
    assert (summary["documents"], summary["downloaded"], summary["failed"]) == (3, 6, 0)
    assert summary["completeness"] == "unknown"                    # 이름 기준 — 동명이인을 가를 수 없다
    assert not summary["simulation"] and not summary["live_verified"]
    assert state["identity"]["adapter"]["system_id"] == "onnara@onnara.invalid"
    assert state["package"]["author_identity"] == "name"
    assert state["package"]["id_basis"] == {"list-row": 3}
    assert state["package"]["body_fidelity"] == {"rendered-text": 3}
    assert {r["authored_at"] for r in state["documents"].values()} == {"2025-09-01", "2025-09-02", "2025-09-03"}
    bodies = [a["asset"] for a in state["assets"].values() if a["asset"]["role"] == "body"]
    assert {a["fidelity"] for a in bodies} == {"rendered-text"}
    text = onnara_import.describe(state)
    assert "이름 기준" in text and "목록 행" in text and "화면 글자로 만든 PDF" in text


def test_whole_listing_with_author_ids_can_be_complete(tmp_path):
    data = make_package(tmp_path / "pkg")
    for document in data["documents"]:
        document["author_id"] = "author-001"
        document["id_basis"] = "source-document-id"
    data["scope"] = {"kind": "listing", "author_ids": ["author-001"],
                     "start_date": "2025-01-01", "end_date": "2025-12-31"}
    data["listing"] = {"total": 3, "finished": True}
    write_package(tmp_path / "pkg", data)
    assert collect(tmp_path / "pkg", tmp_path / "collected")["summary"]["completeness"] == "complete"
    data["listing"]["total"] = 5                                   # 화면은 5건이라 했는데 3건만 왔다
    write_package(tmp_path / "pkg", data)
    assert collect(tmp_path / "pkg", tmp_path / "again")["summary"]["completeness"] == "unknown"


def test_files_the_extension_could_not_get_stay_on_record(tmp_path):
    data = make_package(tmp_path / "pkg")
    data["documents"][0]["assets"][1] = {"asset_id": "a2", "role": "attachment", "filename": "붙임.xlsx",
                                         "status": "failed", "message": "다운로드가 중단되었습니다."}
    data["documents"][1]["assets"] = [a for a in data["documents"][1]["assets"] if a["role"] != "body"]
    write_package(tmp_path / "pkg", data)
    state = collect(tmp_path / "pkg", tmp_path / "collected")
    assert state["summary"]["completeness"] == "partial"
    assert state["summary"]["failed"] == 1
    assert [e["document_id"] for e in state["metadata_errors"]] == ["row-2"]     # 본문이 없는 문서


@pytest.mark.parametrize("path", ["../outside.txt", "C:/Windows/win.ini", "/etc/hosts", "sub\\..\\..\\outside.txt"])
def test_paths_outside_the_package_are_never_read(tmp_path, path):
    (tmp_path / "outside.txt").write_text("꾸러미 밖의 파일", encoding="utf-8")
    data = make_package(tmp_path / "pkg", count=1)
    data["documents"][0]["assets"][1]["path"] = path
    write_package(tmp_path / "pkg", data)
    state = collect(tmp_path / "pkg", tmp_path / "collected")
    assert state["summary"]["failed"] == 1 and state["summary"]["downloaded"] == 1
    assert not any("outside" in p.name for p in (tmp_path / "collected").rglob("*"))


def test_package_folder_is_read_only_and_resume_copies_nothing_twice(tmp_path):
    make_package(tmp_path / "pkg")
    before = {p: p.read_bytes() for p in (tmp_path / "pkg").rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        collect(tmp_path / "pkg", tmp_path / "pkg" / "out")
    collect(tmp_path / "pkg", tmp_path / "collected")
    connector = SaidePackageConnector(tmp_path / "pkg")
    calls = []
    original = connector.download_asset
    connector.download_asset = lambda *args: (calls.append(args[:2]), original(*args))[-1]
    Collection(connector, connector.scope(), tmp_path / "collected").run()
    assert calls == []
    assert before == {p: p.read_bytes() for p in (tmp_path / "pkg").rglob("*") if p.is_file()}


def test_undated_document_is_reported_not_guessed(tmp_path):
    data = make_package(tmp_path / "pkg")
    data["documents"][2]["authored_at"] = ""
    write_package(tmp_path / "pkg", data)
    state = collect(tmp_path / "pkg", tmp_path / "collected")
    assert state["summary"]["documents"] == 2 and state["listing_errors"]


def test_unknown_formats_are_refused_with_a_reason(tmp_path):
    with pytest.raises(ValueError, match="package.json"):
        SaidePackageConnector(tmp_path)
    make_package(tmp_path / "v2", version=2)
    with pytest.raises(ValueError, match="형식"):
        SaidePackageConnector(tmp_path / "v2")
    data = make_package(tmp_path / "dup")
    data["documents"][1]["document_id"] = "row-1"
    write_package(tmp_path / "dup", data)
    with pytest.raises(ValueError, match="중복"):
        SaidePackageConnector(tmp_path / "dup")
    make_package(tmp_path / "empty", documents=[])
    with pytest.raises(ValueError, match="범위"):
        SaidePackageConnector(tmp_path / "empty")


def test_package_written_by_the_extension_is_understood(tmp_path):
    """두 저장소의 공통 계약. 같은 파일이 onNara.sAIde `src/lib/nunchi/package.fixture.json`에 있고
    확장의 꾸러미 작성기가 정확히 이 내용을 만드는지 그쪽에서 시험한다. 규격을 바꾸면 함께 바꾼다."""
    from pathlib import Path
    data = json.loads((Path(__file__).parent / "fixtures" / "saide_package_v1.json").read_text(encoding="utf-8"))
    folder = tmp_path / "pkg"
    for document in data["documents"]:
        for asset in document["assets"]:
            if asset.get("path"):
                path = folder / asset["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"합성 파일 {asset['filename']}".encode("utf-8"))
    write_package(folder, data)
    state = collect(folder, tmp_path / "collected")
    summary = state["summary"]
    assert (summary["documents"], summary["downloaded"], summary["failed"]) == (2, 3, 2)
    assert summary["completeness"] == "partial"
    # 화면에서 읽은 글자로 받은 본문 — 원본이 아니고, 끝까지 읽지 못했다는 것까지 남는다.
    assert state["package"]["body_fidelity"] == {"rendered-text": 1, "extracted-text": 1}
    assert state["package"]["body_truncated"] == 1
    text = onnara_import.describe(state)
    assert "본문 1건은 추출한 글자" in text and "본문 1건은 일부만 담김" in text
    markdown = next(a for a in state["assets"].values() if a["asset"]["filename"].endswith(".md"))
    assert markdown["asset"]["fidelity"] == "extracted-text" and markdown["path"].endswith("-글자로 받은 문서_본문.md")
    assert [e["document_id"] for e in state["metadata_errors"]] == ["row-e23f25bd8f5e52fd4b6ddf94"]   # 열지 못한 문서
    record = state["documents"]["row-e11ced56b333bd2d398b410a"]
    assert (record["author_name"], record["department"], record["authored_at"], record["status"]) == (
        "홍길동", "기획과", "2025-09-01", "결재완료")
    assert state["package"]["run_id"] == "20261012_153000_4f2a" and state["package"]["exporter"] == "onNara.sAIde 0.1.0"
    body = next(a for a in state["assets"].values() if a["asset"]["role"] == "body")
    assert body["path"].endswith("-2025년 실적 보고_본문.pdf")        # 저장 이름은 브라우저가 붙인 이름이 아니라 공문의 이름


def test_folder_picker_tells_a_package_from_a_collection_before_importing(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
    from app.ui.views.notebooks.sources_panel import SourcesPanel
    qapp = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "project" / "project.db")
    db.init()
    panel = SourcesPanel(db)
    try:
        panel.set_notebook(notebook.create(db, "전임자 공문"), True)
        make_package(tmp_path / "pkg")
        asked, started = [], []
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args, **kwargs: str(tmp_path / "pkg"))
        monkeypatch.setattr(QMessageBox, "question",
                            lambda *args, **kwargs: (asked.append(args[2]), QMessageBox.StandardButton.Yes)[-1])
        monkeypatch.setattr(panel, "import_collection", started.append)
        panel._pick_collection()
        assert "sAIde 꾸러미 · 문서 3건" in asked[0] and "이름 기준" in asked[0]
        assert "프로젝트 안에 수집본으로 복사" in asked[0] and "온나라에 접속하지는 않습니다" in asked[0]
        assert started == [str(tmp_path / "pkg")]
    finally:
        panel._ingest.stop()
        panel.deleteLater()
        qapp.processEvents()
        db.close()


def test_package_becomes_a_project_source_and_analysis_links_the_official_origin(tmp_path, monkeypatch):
    """노트북 없이 자료 폴더로 등록 — 읽기는 분석이 하고, 읽은 뒤에 공식 출처가 이어진다."""
    import app.jobs.pipeline as pipeline_module
    from app.ai.client import OllamaClient
    monkeypatch.setattr(pipeline_module, "OllamaClient",
                        lambda *a, **k: OllamaClient(base_url="http://127.0.0.1:1"))
    db = Database(tmp_path / "project" / "project.db")
    db.init()
    try:
        make_package(tmp_path / "pkg")
        result = onnara_import.add_to_project(db, tmp_path / "pkg")
        files = tmp_path / "project" / "onnara" / "20261012-153000-test" / "files"
        assert [(row["path"], row["kind"]) for row in db.work_sources()] == [(str(files), "onnara")]
        assert (result["records"], result["linked"], result["unlinked"]) == (3, 0, 6)     # 아직 읽기 전이다
        assert onnara_import.add_to_project(db, tmp_path / "pkg")["source_id"] == result["source_id"]
    finally:
        db.close()

    failures = []
    pipeline = pipeline_module.Pipeline(tmp_path / "project" / "project.db")
    pipeline.failed.connect(failures.append)
    pipeline.run()
    assert failures == []

    db = Database(tmp_path / "project" / "project.db")
    try:
        docs = [row["id"] for row in db.con.execute("SELECT id FROM documents WHERE missing_since IS NULL")]
        official = provenance.official_sources(db, docs)
        assert len(docs) == 6 and len(official) == 6
        assert not any(o["same_content"] for o in official.values())
        assert db.con.execute("SELECT COUNT(*) FROM document_fts WHERE document_fts MATCH '제출기한'").fetchone()[0] > 0
        assert onnara_import.relink(db) == 0                                              # 이을 것이 없으면 조용하다
    finally:
        db.close()


def test_settings_registers_onnara_material_as_a_source_folder(tmp_path):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton
    from app.ui.views.settings import SettingsDialog
    qapp = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "project" / "project.db")
    db.init()
    dialog = SettingsDialog(db)
    try:
        assert "+ 온나라 자료 가져오기" in [button.text() for button in dialog.findChildren(QPushButton)]
        make_package(tmp_path / "pkg")
        changed = []
        dialog.sources_changed.connect(lambda: changed.append(True))
        dialog.add_onnara(str(tmp_path / "pkg"))
        assert changed == [True]                                                          # 분석을 다시 돌리게 한다
        assert [row["kind"] for row in db.work_sources()] == ["onnara"]
        assert "온나라 수집본" in [label.text() for label in dialog.findChildren(QLabel)]
        with pytest.raises(ValueError):
            dialog.add_onnara(str(tmp_path / "아무폴더"))
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()
        db.close()


def test_project_made_before_these_columns_gains_them_and_keeps_its_records(tmp_path):
    """출처의 근거 열은 판을 올리지 않고 붙는다. 그 전에 만든 프로젝트의 출처 기록은 그대로다."""
    from app.connectors.onnara import FixtureConnector, SearchScope
    from app.tools.onnara_demo import make_catalog
    connector = FixtureConnector(make_catalog(tmp_path / "source", count=5))
    state = Collection(connector, SearchScope(("author-001",), "2025-01-01", "2025-12-31"), tmp_path / "collected").run()
    path = tmp_path / "project" / "project.db"
    db = Database(path)
    db.init()
    provenance.register_collection(db, state, tmp_path / "collected")
    for table, column in (("collection_runs", "author_identity"), ("source_records", "id_basis"),
                          ("source_assets", "fidelity")):
        db.con.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    db.close()

    db = Database(path)
    db.init()
    try:
        row = db.con.execute("SELECT r.id_basis, a.fidelity, c.author_identity, c.simulation FROM source_assets a"
                             " JOIN source_records r ON r.id=a.record_id JOIN collection_runs c ON c.id=r.run_id").fetchone()
        assert tuple(row) == ("", "", "", 1)                       # 밝히지 않은 값은 빈 값으로 남는다
        assert db.con.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 2
        official = {"simulation": 1, "status": "approved", "role": "body", "document_number": "합성부서-1",
                    "fidelity": "", "id_basis": ""}
        assert provenance.describe(official) == "온나라(합성) · 합성부서-1 · 결재완료 · 본문"
    finally:
        db.close()


def test_package_reaches_the_notebook_with_its_source_shown(tmp_path):
    db = Database(tmp_path / "project" / "project.db")
    db.init()
    try:
        book = notebook.create(db, "전임자 공문")
        make_package(tmp_path / "pkg")
        assert onnara_import.is_package(tmp_path / "pkg")
        assert "sAIde 꾸러미 · 문서 3건 · 파일 6개" in onnara_import.describe_package(tmp_path / "pkg")
        results = onnara_import.import_collection(db, book, tmp_path / "pkg", Offline())
        usable = [r for r in results if r.usable]
        assert len(usable) == 6
        collected = tmp_path / "project" / "onnara" / "20261012-153000-test"
        assert (collected / "manifest.json").is_file()
        assert not onnara_import.is_package(collected)
        official = provenance.official_sources(db, [r.doc_id for r in usable])
        assert len(official) == 6
        assert all(o["system_id"] == "onnara@onnara.invalid" and not o["simulation"] for o in official.values())
        assert "온나라(합성)" not in provenance.describe(next(iter(official.values())))
        # 근거 옆 한 줄이 원본이 아닌 본문과 문서 ID가 없다는 사실을 숨기지 않는다.
        assert {(o["role"], o["fidelity"]) for o in official.values()} == {
            ("body", "rendered-text"), ("attachment", "original")}
        assert {(o["id_basis"], o["author_identity"]) for o in official.values()} == {("list-row", "name")}
        lines = {o["role"]: provenance.describe(o) for o in official.values()}
        assert lines["body"] == "온나라 · 상태 미상 · 본문(화면 글자로 만든 PDF) · 문서 ID 미확인"
        assert lines["attachment"] == "온나라 · 상태 미상 · 붙임 · 문서 ID 미확인"
        assert db.con.execute("SELECT COUNT(*) FROM document_fts WHERE document_fts MATCH '제출기한'").fetchone()[0] > 0
        again = onnara_import.import_collection(db, book, tmp_path / "pkg", Offline())      # 다시 가져와도 늘지 않는다
        assert len(notebook.sources(db, book)) == len(usable) == len([r for r in again if r.usable])
    finally:
        db.close()
