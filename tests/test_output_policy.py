from pathlib import Path
import os

import pytest

from app.db import Database
from app.core.output_policy import UnsafeOutputError, safe_text_writer


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "project" / "project.db")
    database.init()
    yield database
    database.close()


def test_registered_inputs_and_new_files_under_source_are_protected(db, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    original = source / "original.md"
    original.write_text("원본", encoding="utf-8")
    db.add_source(str(source))
    for target in (original, source / "new.csv", source / "sub" / "new.md"):
        with pytest.raises(UnsafeOutputError):
            with safe_text_writer(target, db) as handle:
                handle.write("changed")
    assert original.read_text(encoding="utf-8") == "원본"
    assert not (source / "sub").exists()


def test_hard_link_to_a_document_is_protected(db, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    original = source / "original.md"
    original.write_text("원본", encoding="utf-8")
    source_id = db.add_source(str(source))
    db.upsert_document(source_id, {"path": str(original), "filename": original.name, "ext": ".md"})
    alias = tmp_path / "alias.md"
    os.link(original, alias)
    with pytest.raises(UnsafeOutputError):
        with safe_text_writer(alias, db):
            pass
    assert original.read_text(encoding="utf-8") == "원본"


def test_failed_export_keeps_previous_output_and_cleans_temporary_file(db, tmp_path):
    target = tmp_path / "report.md"
    target.write_text("previous", encoding="utf-8")
    with pytest.raises(RuntimeError):
        with safe_text_writer(target, db) as handle:
            handle.write("partial")
            raise RuntimeError("generation failed")
    assert target.read_text(encoding="utf-8") == "previous"
    assert not list(tmp_path.glob(".export-*"))


def test_project_database_is_protected(db):
    with pytest.raises(UnsafeOutputError):
        with safe_text_writer(db.path, db):
            pass


def test_actual_exporters_use_the_policy(db, tmp_path):
    from app.ui.views.tasks import TasksView
    from app.ui.views.settings import SettingsDialog
    source = tmp_path / "source"
    source.mkdir()
    db.add_source(str(source))
    # Call export implementations without creating a Qt window.
    class Owner:
        pass
    owner = Owner()
    owner.db = db
    for method, name in ((TasksView._write_report, "report.md"),
                         (SettingsDialog._write_audit_csv, "audit.csv")):
        with pytest.raises(UnsafeOutputError):
            method(owner, str(source / name))
    out = tmp_path / "output" / "report.md"
    TasksView._write_report(owner, str(out))
    assert out.read_bytes().startswith(b"\xef\xbb\xbf")


def test_source_registered_during_export_cannot_be_overwritten(db, tmp_path):
    target = tmp_path / "output" / "report.md"
    with pytest.raises(UnsafeOutputError):
        with safe_text_writer(target, db) as handle:
            handle.write("report")
            db.add_source(str(target.parent))
    assert not target.exists()
