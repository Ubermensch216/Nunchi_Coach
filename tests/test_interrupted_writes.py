"""작업 경계에서 죽거나 멈춘 뒤에도 문서 상태가 사실과 어긋나지 않는가 (B07).

프로세스 강제 종료는 '쓰던 묶음이 확정되지 않고 사라지는 것'이다. 여기서는
묶음 한가운데에서 예외를 던져 같은 결과를 만든다(실제 프로세스를 죽이는
시험은 test_crash_resume.py에 있다).
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app.jobs.pipeline as pipeline_module  # noqa: E402
from app.ai.client import Health  # noqa: E402
from app.db import Database  # noqa: E402
from app.ingest.scanner import scan_source  # noqa: E402
from app.jobs.pipeline import Pipeline  # noqa: E402


class Killed(BaseException):
    """파이프라인의 예외 처리에 잡히지 않는, 프로세스 종료의 대역."""


@pytest.fixture
def project(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "report.txt").write_text("2025-09-01 실적 보고 예산 820", encoding="utf-8")
    db = Database(tmp_path / "p.db")
    db.init()
    scan_source(db, db.add_source(source), source)
    yield db, Pipeline(db.path), source
    db.close()


def state(db):
    return db.con.execute("SELECT parse_status, hash, char_count FROM documents").fetchone()


def count(db, table):
    return db.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@pytest.mark.parametrize("boundary", ["replace_sections", "index_document"])
def test_death_in_the_middle_of_one_document_leaves_it_unread(project, monkeypatch, boundary):
    db, worker, _source = project
    worker._hash(db)

    def die(*args, **kwargs):
        raise Killed()
    monkeypatch.setattr(db, boundary, die)
    with pytest.raises(Killed):
        worker._parse(db)

    # '읽음' 표시만 남고 본문이 없는 상태가 되면 다시 읽을 계기가 없다.
    assert state(db)["parse_status"] == "pending"
    assert count(db, "document_sections") == 0 and count(db, "document_index") == 0

    monkeypatch.undo()
    worker._parse(db)
    assert state(db)["parse_status"] == "ok"
    assert count(db, "document_sections") == 1 and count(db, "document_index") == 1


def test_document_is_never_ready_without_a_hash(project, monkeypatch):
    db, worker, _source = project
    # 해시 단계를 건너뛰고 바로 읽어도 해시가 함께 남는다.
    monkeypatch.setattr(pipeline_module, "sha256", lambda path: (None, "다른 프로그램이 사용 중"))
    reports = []
    worker.stage_done.connect(reports.append)
    worker._parse(db)
    assert state(db)["parse_status"] == "pending" and state(db)["hash"] is None
    assert count(db, "document_index") == 0
    assert "중복 확인을 못 해 건너뜀" in reports[-1].errors[0]

    monkeypatch.undo()
    worker._parse(db)
    assert state(db)["parse_status"] == "ok" and len(state(db)["hash"]) == 64


def test_half_written_documents_from_an_older_run_are_read_again(project):
    db, worker, _source = project
    worker._hash(db)
    worker._parse(db)
    worker._date(db)
    db.set_document_date(1, "2024-07-02", "day")
    # 예전 판이 남긴 흔적: '읽음'인데 검색 색인이 없다.
    db.con.execute("DELETE FROM document_index")
    assert db.repair_interrupted() == 1
    assert state(db)["parse_status"] == "pending"
    assert db.document(1)["eff_date"] == "2024-07-02", "사람이 고른 날짜는 지우지 않는다"
    assert db.con.execute(
        "SELECT result FROM audit_logs WHERE action='integrity.repair'").fetchone()[0] == "reparse"

    worker._parse(db)
    assert state(db)["parse_status"] == "ok" and count(db, "document_index") == 1
    assert db.repair_interrupted() == 0


def test_repair_leaves_sound_and_missing_documents_alone(project):
    db, worker, source = project
    worker._hash(db)
    worker._parse(db)
    assert db.repair_interrupted() == 0

    db.con.execute("DELETE FROM document_sections")
    db.mark_missing([1])
    # 원본이 없으면 다시 읽을 수 없다 — 상태만 되돌리면 남은 기록까지 잃는다.
    assert db.repair_interrupted() == 0
    assert state(db)["parse_status"] == "ok"


def test_stopped_stages_do_not_claim_they_finished(project, monkeypatch):
    db, worker, _source = project
    worker._hash(db)
    worker._parse(db)
    db.con.execute("INSERT INTO tasks(id, name) VALUES (1, '실적보고')")
    db.assign_document(1, 1)

    class Client:
        embed_model, gen_model = "bge-m3", "gemma4:e2b"

        def __init__(self, **kwargs):
            pass

        def health(self):
            return Health(True, "ready", embedding_ready=True)

        def cancel(self):
            pass

    monkeypatch.setattr(pipeline_module, "OllamaClient", Client)
    worker.stop()
    worker._cycles(db)
    worker._steps(db)
    worker._chunks(db)
    for flag in ("cycles_checked", "steps_checked", "chunks_checked"):
        assert db.get_meta(flag) is None, f"{flag}: 멈춘 단계를 끝났다고 적었다"

    worker._stop = False
    worker._cycles(db)
    worker._steps(db)
    assert db.get_meta("cycles_checked") == "1" and db.get_meta("steps_checked") == "1"
