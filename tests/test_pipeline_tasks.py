"""S5: 업무 단위 집중 분석 파이프라인 테스트."""

import pytest
from pathlib import Path
from app.db import Database
from app.jobs.pipeline import Pipeline, _order_tasks_for_analysis
from app.core import survey


@pytest.fixture
def sample_db(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    db.init()
    db.con.execute("INSERT INTO sources(id, path) VALUES (1, 'c:/docs')")
    db.con.execute(
        "INSERT INTO documents(id, source_id, path, filename, ext, size, fs_mtime, eff_year, eff_month, eff_date, eff_date_kind, eff_precision, parse_status) "
        "VALUES (1, 1, 'c:/docs/시설관리/계획.hwp', '계획.hwp', '.hwp', 1000, '2023-01-01T00:00:00', 2023, 1, '2023-01-01', 'created', 'day', 'ok')"
    )
    db.con.execute(
        "INSERT INTO documents(id, source_id, path, filename, ext, size, fs_mtime, eff_year, eff_month, eff_date, eff_date_kind, eff_precision, parse_status) "
        "VALUES (2, 1, 'c:/docs/시설관리/결과.hwp', '결과.hwp', '.hwp', 2000, '2023-02-01T00:00:00', 2023, 2, '2023-02-01', 'created', 'day', 'ok')"
    )
    db.con.execute(
        "INSERT INTO documents(id, source_id, path, filename, ext, size, fs_mtime, eff_year, eff_month, eff_date, eff_date_kind, eff_precision, parse_status) "
        "VALUES (3, 1, 'c:/docs/보안/지침.hwp', '지침.hwp', '.hwp', 500, '2023-03-01T00:00:00', 2023, 3, '2023-03-01', 'created', 'day', 'ok')"
    )
    # 1번 문서를 directive(공문/시행문)으로 등록
    db.con.execute(
        "INSERT INTO doc_roles(doc_id, role, basis, confidence, decided_by) VALUES (1, 'directive', '시행문', 'high', 'ai')"
    )
    db.con.commit()
    return db


def test_order_tasks_for_analysis(sample_db: Database):
    db = sample_db
    t1 = db.con.execute("INSERT INTO tasks(name, origin, status) VALUES ('시설관리', 'ai', 'proposed')").lastrowid
    t2 = db.con.execute("INSERT INTO tasks(name, origin, status) VALUES ('보안관리', 'ai', 'proposed')").lastrowid

    db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, 1, 'ai', 'high')", (t1,))
    db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, 2, 'ai', 'high')", (t1,))
    db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, 3, 'ai', 'high')", (t2,))

    survey.build(db)

    db.con.execute("INSERT INTO charter_items(name, weight, doc_id) VALUES ('보안관리', 50, 3)")
    db.con.execute("INSERT INTO charter_items(name, weight, doc_id) VALUES ('시설관리', 10, 1)")
    db.con.commit()

    ordered = _order_tasks_for_analysis(db)
    assert len(ordered) == 2
    # 시설관리가 문서 2건 (더 많은 비중)으로 우선 정렬됨
    assert ordered[0]["name"] == "시설관리"
    assert ordered[1]["name"] == "보안관리"


def test_analyze_by_tasks_emits_signal_and_persists_state(sample_db: Database):
    db = sample_db
    t1 = db.con.execute("INSERT INTO tasks(name, origin, status) VALUES ('시설관리', 'ai', 'proposed')").lastrowid
    t2 = db.con.execute("INSERT INTO tasks(name, origin, status) VALUES ('보안관리', 'ai', 'proposed')").lastrowid

    db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, 1, 'ai', 'high')", (t1,))
    db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, 2, 'ai', 'high')", (t1,))
    db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, 3, 'ai', 'high')", (t2,))
    db.con.commit()

    pipe = Pipeline(db.path)
    emitted = []
    pipe.task_analyzed.connect(emitted.append)

    pipe._analyze_by_tasks(db)

    # 두 업무 모두 완료 신호 수신
    assert emitted == [t1, t2] or emitted == [t2, t1]
    assert len(emitted) == 2

    # task_analysis_state 확인
    states = db.con.execute("SELECT task_id, state FROM task_analysis_state ORDER BY task_id").fetchall()
    assert len(states) == 2
    assert states[0]["state"] == "completed"
    assert states[1]["state"] == "completed"

    # 이미 completed 상태에서 다시 돌리면 실행 스킵 (시그널 재방출 안 됨)
    emitted.clear()
    pipe._analyze_by_tasks(db)
    assert len(emitted) == 0
