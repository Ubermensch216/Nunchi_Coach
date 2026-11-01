"""업무 카드 → 노트북 자동 연결 및 AI 업무 매뉴얼 시험.

- 분석 완료 후 적격 업무 대상 노트북 자동 생성
- 모델 없이도 규칙 기반 정리본 및 원문 문서 연결
- 업무 아님(not_a_task) 처리 시 노트북 보관(archived=1) 및 복원
- 중복 생성 방지 및 재분석 시 내용 불변 확인
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.core import notebook, notebook_tasks
from app.db import Database


@pytest.fixture
def db(tmp_path: Path):
    db_file = tmp_path / "project.db"
    database = Database(db_file)
    database.init()
    source = database.add_source(tmp_path)

    # 1. 적격 업무: 문서 2건 이상, origin='ai', status='proposed'
    for i in (1, 2):
        f = tmp_path / f"예산_{i}.txt"
        f.write_text(f"예산 편성 지침 {i}", encoding="utf-8")
        doc_id = database.upsert_document(source, {
            "path": str(f), "filename": f.name, "ext": ".txt",
            "hash": hashlib.sha256(f.read_bytes()).hexdigest(),
            "parse_status": "ok", "eff_year": 2026,
        })
        database.replace_document_chunks(doc_id, [(1, "1문단", f"예산 편성 지침 {i}")])

    t1_id = database.con.execute(
        "INSERT INTO tasks(name, origin, status, not_a_task) VALUES ('예산 편성', 'ai', 'proposed', 0)"
    ).lastrowid

    database.con.execute("INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, 1, 'ai', 'high')", (t1_id,))
    database.con.execute("INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, 2, 'ai', 'high')", (t1_id,))

    # 2. 부적격 업무: 문서 1건만 있음
    f3 = tmp_path / "잡무.txt"
    f3.write_text("잡무 내용", encoding="utf-8")
    doc3_id = database.upsert_document(source, {
        "path": str(f3), "filename": f3.name, "ext": ".txt",
        "hash": hashlib.sha256(f3.read_bytes()).hexdigest(),
        "parse_status": "ok", "eff_year": 2026,
    })
    t2_id = database.con.execute(
        "INSERT INTO tasks(name, origin, status, not_a_task) VALUES ('단일 문서 업무', 'ai', 'proposed', 0)"
    ).lastrowid
    database.con.execute("INSERT INTO task_docs(task_id, doc_id, origin, confidence) VALUES (?, ?, 'ai', 'high')", (t2_id, doc3_id))

    return database


def test_auto_generate_notebooks(db: Database):
    # 자동 생성 실행 (모델 없이 동작)
    created_ids = notebook_tasks.auto_generate_for_tasks(db)

    # 문서 2건 이상인 '예산 편성'만 생성되어야 함
    assert len(created_ids) == 1
    nb_id = created_ids[0]

    nb = notebook.require(db, nb_id)
    assert nb["title"] == "예산 편성"
    assert nb["task_id"] is not None

    # 노트북에 업무 문서 및 정리본이 들어있는지 확인
    sources = notebook.sources(db, nb_id)
    filenames = [s["filename"] for s in sources]
    # '업무 정리본 - 예산 편성.md' 가 포함되어야 함
    assert any("업무 정리본" in fn for fn in filenames)
    # 원문 문서 2건도 연결되어야 함
    assert any("예산_1.txt" in fn for fn in filenames)
    assert any("예산_2.txt" in fn for fn in filenames)


def test_reanalysis_does_not_duplicate(db: Database):
    # 첫 생성
    created_ids1 = notebook_tasks.auto_generate_for_tasks(db)
    assert len(created_ids1) == 1

    # 두 번째 실행 시 새로 생성되지 않고 기존 것 유지
    created_ids2 = notebook_tasks.auto_generate_for_tasks(db)
    assert len(created_ids2) == 0

    books = [b for b in notebook.overview(db) if not b["archived"]]
    assert len(books) == 1


def test_not_a_task_archives_notebook(db: Database):
    created_ids = notebook_tasks.auto_generate_for_tasks(db)
    nb_id = created_ids[0]

    # 업무 조회
    t_id = db.con.execute("SELECT id FROM tasks WHERE name='예산 편성'").fetchone()[0]

    # '업무 아님' 처리
    db.mark_not_a_task(t_id, flag=True)

    nb = db.con.execute("SELECT archived FROM notebooks WHERE id=?", (nb_id,)).fetchone()
    assert nb["archived"] == 1

    # 복원 처리
    db.mark_not_a_task(t_id, flag=False)
    nb = db.con.execute("SELECT archived FROM notebooks WHERE id=?", (nb_id,)).fetchone()
    assert nb["archived"] == 0

def test_suggested_questions(db: Database):
    t_id = db.con.execute("SELECT id FROM tasks WHERE name='예산 편성'").fetchone()[0]
    questions = notebook_tasks.suggested_questions(db, t_id)
    assert len(questions) == 5
    assert any("기한" in q or "제출" in q for q in questions)
    assert any("서식" in q or "양식" in q or "자료" in q for q in questions)


def test_chat_panel_shows_suggested_questions(db: Database, monkeypatch):
    """빈 대화에 추천 질문 단추가 실제로 그려진다.

    예전에는 잘못된 상대 경로 import가 조용히 삼켜져 단추가 한 번도 나오지 않았다.
    """
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QPushButton
    from app.jobs.ask_task import WarmupRunner
    from app.ui.views.notebooks.chat_panel import ChatPanel

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(WarmupRunner, "start", lambda self: False)
    nb_id = notebook_tasks.auto_generate_for_tasks(db)[0]
    t_id = db.con.execute("SELECT id FROM tasks WHERE name='예산 편성'").fetchone()[0]
    expected = notebook_tasks.suggested_questions(db, t_id)

    panel = ChatPanel(db)
    try:
        panel.set_notebook(nb_id, editable=True)
        app.processEvents()
        shown = {button.text() for button in panel.findChildren(QPushButton)}
        assert set(expected) <= shown
    finally:
        panel._runner.stop()
        panel._warmup.stop()
        panel.deleteLater()
        app.processEvents()


def test_version_notice_after_reanalysis_changes_the_brief(db: Database):
    """재분석으로 정리본 판이 오르면 다음에 열 때 '판 n → n+1'을 한 번 알린다."""
    nb_id = notebook_tasks.auto_generate_for_tasks(db)[0]
    t_id = db.con.execute("SELECT id FROM tasks WHERE name='예산 편성'").fetchone()[0]
    assert notebook_tasks.version_notice(db, nb_id) == ""        # 처음 여는 업무노트

    db.add_task_item(t_id, "deadline", "매년 3월 15일까지 제출")
    assert notebook_tasks.open_for_task(db, t_id).refreshed      # 분석 뒤 파이프라인이 하는 일
    notice = notebook_tasks.version_notice(db, nb_id)
    assert "판 1 → 2" in notice

    notebook_tasks.mark_version_seen(db, nb_id)
    assert notebook_tasks.version_notice(db, nb_id) == ""


def test_answer_applied_to_the_task_appears_in_the_next_brief(db: Database):
    nb_id = notebook_tasks.auto_generate_for_tasks(db)[0]
    t_id = db.con.execute("SELECT id FROM tasks WHERE name='예산 편성'").fetchone()[0]

    item_id = notebook_tasks.apply_answer(db, nb_id, "recipient", "기획예산과 예산팀")
    item = db.con.execute("SELECT * FROM task_items WHERE id=?", (item_id,)).fetchone()
    assert item["task_id"] == t_id and item["origin"] == "user"
    assert "기획예산과 예산팀" in notebook_tasks.brief_markdown(db, t_id)
    assert t_id in set(notebook_tasks.outdated(db, nb_id).values())


def test_apply_answer_needs_a_task_notebook(db: Database):
    plain = notebook.create(db, "그냥 메모")
    with pytest.raises(ValueError):
        notebook_tasks.apply_answer(db, plain, "note", "내용")


def test_workspace_shows_the_version_notice_once(db: Database, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from app.jobs.ask_task import WarmupRunner
    from app.ui.views.notebooks.workspace import NotebookWorkspace

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(WarmupRunner, "start", lambda self: False)
    nb_id = notebook_tasks.auto_generate_for_tasks(db)[0]
    t_id = db.con.execute("SELECT id FROM tasks WHERE name='예산 편성'").fetchone()[0]
    notebook_tasks.version_notice(db, nb_id)                       # 처음 열었다
    db.add_task_item(t_id, "deadline", "매년 3월 15일까지 제출")
    notebook_tasks.open_for_task(db, t_id)

    view = NotebookWorkspace(db)
    try:
        view.open(nb_id)
        assert "판 1 → 2" in view.banner.label.text() and not view.banner.isHidden()
        view.open(nb_id)
        assert view.banner.isHidden()
    finally:
        view.chat._runner.stop()
        view.chat._warmup.stop()
        view.deleteLater()
        app.processEvents()
