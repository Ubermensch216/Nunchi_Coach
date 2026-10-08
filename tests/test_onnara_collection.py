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
