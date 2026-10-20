"""매뉴얼과 시점별 기록의 분리, 내보내기 내용·출처·원문 보호를 검증한다."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from datetime import date
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from app.core import reconstruction as rc, report, handover_release
from app.core.output_policy import UnsafeOutputError
from app.db import Database
from app.ui import theme
from app.ui.views.handover import HandoverView


@pytest.fixture(scope='module')
def qapp():
    app = QApplication.instance() or QApplication([])
    theme.apply(app, 'medium', 'light')
    return app


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'project' / 'p.db')
    database.init()
    source = database.add_source(tmp_path / '자료')
    original = tmp_path / '자료' / '요청.txt'
    original.parent.mkdir(exist_ok=True)
    original.write_text('수정하면 안 되는 원본', encoding='utf-8')
    doc = database.upsert_document(source, dict(path=str(original), filename=original.name,
                    ext='.txt', hash='source', parse_status='ok', eff_year=2026))
    database.con.execute("INSERT INTO tasks(id,name) VALUES (1,'행정사무감사')")
    database.con.execute('INSERT INTO task_docs(task_id,doc_id) VALUES(1,?)', (doc,))
    database.add_task_item(1, rc.SCOPE, '가람시의회', slot='감사주체')
    database.add_task_item(1, rc.SCOPE, '2025.11.1.~2026.10.31.', slot='감사대상기간')
    output = database.add_task_item(1, rc.OUTPUT, '감사 제출자료')
    database.add_task_item(1, rc.RECIPIENT, '기획조정실')
    database.con.execute("INSERT INTO evidence(owner_kind,owner_id,doc_id,doc_hash,locator,quote) "
                         "VALUES ('item',?,?,'source','1문단','제출자료 원문')", (output, doc))
    yield database
    database.close()


def test_manual_contains_instruction_facts_preparation_outputs_and_evidence(db):
    before = db.con.total_changes
    document = report.build_manual(db, '환경국', date(2026, 10, 20))
    assert db.con.total_changes == before
    for text in ('환경국 인수인계 매뉴얼', '2026-10-20', '가람시의회', '2025.11.1.~2026.10.31.',
                 '감사 제출자료', '기획조정실', '요청.txt', '1문단', '제출자료 원문',
                 '후임자 확인', '준비 제안', '| 단계 | 시점 | 할 일 | 산출물·완료 기준 | 확인 상태 |'):
        assert text in document
    assert '원문 파일은 포함하지 않습니다' in document


def test_handover_preview_saves_same_content_and_does_not_create_a_snapshot(db, qapp, tmp_path):
    view = HandoverView(db, '환경국')
    try:
        view.refresh()
        assert not db.snapshots()
        assert view.export_button.isEnabled()
        out = tmp_path / 'manual.md'
        view.write_manual(str(out))
        assert out.read_text(encoding='utf-8-sig') == view.document_text
        assert out.read_bytes().startswith(b'\xef\xbb\xbf')
        assert db.recent_audit(1)[0]['action'] == 'handover.manual.export'
        original = db.document(1)['path']
        with pytest.raises(UnsafeOutputError):
            view.write_manual(original)
        assert Path(original).read_text(encoding='utf-8') == '수정하면 안 되는 원본'
    finally:
        view.deleteLater()


def test_archive_shows_saved_state_while_manual_uses_current_knowledge(db, qapp, tmp_path):
    saved, _ = handover_release.save(db)
    item = next(i for i in db.task_items(1) if i['kind'] == rc.RECIPIENT)
    db.review_task_item(item['id'], 'edit', '변경된 수신처', '후임자')
    view = HandoverView(db)
    try:
        view.refresh()
        assert '변경된 수신처' in view.document_text
        view._switch('archive')
        assert view.archive.selected_id() == saved
        target = tmp_path / 'saved.md'
        view.archive.export(saved, str(target))
        text = target.read_text(encoding='utf-8-sig')
        assert '기획조정실' in text and '변경된 수신처' not in text
        assert '닫기' not in [button.text() for button in view.archive.findChildren(QPushButton)]
    finally:
        view.deleteLater()


def test_empty_project_can_open_archive_without_creating_a_manual(qapp, tmp_path):
    database = Database(tmp_path / 'empty.db')
    database.init()
    view = HandoverView(database)
    try:
        view.refresh()
        assert not view.export_button.isEnabled()
        assert '아직 파악된 업무가 없습니다' in view.document_text
        view._switch('archive')
        assert view.archive.list.count() == 0
        assert view.archive.save_button.isEnabled()
    finally:
        view.deleteLater()
        database.close()


def test_manual_preview_preserves_multiline_tables_and_all_past_steps(db, qapp):
    db.add_task_item(1, rc.SCOPE, '첫 줄 대상\n둘째 줄 대상', slot='감사대상기관')
    for n in range(1, 12):
        db.con.execute(
            "INSERT INTO work_steps(task_id,slot,ordinal,label,output) VALUES(1,?,?,?,?)",
            (f'past-{n}', n, f'사례 단계 {n}', f'결과 파일 {n}.xlsx'))
    view = HandoverView(db)
    try:
        view.refresh()
        text = view.preview.toPlainText()
        for expected in ('첫 줄 대상', '둘째 줄 대상', '근거·빠진 정보', '제출자료 원문'):
            assert expected in text
        for n in range(1, 12):
            assert f'{n}. 사례 단계 {n}' in text
            assert f'결과 파일 {n}.xlsx' in text
        block = view.preview.document().begin()
        while block.isValid():
            if block.textList():
                assert block.text().strip(), '미리보기에 빈 목록 항목이 생겼습니다'
            block = block.next()
    finally:
        view.deleteLater()


def test_manual_preview_preserves_quotes_and_literal_markup(db, qapp, tmp_path):
    note = "'건 확인·고치기'에서 \"우리 기관\" 확인 · <자료> & A | B · **원문**"
    db.add_task_item(1, rc.RISK, note)
    view = HandoverView(db)
    try:
        view.refresh()
        text = view.preview.toPlainText()
        assert note in text
        assert '&#x27;' not in text
        assert '&lt;' not in text and '&amp;' not in text
        path = tmp_path / 'quoted-manual.md'
        view.write_manual(str(path))
        assert path.read_text(encoding='utf-8-sig') == view.document_text
    finally:
        view.deleteLater()
