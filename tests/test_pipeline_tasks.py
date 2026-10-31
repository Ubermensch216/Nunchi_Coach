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


def test_analyze_by_tasks_sets_meta_flags_and_reports(sample_db: Database):
    db = sample_db
    t1 = db.con.execute("INSERT INTO tasks(name, origin, status) VALUES ('시설관리', 'ai', 'proposed')").lastrowid
    db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, 1, 'ai', 'high')", (t1,))
    db.con.commit()

    pipe = Pipeline(db.path)
    stages = []
    pipe.stage_done.connect(lambda r: stages.append(r.stage))

    pipe._analyze_by_tasks(db)

    # 메타 플래그 세팅 확인
    assert db.get_meta("cycles_checked") == "1"
    assert db.get_meta("steps_checked") == "1"
    assert db.get_meta("items_checked") == "1"

    # StageReport 방출 확인 (roles, cycles, steps, restore)
    from app.jobs.pipeline import STAGE_ROLES, STAGE_CYCLES, STAGE_STEPS, STAGE_RESTORE
    assert STAGE_ROLES in stages
    assert STAGE_CYCLES in stages
    assert STAGE_STEPS in stages
    assert STAGE_RESTORE in stages


def test_analyze_by_tasks_reanalyzes_on_doc_change(sample_db: Database):
    db = sample_db
    t1 = db.con.execute("INSERT INTO tasks(name, origin, status) VALUES ('시설관리', 'ai', 'proposed')").lastrowid
    db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, 1, 'ai', 'high')", (t1,))
    db.con.commit()

    pipe = Pipeline(db.path)
    emitted = []
    pipe.task_analyzed.connect(emitted.append)

    pipe._analyze_by_tasks(db)
    assert emitted == [t1]

    # 완료 후 다시 돌리면 스킵
    emitted.clear()
    pipe._analyze_by_tasks(db)
    assert len(emitted) == 0

    # 문서 mtime을 미래로 업데이트하면 stale 감지되어 재분석됨
    db.con.execute("UPDATE documents SET fs_mtime = '2099-01-01T00:00:00' WHERE id = 1")
    db.con.commit()

    pipe._analyze_by_tasks(db)
    assert emitted == [t1]


def test_single_task_cycles_evidence_is_doc_id_list(sample_db: Database):
    from app.ui.views.evidence import for_cycle
    db = sample_db
    t1 = db.con.execute("INSERT INTO tasks(name, origin, status) VALUES ('시설관리', 'ai', 'proposed')").lastrowid
    db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, 1, 'ai', 'high')", (t1,))
    db.con.commit()

    pipe = Pipeline(db.path)
    pipe._analyze_by_tasks(db)

    cycle = db.task_cycle(t1)
    if cycle:
        # evidence가 빈 문자열이거나 쉼표 구분 doc_id여야 하고 for_cycle 호출 시 에러가 없어야 함
        ev_list = for_cycle(db, cycle)
        assert isinstance(ev_list, list)


def test_analyze_by_tasks_reanalyzes_on_task_docs_membership_change(sample_db: Database):
    """업무 찾기 단계에서 문서가 새로 붙거나 빠져도(mtime 불변) stale로 감지되어 재분석된다."""
    db = sample_db
    t1 = db.con.execute("INSERT INTO tasks(name, origin, status) VALUES ('시설관리', 'ai', 'proposed')").lastrowid
    db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, 1, 'ai', 'high')", (t1,))
    db.con.commit()

    pipe = Pipeline(db.path)
    emitted = []
    pipe.task_analyzed.connect(emitted.append)

    pipe._analyze_by_tasks(db)
    assert emitted == [t1]

    # 완료 후 mtime 변경 없이 기존 2번 문서가 업무에 새로 추가됨
    emitted.clear()
    assert not pipe._is_task_stale(db, t1)

    db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, 2, 'ai', 'high')", (t1,))
    db.con.commit()

    # fingerprint가 달라져 stale: True가 됨!
    assert pipe._is_task_stale(db, t1)

    pipe._analyze_by_tasks(db)
    assert emitted == [t1]
    assert not pipe._is_task_stale(db, t1)


def test_edited_task_not_infinitely_reanalyzed(sample_db: Database):
    """사람이 고친 업무(status='edited')도 한 번 재분석된 후에는 무한 재분석되지 않는다."""
    db = sample_db
    t1 = db.con.execute(
        "INSERT INTO tasks(name, origin, status, reviewed_at) VALUES ('시설관리', 'user', 'edited', datetime('now'))"
    ).lastrowid
    db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, 1, 'ai', 'high')", (t1,))
    db.con.commit()

    pipe = Pipeline(db.path)
    emitted = []
    pipe.task_analyzed.connect(emitted.append)

    # 1회 분석
    pipe._analyze_by_tasks(db)
    assert emitted == [t1]
    assert not pipe._is_task_stale(db, t1)

    # 다시 돌려도 stale이 아니므로 재실행되지 않음
    emitted.clear()
    pipe._analyze_by_tasks(db)
    assert len(emitted) == 0

    # 사용자가 이후에 업무를 다시 편집하면(reviewed_at 갱신) 다시 stale: True
    db.con.execute("UPDATE tasks SET reviewed_at = datetime('now', '+1 minute') WHERE id = ?", (t1,))
    db.con.commit()
    assert pipe._is_task_stale(db, t1)

    pipe._analyze_by_tasks(db)
    assert emitted == [t1]


def test_cycles_report_note_counts_actual_cycles(sample_db: Database):
    """일정 단계 보고 문구가 전체 업무 수가 아니라 실제 발견된 반복 업무 수를 센다."""
    db = sample_db
    t1 = db.con.execute("INSERT INTO tasks(name, origin, status) VALUES ('시설관리', 'ai', 'proposed')").lastrowid
    t2 = db.con.execute("INSERT INTO tasks(name, origin, status) VALUES ('보안관리', 'ai', 'proposed')").lastrowid
    db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, 1, 'ai', 'high')", (t1,))
    db.con.execute("INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, 3, 'ai', 'high')", (t2,))
    db.con.commit()

    pipe = Pipeline(db.path)
    reports = []
    pipe.stage_done.connect(reports.append)

    pipe._analyze_by_tasks(db)

    cycle_rep = next(r for r in reports if r.stage == "일정 파악")
    found_count = db.con.execute("SELECT COUNT(DISTINCT task_id) FROM task_cycles WHERE kind != 'none'").fetchone()[0]
    assert cycle_rep.note == f"반복 업무 {found_count}개 발견"
