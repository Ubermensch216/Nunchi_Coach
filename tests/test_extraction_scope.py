from pathlib import Path

import openpyxl
import pymupdf
import pytest

from app.core import coverage
from app.core.notebook_ingest import write_parse
from app.db import Database
from app.ingest.parsers import pdf, xlsx
from app.ingest.parsers.base import EMPTY, OK, PARTIAL
from app.ingest.scanner import scan_source
from app.jobs.pipeline import Pipeline


def make_pdf(path, pages):
    with pymupdf.open() as doc:
        for text in pages:
            page = doc.new_page()
            if text:
                page.insert_text((72, 72), text)
        doc.save(path)


def make_xlsx(path, rows, second=False):
    wb = openpyxl.Workbook()
    for row in rows:
        wb.active.append(row)
    if second:
        wb.create_sheet('후속').append(['must be reported'])
    wb.save(path)
    wb.close()


def test_pdf_reports_truncated_page_and_unvisited_tail(tmp_path, monkeypatch):
    path = tmp_path / 'limit.pdf'
    make_pdf(path, ['A' * 60, 'B' * 60, 'C' * 60])
    before = path.read_bytes()
    monkeypatch.setattr(pdf, 'MAX_CHARS', 70)
    result = pdf.parse(path)
    assert result.status == PARTIAL
    assert result.char_count == 70
    assert result.coverage['pages_visited'] == 2
    assert result.coverage['pages_unvisited'] == 1
    assert result.coverage['omissions'][-1]['page_range'] == [3, 3]
    assert path.read_bytes() == before


def test_pdf_exact_budget_with_no_remaining_pages_is_not_truncation(tmp_path, monkeypatch):
    path = tmp_path / 'exact.pdf'
    make_pdf(path, ['A' * 60])
    monkeypatch.setattr(pdf, 'MAX_CHARS', 60)
    result = pdf.parse(path)
    assert result.status == OK
    assert result.coverage['omissions'] == []


def test_pdf_blank_pages_counted_even_when_other_pages_have_much_text(tmp_path):
    path = tmp_path / 'mixed.pdf'
    make_pdf(path, ['A' * 60, ''])
    result = pdf.parse(path)
    assert result.status == PARTIAL
    assert result.coverage['pages_textless'] == 1
    assert result.coverage['omissions'][0]['page_samples'] == [2]


def test_all_pdf_page_errors_never_become_successful_empty_sections(monkeypatch):
    class Page:
        def get_text(self, kind):
            raise RuntimeError('broken page')
    class Doc:
        needs_pass = False
        metadata = {}
        page_count = 2
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def __iter__(self): return iter([Page(), Page()])
    monkeypatch.setattr(pdf.pymupdf, 'open', lambda path: Doc())
    result = pdf.parse(Path('fake.pdf'))
    assert result.status == EMPTY and not result.sections
    assert result.coverage['pages_failed'] == 2
    assert result.coverage['pages_textless'] == 0


def test_xlsx_character_cap_reports_unvisited_sheet(tmp_path, monkeypatch):
    path = tmp_path / 'limit.xlsx'
    make_xlsx(path, [['A' * 50]], second=True)
    before = path.read_bytes()
    monkeypatch.setattr(xlsx, 'MAX_CHARS', 20)
    result = xlsx.parse(path)
    assert result.status == PARTIAL and result.char_count == 20
    assert result.coverage['sheets_total'] == 2
    assert result.coverage['sheets_visited'] == 1
    assert any(o['unit'] == 'unvisited_sheet' for o in result.coverage['omissions'])
    assert path.read_bytes() == before


def test_xlsx_cell_budget_never_exceeded_and_row_tail_is_explicit(tmp_path, monkeypatch):
    path = tmp_path / 'cells.xlsx'
    make_xlsx(path, [['a', 'b', 'c'], ['d', 'e', 'f'], ['g', 'h', 'i']])
    monkeypatch.setattr(xlsx, 'MAX_CELLS_PER_SHEET', 5)
    result = xlsx.parse(path)
    assert result.status == PARTIAL
    assert result.coverage['cells_visited'] == 5
    tail = next(o for o in result.coverage['omissions'] if o['unit'] == 'row_tail')
    assert (tail['row'], tail['first_column']) == (2, 3)
    assert 'f' not in result.text


def test_xlsx_exact_cell_boundary_without_extra_rows_is_ok(tmp_path, monkeypatch):
    path = tmp_path / 'exact.xlsx'
    make_xlsx(path, [['a', 'b'], ['c', 'd']])
    monkeypatch.setattr(xlsx, 'MAX_CELLS_PER_SHEET', 4)
    result = xlsx.parse(path)
    assert result.status == OK
    assert result.coverage['cells_visited'] == 4
    assert not result.coverage['omissions']


def test_xlsx_character_cap_between_sections_reports_remaining_rows(tmp_path, monkeypatch):
    path = tmp_path / 'rows.xlsx'
    make_xlsx(path, [['A' * 600], ['B' * 600], ['C' * 600]])
    monkeypatch.setattr(xlsx, 'MAX_CHARS', 620)
    result = xlsx.parse(path)
    assert result.status == PARTIAL and result.char_count == 620
    assert any(o['reason'] == 'character_limit' for o in result.coverage['omissions'])
    assert 'C' not in result.text


@pytest.mark.parametrize('writer', ['pipeline', 'notebook'])
def test_scope_persists_and_reparse_replaces_old_omissions(tmp_path, monkeypatch, writer):
    root = tmp_path / 'source'
    root.mkdir()
    path = root / 'scope.pdf'
    make_pdf(path, ['A' * 60, 'B' * 60])
    db = Database(tmp_path / 'project' / 'project.db')
    db.init()
    try:
        sid = db.add_source(root)
        scan_source(db, sid, root)
        doc_id = db.con.execute('SELECT id FROM documents').fetchone()[0]
        monkeypatch.setattr(pdf, 'MAX_CHARS', 70)
        if writer == 'pipeline':
            Pipeline(db.path)._parse(db)
        else:
            write_parse(db, doc_id, path.name, str(path), pdf.parse(path))
        db.close()
        report = coverage.snapshot(db, include_documents=True)
        assert report['extraction_scope_measured_documents'] == 1
        assert report['extraction_known_omission_documents'] == 1
        assert report['extraction_complete_documents'] is None
        assert report['documents'][0]['extraction_coverage']['pages_visited'] == 2
        monkeypatch.setattr(pdf, 'MAX_CHARS', 200)
        if writer == 'pipeline':
            db.update_document(doc_id, parse_status='pending')
            Pipeline(db.path)._parse(db)
        else:
            write_parse(db, doc_id, path.name, str(path), pdf.parse(path))
        assert coverage.snapshot(db)['extraction_known_omission_documents'] == 0
    finally:
        db.close()


def test_v10_upgrade_keeps_human_records_and_creates_backup(tmp_path):
    path = tmp_path / 'project.db'
    db = Database(path)
    db.init()
    db.con.execute("ALTER TABLE documents DROP COLUMN extraction_coverage")
    db.set_meta('schema_version', '10')
    db.con.execute("INSERT INTO tasks(name, origin) VALUES ('사용자 업무','user')")
    db.close()
    db.init()
    try:
        from app.db.migrations import SCHEMA_VERSION
        assert db.get_meta('schema_version') == SCHEMA_VERSION
        assert db.con.execute('SELECT name,origin FROM tasks').fetchone()[:] == ('사용자 업무', 'user')
        assert path.with_name('project.db.bak-v10').exists()
        assert coverage.snapshot(db)['extraction_scope_measured_documents'] == 0
    finally:
        db.close()


def test_readonly_legacy_diagnostics_do_not_require_new_column(tmp_path):
    db = Database(tmp_path / 'legacy.db')
    db.init()
    try:
        db.con.execute('ALTER TABLE documents DROP COLUMN extraction_coverage')
        db.set_meta('schema_version', '10')
        report = coverage.snapshot(db)
        assert report['schema_version'] == '10'
        assert report['extraction_scope_measured_documents'] == 0
    finally:
        db.close()


def test_interruption_between_scope_and_sections_rolls_back_and_can_resume(tmp_path, monkeypatch):
    root = tmp_path / 'source'
    root.mkdir()
    path = root / 'resume.pdf'
    make_pdf(path, ['A' * 60])
    original = path.read_bytes()
    db = Database(tmp_path / 'project' / 'project.db')
    db.init()
    try:
        scan_source(db, db.add_source(root), root)
        doc_id = db.con.execute('SELECT id FROM documents').fetchone()[0]
        with monkeypatch.context() as interrupted:
            def fail_after_metadata(*args, **kwargs):
                raise RuntimeError('simulated interruption after metadata write')
            interrupted.setattr(db, 'replace_sections', fail_after_metadata)
            with pytest.raises(RuntimeError, match='simulated interruption'):
                Pipeline(db.path)._parse(db)
        db.close()
        row = db.document(doc_id)
        assert row['parse_status'] == 'pending' and row['extraction_coverage'] == '{}'
        assert not db.sections(doc_id)
        Pipeline(db.path)._parse(db)
        assert db.document(doc_id)['parse_status'] == OK
        assert coverage.snapshot(db)['extraction_scope_measured_documents'] == 1
        assert db.sections(doc_id)
        assert path.read_bytes() == original
    finally:
        db.close()
