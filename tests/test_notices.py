"""오픈소스 고지문 시험.

고지문은 '무엇이 들어 있고 어떤 조건인가'를 받는 사람에게 알리는 문서다.
확인할 것은 **빠진 구성요소가 없는가**, **조건이 따르는 것을 따로 밝히는가**,
**본문을 못 실은 라이선스를 실은 것처럼 넘어가지 않는가**다.
"""

from __future__ import annotations

import os

from app.tools import lockfile, notices, sbom


def test_every_component_in_the_sbom_is_disclosed():
    listed = {lockfile.normalize(item.name) for item in notices.components()}
    assert listed == {lockfile.normalize(item["name"]) for item in sbom.components()}

    text = notices.render()
    for item in notices.components():
        assert f"{item.name} {item.version} — " in text


def test_bundled_libraries_and_build_tools_are_told_apart():
    roles = {lockfile.normalize(item.name): item.role for item in notices.components()}
    for name in ("pymupdf", "pyside6", "shiboken6", "lxml", "numpy", "httpx", "openpyxl"):
        assert roles[name] == notices.RUNTIME, name
    for name in ("pytest", "pyinstaller", "coverage"):
        assert roles[name] == notices.TOOLING, name


def test_conditions_are_stated_for_copyleft_components():
    text = notices.render()
    head = text.split("2. 구성요소 목록")[0]
    assert "AGPL-3.0" in head and "PyMuPDF" in head and "MuPDF" in head
    assert notices.SOURCE_URL in head              # 이 프로그램의 대응 소스를 받는 곳
    assert "상용" in head                           # 상용 라이선스를 쓰지 않는다고 밝힌다
    assert "LGPL-3.0" in head and "PySide6" in head
    assert "PyInstaller" in head and "부트로더 예외" in head
    assert "AI 모델" in head and "포함하지 않음" in head
    assert "법률" in head


def test_license_texts_shipped_by_the_packages_are_included():
    text = notices.render()
    numpy = next(item for item in notices.components() if item.name.lower() == "numpy")
    assert numpy.texts
    assert numpy.texts[0][1][:200] in text
    assert "PYTHON SOFTWARE FOUNDATION LICENSE" in text


def test_a_license_without_its_text_is_reported_not_passed_over(tmp_path, monkeypatch):
    monkeypatch.setattr(notices, "TEXTS_DIR", tmp_path)
    assert notices.missing_texts() == list(notices.CANONICAL)
    text = notices.render()
    assert "싣지\n못했습니다" in text
    assert notices.CANONICAL["AGPL-3.0.txt"] in text
    assert notices.main(["--out", str(tmp_path / "out"), "--strict"]) == 1

    for name in notices.CANONICAL:
        (tmp_path / name).write_text(f"{name} 본문", encoding="utf-8")
    assert notices.missing_texts() == []
    text = notices.render()
    assert "싣지\n못했습니다" not in text
    assert "AGPL-3.0.txt 본문" in text
    assert notices.main(["--out", str(tmp_path / "out"), "--strict"]) == 0


def test_the_app_shows_the_bundled_file_when_there_is_one(tmp_path, monkeypatch):
    path = notices.write(tmp_path)
    assert path.name == notices.FILENAME
    assert path.read_text(encoding="utf-8") == notices.render()

    bundled = tmp_path / "bundled.txt"
    bundled.write_text("실행물에 넣은 고지문", encoding="utf-8")
    monkeypatch.setattr(notices, "BUNDLED", bundled)
    assert notices.load() == "실행물에 넣은 고지문"
    monkeypatch.setattr(notices, "BUNDLED", tmp_path / "없음.txt")
    assert notices.load() == notices.render()


def test_settings_opens_the_notice(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QPushButton

    from app.db import Database
    from app.ui.views.settings import NoticesDialog, SettingsDialog

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "p.db")
    db.init()
    dialog = SettingsDialog(db)
    assert any(button.text() == "고지문 보기" for button in dialog.findChildren(QPushButton))
    shown = NoticesDialog(dialog)
    assert "PyMuPDF" in shown.text.toPlainText()
    assert shown.text.isReadOnly()
    db.close()
