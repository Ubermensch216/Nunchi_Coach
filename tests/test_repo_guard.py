"""추적하면 안 되는 파일 검사 (개발지시서 P0-02·P0-05)."""

from app.tools import repo_guard


def test_databases_and_audit_outputs_are_flagged_wherever_they_sit():
    found = dict(repo_guard.violations([
        ".analysis_audit/live-project/project.db",
        ".analysis_audit/checks/보고서.md",
        "tests/fixtures/어딘가/project.db-wal",
        "예전/project.db.bak-v1",
        "data/manual-demo/문서.hwpx",
        "app/__pycache__/main.cpython-314.pyc",
    ]))
    assert len(found) == 6
    assert found[".analysis_audit/checks/보고서.md"].endswith(".analysis_audit/")
    assert found["tests/fixtures/어딘가/project.db-wal"] == "데이터베이스·실행 산출물"


def test_source_docs_and_static_fixtures_are_left_alone():
    assert repo_guard.violations([
        "app/db/schema.sql", "app/db/repo.py", "docs/현재구현_및_검증현황.md",
        "tests/fixtures/정적표본.hwpx", "doc/screenshots/업무.png", "requirements.lock",
        "app/tools/database_notes.md", "tests/test_data.py",
    ]) == []


def test_windows_separators_are_read_the_same_way():
    assert repo_guard.violations([r".analysis_audit\live-project\a.txt"])


def test_gitignore_covers_every_guarded_folder():
    """두 목록이 어긋나면 한쪽만 믿고 올린 파일이 생긴다."""
    ignored = (repo_guard.ROOT / ".gitignore").read_text(encoding="utf-8")
    for folder in repo_guard.FORBIDDEN_DIRS:
        assert f"{folder}/" in ignored, folder


def test_missing_git_is_reported_as_not_checked_rather_than_clean(monkeypatch, capsys):
    monkeypatch.setattr(repo_guard, "tracked", lambda root=None: None)
    assert repo_guard.main([]) == 2
    assert "검사하지 못했습니다" in capsys.readouterr().err
