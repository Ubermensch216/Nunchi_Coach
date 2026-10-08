"""청크 생성 파이프라인 단계 통합 시험.

Pipeline._chunks를 QThread 없이 직접 불러 검증한다. Ollama가 없으면
건너뛴다는 계약(AI는 단일 장애점이 아니다)을 확인하는 것이 이 파일의
핵심이다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ai.client import OllamaClient  # noqa: E402
from app.db import Database  # noqa: E402
from app.jobs.pipeline import Pipeline  # noqa: E402

DEAD = "http://127.0.0.1:1"


@pytest.fixture(scope="session", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.db")
    database.init()
    yield database
    database.close()


def _doc_with_sections(db: Database, source_id: int, filename: str,
                       sections: list[tuple[str, str]], hash_: str = "h1") -> int:
    doc_id = db.upsert_document(source_id, {
        "path": rf"D:\자료\{filename}", "filename": filename, "ext": ".hwp",
        "parse_status": "ok", "hash": hash_,
    })
    db.replace_sections(doc_id, [
        (kind_from(i), i, locator, text) for i, (locator, text) in enumerate(sections, start=1)
    ])
    return doc_id


def kind_from(_i: int) -> str:
    return "paragraph"


def _run(db: Database, monkeypatch, factory) -> list:
    import app.jobs.pipeline as pipeline_module
    monkeypatch.setattr(pipeline_module, "OllamaClient", factory)
    reports = []
    pipeline = Pipeline(db.path)
    pipeline.stage_done.connect(lambda r: reports.append(r))
    pipeline._chunks(db)
    return reports


def _fake(state: dict):
    """Ollama 자리에 서는 모의 임베더. state로 준비 여부와 실패 시점을 바꾼다."""
    from app.ai.client import Health

    class Fake:
        def __init__(self, gen_model: str = "gen", embed_model: str = "bge-m3", **_kw):
            self.gen_model, self.embed_model = gen_model, embed_model

        def health(self):
            ready = state.get("ready", True)
            return Health(ok=ready, message="모의 연결 없음", embedding_ready=ready)

        def embed(self, texts):
            state["calls"] = state.get("calls", 0) + 1
            if state.get("fail_on") == state["calls"]:
                return [], "모의 실패"
            return [[0.1, 0.2, 0.3] for _ in texts], None

    return Fake


def _chunk_rows(db: Database) -> list[tuple]:
    return [tuple(r) for r in db.con.execute(
        "SELECT id, doc_id, ordinal, locator, text FROM chunks ORDER BY id")]


def _count(db: Database, table: str) -> int:
    return db.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_chunks_are_built_without_ollama_and_only_embedding_waits(db: Database, monkeypatch):
    """AI는 단일 장애점이 아니다 — 조각 나누기는 모델 없이 하고 임베딩만 미룬다.

    예전 계약은 '연결이 없으면 조각도 만들지 않는다'였다. 그러면 Ollama가 없는
    장비(원격 CI 포함)에서 온나라 수집본이 조각·글자 검색·공식 출처 인용까지
    가지 못한다(개발지시서 P0-01).
    """
    source_id = db.add_source(r"D:\자료")
    _doc_with_sections(db, source_id, "문서.hwp", [("1문단", "고유낱말검사 본문입니다")])

    reports = _run(db, monkeypatch, lambda *a, **k: OllamaClient(base_url=DEAD))

    assert _count(db, "chunks") == 1
    assert _count(db, "embeddings") == 0
    assert db.con.execute(
        "SELECT COUNT(*) FROM chunk_fts WHERE chunk_fts MATCH '고유낱말검사'").fetchone()[0] == 1
    assert "임베딩 보류" in reports[0].note and "1조각 나눔" in reports[0].note
    # chunks_checked는 표시하지 않는다 — 연결되면 임베딩을 이어서 해야 하므로.
    assert db.get_meta("chunks_checked") is None


def test_late_ollama_embeds_existing_chunks_without_changing_their_ids(db: Database, monkeypatch):
    source_id = db.add_source(r"D:\자료")
    _doc_with_sections(db, source_id, "문서.hwp", [("1문단", "첫 문단"), ("표1", "둘째")])
    state = {"ready": False}
    _run(db, monkeypatch, _fake(state))
    before = _chunk_rows(db)
    assert before and _count(db, "embeddings") == 0

    state["ready"] = True
    reports = _run(db, monkeypatch, _fake(state))

    assert _chunk_rows(db) == before
    assert _count(db, "embeddings") == len(before)
    assert db.unembedded_chunk_count("bge-m3") == 0
    assert db.get_meta("chunks_checked") == "1"
    assert "조각 준비" in reports[0].note


def test_embedding_failure_keeps_chunks_and_resumes_only_what_is_left(db: Database, monkeypatch):
    source_id = db.add_source(r"D:\자료")
    for i in (1, 2):
        _doc_with_sections(db, source_id, f"문서{i}.hwp", [("1문단", f"문서{i} 본문")],
                           hash_=f"h{i}")
    state = {"fail_on": 2}
    reports = _run(db, monkeypatch, _fake(state))
    before = _chunk_rows(db)
    assert len(before) == 2 and _count(db, "embeddings") == 1
    assert reports[0].errors and "1조각 남음" in reports[0].note

    state.update(fail_on=None, calls=0)
    _run(db, monkeypatch, _fake(state))

    assert _chunk_rows(db) == before
    assert _count(db, "embeddings") == 2
    assert state["calls"] == 1, "이미 있는 임베딩까지 다시 만들었다"


def test_changing_the_embedding_model_refills_vectors_on_the_same_chunks(db: Database, monkeypatch):
    source_id = db.add_source(r"D:\자료")
    _doc_with_sections(db, source_id, "문서.hwp", [("1문단", "본문")])
    _run(db, monkeypatch, _fake({}))
    before = _chunk_rows(db)

    db.set_meta("embed_model", "새모델")
    _run(db, monkeypatch, _fake({}))

    assert _chunk_rows(db) == before
    models = [r[0] for r in db.con.execute("SELECT DISTINCT model FROM embeddings")]
    assert models == ["새모델"], "옛 모델과 새 모델의 벡터가 섞였다"


def test_stopping_between_documents_leaves_no_half_built_document(db: Database, monkeypatch):
    """문서 하나의 조각은 한 트랜잭션이다 — 멈춰도 반쪽 문서가 남지 않는다."""
    source_id = db.add_source(r"D:\자료")
    for i in (1, 2, 3):
        _doc_with_sections(db, source_id, f"문서{i}.hwp",
                           [("1문단", f"문서{i} 가"), ("표1", f"문서{i} 나")], hash_=f"h{i}")
    import app.jobs.pipeline as pipeline_module
    monkeypatch.setattr(pipeline_module, "OllamaClient", _fake({"ready": False}))
    pipeline = Pipeline(db.path)
    real = db.replace_document_chunks

    def stop_after_first(doc_id, pieces):
        pipeline._stop = True
        return real(doc_id, pieces)

    monkeypatch.setattr(db, "replace_document_chunks", stop_after_first)
    pipeline._chunks(db)

    texts = [r[0] for r in db.con.execute("SELECT text FROM chunks ORDER BY ordinal")]
    assert db.con.execute("SELECT COUNT(DISTINCT doc_id) FROM chunks").fetchone()[0] == 1
    # 짧은 문단과 표는 한 조각으로 합쳐진다. 그 문서의 내용이 빠짐없이 들어 있어야 한다.
    assert "문서1 가" in "".join(texts) and "문서1 나" in "".join(texts)
    assert db.con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert len(db.documents_without_chunks()) == 2


# ── 실제 Ollama가 있을 때만 (선택) ──────────────────────────────────

def _ollama_ready() -> bool:
    if os.environ.get("NUNCHICOACH_LIVE_AI") != "1":
        return False
    return OllamaClient().health().embedding_ready


live = pytest.mark.skipif(
    not _ollama_ready(), reason="NUNCHICOACH_LIVE_AI=1 과 Ollama·bge-m3가 필요합니다"
)


@live
def test_live_chunks_are_embedded_and_queryable(db: Database):
    """청크 생성 → 임베딩 → RAG 검색까지 실제로 이어지는지 끝까지 확인한다."""
    from app.search.rag import ask

    source_id = db.add_source(r"D:\자료")
    _doc_with_sections(db, source_id, "행정사무감사_제출자료.hwp", [
        ("3쪽 2문단", "2024년 행정사무감사 수질 관련 제출자료입니다. "
                    "원수 수질, 정수장별 검사결과, 수질민원 현황을 포함합니다."),
    ])

    Pipeline(db.path)._chunks(db)

    n = db.con.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
    assert n >= 1

    answer = ask(db, "행정사무감사 때 수질 관련해서 뭘 제출했어?")
    assert not answer.withheld, answer.error
    assert answer.citations
    assert answer.citations[0].filename == "행정사무감사_제출자료.hwp"
