"""눈치코치가 파악한 업무를 노트북의 자료로 — 정리본 만들기·넣기·바뀜 알리기·고르는 창."""

import hashlib
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from app.core import notebook, notebook_tasks
from app.db import Database
from app.jobs.notebook_ingest_task import IngestWorker
from app.ui.views.notebooks.sources_panel import BRIEF_NOTE, STALE_BRIEF, SourcesPanel
from app.ui.views.notebooks.task_picker import TaskSourceDialog


class Offline:
    """모델이 없는 PC. 조각과 글자 검색까지만 준비된다."""
    embed_model = "none"

    def health(self):
        return SimpleNamespace(embedding_ready=False, generation_ready=False, message="offline")

    def _supports(self, capability):
        return False


@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path / "project" / "project.db")
    db.init()
    yield db
    db.close()


@pytest.fixture
def work(db, tmp_path):
    """업무 하나와 그 문서 둘, 주기·항목·근거·먼저 읽을 문서까지 파악된 상태."""
    folder = tmp_path / "전임자업무"
    folder.mkdir()
    source = db.add_source(folder)
    docs = []
    for name, text in (("2025 감사 요구자료 제출.txt", "제출기한: 9월 30일까지 의회사무국으로 제출한다."),
                       ("2024 감사 계획.txt", "행정사무감사 계획을 알린다.")):
        path = folder / name
        path.write_text(text, encoding="utf-8")
        doc = db.upsert_document(source, {
            "path": str(path), "filename": name, "ext": ".txt", "parse_status": "ok",
            "hash": hashlib.sha256(path.read_bytes()).hexdigest(),
            "eff_year": int(name[:4]), "eff_date": f"{name[:4]}-09-01"})
        db.replace_document_chunks(doc, [(1, "1문단", text)])
        docs.append(doc)
    con = db.con
    task = con.execute("INSERT INTO tasks(name, description) VALUES "
                       "('행정사무감사 자료 제출', '의회가 요구한 감사 자료를 모아 제출한다.')").lastrowid
    con.executemany("INSERT INTO task_docs(task_id, doc_id) VALUES (?,?)", [(task, d) for d in docs])
    con.execute("INSERT INTO task_cycles(task_id, kind, months, years_observed, confidence) "
                "VALUES (?, 'yearly', '9,10,11', 3, 'high')", (task,))
    con.execute("INSERT INTO task_steps(task_id, year, ordinal, label, month, day_hint, doc_id) "
                "VALUES (?, 2025, 1, '요구자료 제출', 9, '9월 초', ?)", (task, docs[0]))
    con.execute("INSERT INTO task_reading(task_id, doc_id, ordinal, reason) "
                "VALUES (?, ?, 1, '가장 최근 제출 공문')", (task, docs[0]))
    item = con.execute("INSERT INTO task_items(task_id, kind, slot, content) "
                       "VALUES (?, 'deadline', '제출기한', '9월 30일')", (task,)).lastrowid
    con.execute("INSERT INTO evidence(owner_kind, owner_id, doc_id, doc_hash, locator, quote) "
                "VALUES ('item', ?, ?, (SELECT hash FROM documents WHERE id=?), '1문단', '제출기한: 9월 30일까지')",
                (item, docs[0], docs[0]))
    book = notebook.create(db, "감사 준비")
    return SimpleNamespace(task=task, docs=docs, book=book, folder=folder)


# ── 정리본 ──────────────────────────────────────────────────────────

def test_brief_carries_what_was_organized_with_its_basis(db, work):
    text = notebook_tasks.brief_markdown(db, work.task)
    assert "원문 문서가 아니라" in text                                  # 정리한 글임을 머리에 밝힌다
    assert "의회가 요구한 감사 자료를 모아 제출한다." in text
    assert "매년 9~11월 (자료에서 추정 · 3년 확인 · 신뢰도 높음)" in text
    assert "기한 · 제출기한: 9월 30일 (문서에 명시" in text and "사람 확인 없음" in text
    assert "근거: 2025 감사 요구자료 제출.txt · 1문단 “제출기한: 9월 30일까지”" in text
    assert "2025년 1번째: 9월 초 — 요구자료 제출 (문서: 2025 감사 요구자료 제출.txt)" in text
    assert "가장 최근 제출 공문" in text
    assert "◐" not in text                                              # 화면의 뱃지 기호는 싣지 않는다
    # 한 줄이 한 문단이다. 검색은 몇 줄만 떼어 보므로 줄마다 어느 업무의 것인지 적혀 있어야 한다.
    lines = text.strip().split("\n\n")
    assert all("\n" not in line for line in lines)
    assert len(lines) >= 10 and all("행정사무감사 자료 제출" in line for line in lines)


def test_brief_is_the_same_text_until_the_task_changes(db, work):
    first = notebook_tasks.brief_digest(db, work.task)
    assert notebook_tasks.brief_digest(db, work.task) == first
    db.edit_task_description(work.task, "감사 자료를 9월 말까지 낸다.")
    assert notebook_tasks.brief_digest(db, work.task) != first


def test_only_tasks_in_use_can_become_sources(db, work):
    db.mark_not_a_task(work.task)
    with pytest.raises(ValueError, match="사용 중인 업무"):
        notebook_tasks.add_brief(db, work.book, work.task, Offline())
    assert notebook_tasks.choices(db) == []


# ── 노트북에 넣기 ───────────────────────────────────────────────────

def test_brief_becomes_a_selected_source_that_answers_questions(db, work):
    outcome = notebook_tasks.add_brief(db, work.book, work.task, Offline())
    assert outcome.status == "ok" and outcome.filename == "업무 정리본 - 행정사무감사 자료 제출.md"
    source, = notebook.sources(db, work.book)
    assert (source["origin"], source["task_id"], source["selected"]) == ("task", work.task, 1)
    assert str(outcome.doc_id) in notebook.selected_snapshot(db, work.book)
    assert notebook_tasks.linked_tasks(db, work.book) == {work.task}

    _message, answer = notebook.query(db, work.book, "행정사무감사 제출기한은 언제야?", Offline())
    assert answer.citations and {c.doc_id for c in answer.citations} == {outcome.doc_id}
    assert any("9월 30일" in c.snippet for c in answer.citations)


def test_brief_stays_out_of_task_discovery_and_the_source_folder(db, work):
    before = {p.name for p in work.folder.iterdir()}
    outcome = notebook_tasks.add_brief(db, work.book, work.task, Offline())
    row = db.document(outcome.doc_id)
    assert {p.name for p in work.folder.iterdir()} == before             # 전임자 폴더에 쓰지 않는다
    assert row["source_id"] not in {s["id"] for s in db.work_sources()}   # 다시 업무로 묶이지 않는다
    assert not db.con.execute("SELECT 1 FROM task_docs WHERE doc_id=?", (outcome.doc_id,)).fetchone()


def test_same_task_in_two_notebooks_shares_one_document(db, work):
    first = notebook_tasks.add_brief(db, work.book, work.task, Offline())
    other = notebook.create(db, "다른 노트북")
    second = notebook_tasks.add_brief(db, other, work.task, Offline())
    assert (second.status, second.doc_id) == ("reused", first.doc_id)
    assert notebook.sources(db, other)[0]["origin"] == "task"
    copy = notebook.duplicate(db, work.book)
    assert notebook_tasks.linked_tasks(db, copy) == {work.task}


def test_changed_task_is_flagged_and_replaced_keeping_the_choice(db, work):
    old = notebook_tasks.add_brief(db, work.book, work.task, Offline())
    assert notebook_tasks.outdated(db, work.book) == {}
    notebook.select(db, work.book, old.doc_id, False)
    db.edit_task_description(work.task, "감사 자료를 9월 말까지 낸다.")
    assert notebook_tasks.outdated(db, work.book) == {old.doc_id: work.task}

    new = notebook_tasks.add_brief(db, work.book, work.task, Offline())
    source, = notebook.sources(db, work.book)
    assert new.doc_id != old.doc_id and source["doc_id"] == new.doc_id
    assert not source["selected"]                                        # 빼 둔 정리본을 다시 근거로 넣지 않는다
    assert notebook_tasks.outdated(db, work.book) == {}
    assert db.document(old.doc_id) is not None                           # 옛 판은 보관소에 남는다


def test_task_documents_link_as_originals_without_duplicates(db, work):
    assert notebook_tasks.add_documents(db, work.book, work.docs) == 2
    assert notebook_tasks.add_documents(db, work.book, work.docs) == 0
    assert {(s["doc_id"], s["origin"]) for s in notebook.sources(db, work.book)} == {
        (doc, "project") for doc in work.docs}


def test_worker_makes_briefs_without_summarizing_them(db, work, monkeypatch):
    from app.jobs import notebook_ingest_task

    monkeypatch.setattr(notebook_ingest_task, "project_client", lambda db, kind: Offline())
    monkeypatch.setattr(notebook_ingest_task.notebook_ingest, "summarize_source",
                        lambda *a: pytest.fail("정리본을 다시 요약하면 안 된다"))
    results = []
    worker = IngestWorker(db.path, work.book, [], [], tasks=[work.task, 9999])
    worker.done.connect(results.extend)
    worker.run()
    assert [r.status for r in results] == ["ok", "rejected"]
    assert "사용 중인 업무" in results[1].error


# ── 화면 ────────────────────────────────────────────────────────────

@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def _children(dialog):
    top = dialog.tree.topLevelItem(0)
    return top, [top.child(i) for i in range(top.childCount())]


def test_picker_takes_the_whole_task_or_just_parts(db, work, qapp):
    dialog = TaskSourceDialog(db, set(), set())
    top, children = _children(dialog)
    assert "행정사무감사 자료 제출" in top.text(0) and "문서 2건" in top.text(0)
    assert children[0].text(0).startswith("업무 정리본") and len(children) == 3
    assert dialog.picked() == ([], [])
    assert not dialog.buttons.buttons()[0].isEnabled()

    top.setCheckState(0, Qt.CheckState.Checked)                           # 업무를 고르면 전부
    assert dialog.picked() == ([work.task], work.docs)
    children[2].setCheckState(0, Qt.CheckState.Unchecked)                 # 펼쳐서 하나를 뺀다
    assert dialog.picked() == ([work.task], work.docs[:1])
    assert top.checkState(0) == Qt.CheckState.PartiallyChecked
    assert "업무 정리본 1건 · 문서 1건" in dialog.summary.text()

    dialog.none_button.click()
    assert dialog.picked() == ([], [])
    dialog.search.setText("없는 업무")
    assert top.isHidden()
    dialog.all_button.click()                                             # 보이는 업무만 고른다
    assert dialog.picked() == ([], [])
    dialog.search.setText("요구자료")                                      # 파일 이름으로도 찾는다
    dialog.all_button.click()
    assert dialog.picked() == ([work.task], work.docs)
    dialog.deleteLater()


def test_picker_locks_what_is_already_in_the_notebook(db, work, qapp):
    dialog = TaskSourceDialog(db, {work.docs[0]}, {work.task})
    top, children = _children(dialog)
    assert children[0].isDisabled() and children[1].isDisabled() and not children[2].isDisabled()
    assert "이미 추가함" in children[0].text(0)
    dialog.all_button.click()
    assert dialog.picked() == ([], [work.docs[1]])                        # 이미 넣은 것은 다시 넣지 않는다
    dialog.deleteLater()


def test_picker_says_so_when_no_task_has_been_found_yet(db, qapp):
    dialog = TaskSourceDialog(db, set(), set())
    assert "아직 파악한 업무가 없습니다" in dialog.summary.text()
    assert not dialog.buttons.buttons()[0].isEnabled()
    dialog.deleteLater()


@pytest.fixture
def panel(db, work, qapp):
    panel = SourcesPanel(db)
    yield panel
    panel._ingest.stop()
    panel.deleteLater()
    qapp.processEvents()


def test_sources_panel_offers_tasks_and_labels_the_brief(db, work, panel):
    assert "눈치코치가 파악한 업무에서 고르기…" in [a.text() for a in panel.add_menu.actions()]
    brief = notebook_tasks.add_brief(db, work.book, work.task, Offline())
    panel.set_notebook(work.book, True)
    panel.add_tasks([], work.docs[:1])                                    # 문서만 고른 경우 — 바로 연결된다
    assert panel.source_list.count() == 2 and "문서 1건을 연결" in panel.hint.text()
    item = next(panel.source_list.item(i) for i in range(2)
                if panel.source_list.item(i).data(Qt.ItemDataRole.UserRole) == brief.doc_id)
    assert "눈치코치가 정리한 업무" in item.toolTip()
    panel.show_document(brief.doc_id)
    assert BRIEF_NOTE in [label.text() for label in panel.findChildren(QLabel)]


def test_sources_panel_flags_a_brief_whose_task_changed(db, work, panel):
    brief = notebook_tasks.add_brief(db, work.book, work.task, Offline())
    panel.set_notebook(work.book, True)
    panel.source_list.setCurrentRow(0)
    assert panel.accept_button.isHidden()
    db.edit_task_description(work.task, "감사 자료를 9월 말까지 낸다.")
    panel.set_notebook(work.book, True)                                   # 노트북을 다시 열 때 잰다
    item = panel.source_list.item(0)
    assert item.data(Qt.ItemDataRole.UserRole) == brief.doc_id and item.text().endswith(STALE_BRIEF)
    panel.source_list.setCurrentRow(0)
    assert panel.accept_button.text() == "새 정리본 사용" and panel.accept_button.isEnabled()
    panel.set_notebook(work.book, False)                                  # 보관한 노트북은 고칠 수 없으니 묻지 않는다
    assert not panel.source_list.item(0).text().endswith(STALE_BRIEF)
