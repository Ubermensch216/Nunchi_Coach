from dataclasses import replace
import json
import hashlib

import pytest

from app.connectors.onnara import FixtureConnector, SearchScope
from app.connectors.onnara.contract import AuthenticationRequired, SearchPage
from app.jobs.onnara_collect import Collection
from app.tools.onnara_demo import make_catalog, index_collection


SCOPE = SearchScope(("author-001",), "2025-01-01", "2025-12-31")


@pytest.fixture(autouse=True)
def no_ollama(monkeypatch):
    """이 파일은 모델 없이 돈다 — 개발 장비에 Ollama가 떠 있어도 원격 CI와 같은 조건."""
    import app.jobs.pipeline as pipeline_module
    from app.ai.client import OllamaClient
    monkeypatch.setattr(pipeline_module, "OllamaClient",
                        lambda *a, **k: OllamaClient(base_url="http://127.0.0.1:1"))


def test_all_pages_body_and_attachments_are_collected_without_namesake(tmp_path):
    catalog = make_catalog(tmp_path / "source")
    connector = FixtureConnector(catalog, page_size=4)
    state = Collection(connector, SCOPE, tmp_path / "collected").run()
    assert state["summary"]["documents"] == 27
    assert state["summary"]["downloaded"] == 81
    assert state["summary"]["completeness"] == "complete"
    assert state["summary"]["simulation"] and not state["summary"]["live_verified"]
    assert all(r["author_id"] == "author-001" for r in state["documents"].values())
    assert len(connector.resolve_author("합성담당자")) == 2


def test_restart_reuses_successes_and_repairs_modified_local_files(tmp_path):
    connector = FixtureConnector(make_catalog(tmp_path / "source", count=5))
    output = tmp_path / "collected"
    first = Collection(connector, SCOPE, output).run()
    original = connector.download_asset
    calls = []
    connector.download_asset = lambda *args: (calls.append(args[:2]), original(*args))[-1]
    Collection(connector, SCOPE, output).run()
    assert calls == []
    entry = next(iter(first["assets"].values()))
    (output / entry["path"]).write_bytes(b"corrupted")
    state = Collection(connector, SCOPE, output).run()
    assert len(calls) == 1 and state["summary"]["completeness"] == "complete"


def test_authentication_pause_retains_completed_downloads_and_resumes(tmp_path):
    connector = FixtureConnector(make_catalog(tmp_path / "source", count=5))
    original = connector.download_asset
    calls = []
    def expire(document_id, asset_id, destination):
        calls.append(asset_id)
        if len(calls) == 2:
            raise AuthenticationRequired()
        original(document_id, asset_id, destination)
    connector.download_asset = expire
    output = tmp_path / "collected"
    first = Collection(connector, SCOPE, output).run()
    assert first["status"] == "authentication_required"
    assert first["summary"]["downloaded"] == 1
    connector.download_asset = original
    resumed = Collection(connector, SCOPE, output).run()
    assert resumed["summary"]["downloaded"] == 6
    assert resumed["summary"]["completeness"] == "complete"


def test_access_denied_is_partial_and_not_silently_discarded(tmp_path):
    catalog = make_catalog(tmp_path / "source", count=5)
    data = json.loads(catalog.read_text(encoding="utf-8"))
    data["documents"][0]["assets"][1]["denied"] = True
    catalog.write_text(json.dumps(data), encoding="utf-8")
    state = Collection(FixtureConnector(catalog), SCOPE, tmp_path / "collected").run()
    assert state["summary"]["completeness"] == "partial"
    assert state["summary"]["failed"] == 1
    assert any(a["status"] == "denied" for a in state["assets"].values())


def test_source_is_read_only_and_output_cannot_be_nested_under_it(tmp_path):
    catalog = make_catalog(tmp_path / "source", count=5)
    connector = FixtureConnector(catalog)
    before = {p: p.read_bytes() for p in connector.root.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        Collection(connector, SCOPE, connector.root / "output")
    Collection(connector, SCOPE, tmp_path / "collected").run()
    assert before == {p: p.read_bytes() for p in connector.root.rglob("*") if p.is_file()}


def test_looped_search_cursor_cannot_claim_complete(tmp_path):
    connector = FixtureConnector(make_catalog(tmp_path / "source", count=5))
    original = connector.list_documents
    def loop(scope, cursor):
        page = original(scope, None)
        return SearchPage(page.records, "repeat", page.total)
    connector.list_documents = loop
    state = Collection(connector, SCOPE, tmp_path / "collected").run()
    assert state["summary"]["completeness"] != "complete"
    assert state["listing_errors"]


def test_selected_scope_cannot_reuse_another_collections_manifest(tmp_path):
    connector = FixtureConnector(make_catalog(tmp_path / "source", count=5))
    output = tmp_path / "collected"
    Collection(connector, SCOPE, output).run()
    with pytest.raises(ValueError):
        Collection(connector, replace(SCOPE, author_ids=("author-002",)), output)


def test_wrong_hash_and_login_page_are_download_failures(tmp_path):
    catalog = make_catalog(tmp_path / "source", count=5)
    data = json.loads(catalog.read_text(encoding="utf-8"))
    data["documents"][0]["assets"][0]["sha256"] = "0" * 64
    data["documents"][1]["assets"][0].pop("sha256")
    data["documents"][1]["assets"][0].pop("size")
    page = catalog.parent / data["documents"][1]["assets"][0]["path"]
    page.write_bytes(b"<html>login required</html>")
    catalog.write_text(json.dumps(data), encoding="utf-8")
    state = Collection(FixtureConnector(catalog), SCOPE, tmp_path / "collected").run()
    assert state["summary"]["failed"] == 2
    assert state["summary"]["completeness"] == "partial"
    assert not list((tmp_path / "collected").rglob(".download-*"))


def test_collected_odt_and_attachments_reach_project_fts(tmp_path):
    from app.db import Database
    connector = FixtureConnector(make_catalog(tmp_path / "source", count=5))
    output = tmp_path / "collected"
    state = Collection(connector, SCOPE, output).run()
    project = tmp_path / "project"
    result = index_collection(state, output, project)
    assert result == {"parsed": 6, "failed": 0, "records": 2, "linked": 6, "unlinked": 0}
    db = Database(project / "project.db")
    try:
        assert db.sources()[0]["kind"] == "onnara"
        assert db.con.execute("SELECT COUNT(*) FROM document_fts WHERE document_fts MATCH '820'").fetchone()[0] > 0
        assert db.con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] > 0
    finally:
        db.close()


def test_cancelled_download_can_resume_without_losing_successes(tmp_path):
    connector = FixtureConnector(make_catalog(tmp_path / "source", count=5))
    output = tmp_path / "collected"
    stop = []
    def progress(phase, label):
        if phase == "download":
            stop.append(True)
    state = Collection(connector, SCOPE, output).run(should_stop=lambda: bool(stop), on_progress=progress)
    assert state["status"] == "cancelled"
    assert state["summary"]["downloaded"] == 1
    resumed = Collection(connector, SCOPE, output).run()
    assert resumed["summary"]["completeness"] == "complete"


def lock_manifest(monkeypatch, refusals):
    """백신·색인기가 방금 쓴 manifest를 붙들고 있는 상황. 앞의 `refusals`번은 덮어쓰기를 거절한다."""
    import app.jobs.onnara_collect as engine
    real, refused, slept = engine.os.replace, [], []

    def replace(source, target):
        if str(target).endswith("manifest.json") and len(refused) < refusals:
            refused.append(target)
            raise PermissionError(5, "액세스가 거부되었습니다")
        return real(source, target)

    monkeypatch.setattr(engine.os, "replace", replace)
    monkeypatch.setattr(engine.time, "sleep", slept.append)      # 실제로 기다리지 않는다
    return refused


def test_manifest_held_by_another_program_does_not_fail_the_collection(tmp_path, monkeypatch):
    """N1: 전체 실행에서만 가끔 '첫 파일만 받고 실패'(downloaded 1) 하던 원인.

    중간 기록 한 번이 거절됐다고 수집을 끝내지 않는다 — 다음 파일 뒤에 다시 쓴다.
    """
    connector = FixtureConnector(make_catalog(tmp_path / "source", count=5))
    refused = lock_manifest(monkeypatch, 20)       # 예전 재시도 한도(8번)를 훌쩍 넘는다
    state = Collection(connector, SCOPE, tmp_path / "collected").run()
    assert len(refused) == 20
    assert state["status"] == "finished" and state["listing_errors"] == []
    assert state["summary"]["downloaded"] == 6 and state["summary"]["completeness"] == "complete"
    saved = json.loads((tmp_path / "collected" / "manifest.json").read_text(encoding="utf-8"))
    assert saved["summary"] == state["summary"]                   # 끝난 뒤의 기록은 디스크에 있다
    assert not list((tmp_path / "collected").glob(".checkpoint-*"))


def test_manifest_that_can_never_be_written_is_an_error_not_a_quiet_success(tmp_path, monkeypatch):
    connector = FixtureConnector(make_catalog(tmp_path / "source", count=5))
    output = tmp_path / "collected"
    lock_manifest(monkeypatch, 10_000)
    with pytest.raises(PermissionError):           # 받았다고 해 놓고 기록이 없는 상태를 만들지 않는다
        Collection(connector, SCOPE, output).run()
    assert not list(output.glob(".checkpoint-*"))
    monkeypatch.undo()                             # 붙들던 프로그램이 놓았다
    resumed = Collection(connector, SCOPE, output).run()
    assert resumed["summary"]["downloaded"] == 6 and resumed["summary"]["completeness"] == "complete"


def test_unverified_total_never_claims_complete_collection(tmp_path):
    connector = FixtureConnector(make_catalog(tmp_path / "source", count=5))
    connector._capabilities = replace(connector.capabilities, authoritative_total=False)
    state = Collection(connector, SCOPE, tmp_path / "collected").run()
    assert state["summary"]["completeness"] == "unknown"


def test_checkpoint_hash_cannot_override_the_source_asset_hash(tmp_path):
    connector = FixtureConnector(make_catalog(tmp_path / "source", count=5))
    output = tmp_path / "collected"
    state = Collection(connector, SCOPE, output).run()
    entry = next(iter(state["assets"].values()))
    path = output / entry["path"]
    bad = b"x" * path.stat().st_size
    path.write_bytes(bad)
    entry["sha256"] = hashlib.sha256(bad).hexdigest()
    (output / "manifest.json").write_text(json.dumps(state), encoding="utf-8")
    resumed = Collection(connector, SCOPE, output).run()
    assert resumed["summary"]["completeness"] == "complete"
    assert path.read_bytes() != bad
