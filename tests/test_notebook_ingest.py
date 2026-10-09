"""노트북에 올린 파일 — 보관·읽기·중복·그림 판독·업무 분석과의 분리."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from app.core import notebook, notebook_ingest, output_policy
from app.db import Database
from app.ingest import vision


class Offline:
    """모델이 없는 PC. 조각과 글자 검색까지만 준비된다."""
    embed_model = "none"

    def health(self):
        return SimpleNamespace(embedding_ready=False, generation_ready=False, message="offline")

    def _supports(self, capability):
        return False


class Seeing(Offline):
    """그림을 읽는 모델의 대역. 무엇을 보냈는지 남긴다."""

    def __init__(self, text="제출기한 | 9월 30일"):
        self.text, self.images = text, []

    def _supports(self, capability):
        return capability == "vision"

    def generate_text(self, prompt, images=None, num_predict=0):
        self.images.append(images)
        return self.text, None


@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path / "project" / "project.db")
    db.init()
    yield db
    db.close()


@pytest.fixture
def book(db):
    return notebook.create(db, "자료 분석")


def write(tmp_path, name, text="제출기한: 9월 30일까지 총무과로 제출한다."):
    path = tmp_path / "desktop" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def picture(tmp_path, name="공문.png"):
    path = tmp_path / name
    pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 20), False).save(str(path))
    return path


def test_uploaded_file_is_copied_read_and_ready_for_search(db, book, tmp_path):
    original = write(tmp_path, "요청.txt")
    before = original.read_bytes()
    outcome = notebook_ingest.ingest_file(db, book, original, Offline())
    assert outcome.usable and outcome.status == "ok"
    row = db.document(outcome.doc_id)
    stored = notebook_ingest.upload_root(db)
    assert stored in Path(row["path"]).parents
    assert row["hash"] == hashlib.sha256(before).hexdigest() and row["filename"] == "요청.txt"
    assert original.read_bytes() == before                       # 원래 자리의 파일은 그대로다
    assert db.con.execute("SELECT COUNT(*) FROM chunks WHERE doc_id=?", (outcome.doc_id,)).fetchone()[0] == 1
    source = notebook.sources(db, book)[0]
    assert source["origin"] == "upload" and source["selected"]
    assert str(outcome.doc_id) in notebook.selected_snapshot(db, book)
    _, answer = notebook.query(db, book, "제출기한", Offline())
    assert [c.doc_id for c in answer.citations] == [outcome.doc_id]


def test_upload_store_is_not_a_work_source_and_cannot_be_overwritten(db, book, tmp_path):
    outcome = notebook_ingest.ingest_file(db, book, write(tmp_path, "요청.txt"), Offline())
    assert db.work_sources() == []
    assert [row["kind"] for row in db.sources()] == ["upload"]
    with pytest.raises(output_policy.UnsafeOutputError):
        output_policy.validate_output(notebook_ingest.upload_root(db) / "결과.md", db)
    with pytest.raises(output_policy.UnsafeOutputError):
        output_policy.validate_output(db.document(outcome.doc_id)["path"], db)


def test_same_content_is_stored_once_and_shared_between_notebooks(db, book, tmp_path):
    first = notebook_ingest.ingest_file(db, book, write(tmp_path, "요청.txt"), Offline())
    other = notebook.create(db, "다른 노트북")
    again = notebook_ingest.ingest_file(db, other, write(tmp_path, "요청 사본.txt"), Offline())
    assert again.status == "reused" and again.doc_id == first.doc_id
    assert db.con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert notebook.sources(db, other)[0]["origin"] == "upload"


def test_file_identical_to_an_analysed_document_links_that_document(db, book, tmp_path):
    folder = tmp_path / "전임자"
    folder.mkdir()
    known = folder / "요청.txt"
    known.write_text("제출기한: 9월 30일", encoding="utf-8")
    source = db.add_source(folder)
    doc = db.upsert_document(source, {"path": str(known), "filename": known.name, "ext": ".txt",
                                      "hash": hashlib.sha256(known.read_bytes()).hexdigest(),
                                      "parse_status": "ok"})
    db.replace_sections(doc, [("paragraph", 1, "1문단", "제출기한: 9월 30일")])
    outcome = notebook_ingest.ingest_file(db, book, write(tmp_path, "같은내용.txt", "제출기한: 9월 30일"), Offline())
    assert outcome.status == "reused" and outcome.doc_id == doc
    assert notebook.sources(db, book)[0]["origin"] == "project"
    assert not notebook_ingest.upload_root(db).exists()
    assert str(doc) in notebook.selected_snapshot(db, book)       # 조각도 그 자리에서 준비한다


@pytest.mark.parametrize("name,text", [("메모.xyz", "내용"), ("옛문서.doc", "내용"), ("빈파일.txt", "")])
def test_unusable_files_are_rejected_with_a_reason(db, book, tmp_path, name, text):
    outcome = notebook_ingest.ingest_file(db, book, write(tmp_path, name, text), Offline())
    assert not outcome.usable and outcome.error
    assert notebook.sources(db, book) == []


def test_unreadable_document_stays_listed_but_is_never_evidence(db, book, tmp_path):
    outcome = notebook_ingest.ingest_file(db, book, write(tmp_path, "깨진.docx", "문서가 아님"), Offline())
    assert not outcome.usable and outcome.error
    source = notebook.sources(db, book)[0]
    assert source["parse_status"] not in ("ok", "partial") and source["parse_error"]
    assert notebook.selected_snapshot(db, book) == {}


def test_picture_is_read_by_the_local_vision_model_and_marked_as_such(db, book, tmp_path):
    client = Seeing()
    outcome = notebook_ingest.ingest_file(db, book, picture(tmp_path), client)
    assert outcome.status == "partial" and vision.NOTE in outcome.note
    assert len(client.images) == 1 and client.images[0][0]       # 그림 한 장을 base64로 보냈다
    row = db.document(outcome.doc_id)
    assert row["parser"] == "vision" and "원본과 대조" in row["parse_note"]
    assert db.sections(outcome.doc_id)[0]["text"] == "제출기한 | 9월 30일"
    assert str(outcome.doc_id) in notebook.selected_snapshot(db, book)


def test_picture_without_a_vision_model_is_listed_but_unusable(db, book, tmp_path):
    outcome = notebook_ingest.ingest_file(db, book, picture(tmp_path), Offline())
    assert not outcome.usable and "그림을 읽을 수 있는 로컬 모델" in outcome.error
    assert notebook.selected_snapshot(db, book) == {}


def test_picture_with_no_text_is_not_turned_into_evidence(db, book, tmp_path):
    outcome = notebook_ingest.ingest_file(db, book, picture(tmp_path), Seeing(vision.NOTHING))
    assert not outcome.usable
    assert db.sections(outcome.doc_id) == []


def test_scanned_pdf_falls_back_to_the_vision_model(db, book, tmp_path):
    path = tmp_path / "스캔본.pdf"
    with pymupdf.open() as document:
        document.new_page()
        document.new_page()
        document.save(str(path))
    client = Seeing("2쪽짜리 스캔 공문")
    outcome = notebook_ingest.ingest_file(db, book, path, client)
    assert outcome.status == "partial" and len(client.images) == 2
    assert [s["locator"] for s in db.sections(outcome.doc_id)] == ["1쪽", "2쪽"]


def test_interrupted_upload_is_found_and_read_again(db, book, tmp_path):
    outcome = notebook_ingest.ingest_file(db, book, write(tmp_path, "요청.txt"), Offline())
    db.update_document(outcome.doc_id, parse_status="pending")
    assert notebook_ingest.outdated_uploads(db) == [outcome.doc_id]
    again = notebook_ingest.reprocess(db, outcome.doc_id, Offline())
    assert again.status == "ok" and notebook_ingest.outdated_uploads(db) == []
    assert db.con.execute("SELECT COUNT(*) FROM chunks WHERE doc_id=?", (outcome.doc_id,)).fetchone()[0] == 1


# ── 온나라 수집본 ───────────────────────────────────────────────────

@pytest.fixture
def collection(tmp_path):
    from app.connectors.onnara import FixtureConnector, SearchScope
    from app.jobs.onnara_collect import Collection
    from app.tools.onnara_demo import make_catalog
    catalog = make_catalog(tmp_path / "onnara-source", count=5)   # 3건은 동명이인의 것이라 2건이 수집된다
    scope = SearchScope(("author-001",), "2025-01-01", "2025-12-31")
    state = Collection(FixtureConnector(catalog), scope, tmp_path / "collected").run()
    return tmp_path / "collected", state


def test_onnara_collection_is_read_linked_and_carries_official_provenance(db, book, collection):
    from app.core import onnara_import, provenance
    folder, state = collection
    before = {p: p.read_bytes() for p in (folder / "files").rglob("*") if p.is_file()}
    assert "합성(모의) 수집본" in onnara_import.describe(state)
    results = onnara_import.import_collection(db, book, folder, Offline())
    usable = [r for r in results if r.usable]
    assert usable and len(usable) == state["summary"]["downloaded"]
    sources = notebook.sources(db, book)
    assert {s["origin"] for s in sources} == {"onnara"} and len(sources) == len(usable)
    assert [row["kind"] for row in db.work_sources()] == ["onnara"]          # 업무 분석에도 쓰이는 자료원이다
    official = provenance.official_sources(db, [s["doc_id"] for s in sources])
    assert len(official) == len(sources)
    assert all(provenance.describe(entry) for entry in official.values())
    assert {p: p.read_bytes() for p in before} == before                     # 수집 폴더는 읽기만 한다
    _, answer = notebook.query(db, book, "실적 보고", Offline())
    assert answer.citations and all(c.official for c in answer.citations)
    # 다시 가져와도 문서가 늘지 않는다.
    onnara_import.import_collection(db, book, folder, Offline())
    assert len(notebook.sources(db, book)) == len(usable)


def test_folder_that_is_not_a_collection_is_refused(db, book, tmp_path):
    from app.core import onnara_import
    (tmp_path / "아무폴더").mkdir()
    with pytest.raises(ValueError):
        onnara_import.import_collection(db, book, tmp_path / "아무폴더", Offline())
    (tmp_path / "아무폴더" / "manifest.json").write_text('{"schema_version": 9}', encoding="utf-8")
    with pytest.raises(ValueError):
        onnara_import.import_collection(db, book, tmp_path / "아무폴더", Offline())
    assert notebook.sources(db, book) == [] and db.sources() == []
