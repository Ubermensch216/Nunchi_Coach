"""실행물 자체 점검 — 복원에서 이어받기까지 한 번 끝까지 간다."""

from app.tools import selfcheck


def test_restoration_roundtrip_passes_and_leaves_nothing_behind(tmp_path, monkeypatch):
    made = []
    real = selfcheck.tempfile.mkdtemp
    monkeypatch.setattr(selfcheck.tempfile, "mkdtemp",
                        lambda **kwargs: made.append(real(dir=tmp_path, **kwargs)) or made[-1])
    assert selfcheck.run() == []
    assert made and not any(tmp_path.iterdir()), "임시 폴더를 남겼다"


def test_a_broken_step_is_reported_instead_of_raising(monkeypatch):
    from app.core import transfer

    def broken(*args, **kwargs):
        raise ModuleNotFoundError("묶음에서 빠진 모듈")
    monkeypatch.setattr(transfer, "export_package", broken)
    problems = selfcheck.run()
    assert problems == ["ModuleNotFoundError: 묶음에서 빠진 모듈"]
    assert selfcheck.main([]) == 1
