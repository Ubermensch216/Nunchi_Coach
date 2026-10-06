"""온나라 공식 출처가 수집 → 프로젝트 문서 → 답변 근거까지 이어지는가."""

import json

from app.connectors.onnara import FixtureConnector, SearchScope
from app.core import provenance
from app.db import Database
from app.jobs.onnara_collect import Collection
from app.search.rag import _compose_answer, _format_context
from app.search.verify import VerifiedSentence
from app.tools.onnara_demo import index_collection, make_catalog

SCOPE = SearchScope(("author-001",), "2025-01-01", "2025-12-31")


def collect(tmp_path, edit=None):
    catalog = make_catalog(tmp_path / "source", count=5)
    if edit:
        data = json.loads(catalog.read_text(encoding="utf-8"))
        edit(data)
        catalog.write_text(json.dumps(data), encoding="utf-8")
    output = tmp_path / "collected"
    state = Collection(FixtureConnector(catalog), SCOPE, output).run()
    result = index_collection(state, output, tmp_path / "project")
    return state, output, result, Database(tmp_path / "project" / "project.db")


def test_every_collected_file_carries_its_official_document(tmp_path):
    state, _output, result, db = collect(tmp_path)
    try:
        assert (result["records"], result["linked"], result["unlinked"]) == (2, 6, 0)
        ids = [r["id"] for r in db.con.execute("SELECT id FROM documents")]
        context = db.document_context(ids)
        assert len(context) == 6 and all("official" in item for item in context.values())
        roles = sorted(item["official"]["role"] for item in context.values())
        assert roles == ["attachment"] * 4 + ["body"] * 2
        body = next(i["official"] for i in context.values() if i["official"]["role"] == "body")
        assert body["document_id"] in state["documents"]
        assert body["document_number"].startswith("합성부서-")
        # 합성 수집 결과는 실제 기관 문서처럼 보이면 안 된다.
        assert provenance.describe(body) == f"온나라(합성) · {body['document_number']} · 결재완료 · 본문"
        run = db.con.execute("SELECT * FROM collection_runs").fetchone()
        assert run["simulation"] == 1 and run["live_verified"] == 0
        assert run["completeness"] == "complete"
    finally:
        db.close()


def test_registering_again_updates_in_place_and_records_status_changes(tmp_path):
    state, output, _result, db = collect(tmp_path)
    try:
        changed = next(iter(state["documents"]))
        state["documents"][changed]["status"] = "rejected"
        again = provenance.register_collection(db, state, output)
        assert again["changed"] == 1 and again["linked"] == 6
        assert db.con.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 1
        assert db.con.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 2
        assert db.con.execute("SELECT COUNT(*) FROM source_assets").fetchone()[0] == 6
        log = db.con.execute(
            "SELECT target, detail FROM audit_logs WHERE action='onnara.record.status'").fetchone()
        assert (log["target"], log["detail"]) == (changed, "approved → rejected")

        # 다시 수집한 목록에서 빠졌다고 기존 출처를 지우지 않는다.
        state["documents"].pop(changed)
        state["assets"] = {k: v for k, v in state["assets"].items() if v["document_id"] != changed}
        provenance.register_collection(db, state, output)
        assert db.con.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 2
        assert db.con.execute("SELECT COUNT(*) FROM source_assets").fetchone()[0] == 6
    finally:
        db.close()


def test_undownloaded_attachment_is_kept_with_its_reason(tmp_path):
    def deny(data):
        data["documents"][0]["assets"][1]["denied"] = True
    _state, _output, result, db = collect(tmp_path, deny)
    try:
        assert result["linked"] == 5
        row = db.con.execute("SELECT * FROM source_assets WHERE status='denied'").fetchone()
        assert row["error"] == "AccessDenied" and row["doc_id"] is None
    finally:
        db.close()


def test_local_copy_is_marked_same_content_only_when_identical(tmp_path):
    state, _output, _result, db = collect(tmp_path)
    try:
        entry = next(e for e in state["assets"].values() if e["asset"]["role"] == "body")
        local = db.add_source(str(tmp_path / "개인폴더"))
        same = db.upsert_document(local, {"path": "개인/등록본사본.odt", "filename": "등록본사본.odt",
                                          "ext": ".odt", "hash": entry["sha256"]})
        draft = db.upsert_document(local, {"path": "개인/초안.odt", "filename": "초안.odt",
                                           "ext": ".odt", "hash": "0" * 64})
        found = provenance.official_sources(db, [same, draft])
        assert set(found) == {same}
        assert found[same]["same_content"] and found[same]["document_id"] == entry["document_id"]
        assert provenance.describe(found[same]).startswith("온나라(합성) 수집본과 같은 내용 · ")
    finally:
        db.close()


def test_answer_evidence_shows_the_official_source(tmp_path):
    _state, _output, _result, db = collect(tmp_path)
    try:
        rows = [dict(r) for r in db.con.execute(
            "SELECT c.id AS chunk_id, c.doc_id, c.locator, c.text, d.filename, d.path"
            " FROM chunks c JOIN documents d ON d.id = c.doc_id"
            " WHERE d.ext = '.odt' ORDER BY c.id LIMIT 1")]
        official = provenance.official_sources(db, [rows[0]["doc_id"]])
        assert "온나라(합성)" in _format_context(rows, official).splitlines()[0]
        assert "온나라" not in _format_context(rows).splitlines()[0]
        _text, citations = _compose_answer(
            [VerifiedSentence("예산은 820백만원이다.", [1])], rows, official)
        assert citations[0].official.endswith("결재완료 · 본문")
    finally:
        db.close()
