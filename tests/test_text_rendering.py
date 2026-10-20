"""앱 시작 설정과 Windows에서 실제 래스터화된 글자 색을 검증한다."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.ui import text_rendering


def test_configure_selects_bundled_engine_only_on_windows(monkeypatch):
    monkeypatch.delenv('QT_QPA_PLATFORM', raising=False)
    monkeypatch.setattr(text_rendering.sys, 'platform', 'win32')
    text_rendering.configure()
    assert os.environ['QT_QPA_PLATFORM'] == 'windows:fontengine=freetype'


@pytest.mark.parametrize('platform', ['offscreen', 'windows:fontengine=directwrite'])
def test_configure_preserves_explicit_platform(monkeypatch, platform):
    monkeypatch.setenv('QT_QPA_PLATFORM', platform)
    monkeypatch.setattr(text_rendering.sys, 'platform', 'win32')
    text_rendering.configure()
    assert os.environ['QT_QPA_PLATFORM'] == platform


def test_configure_leaves_other_operating_systems_alone(monkeypatch):
    monkeypatch.delenv('QT_QPA_PLATFORM', raising=False)
    monkeypatch.setattr(text_rendering.sys, 'platform', 'linux')
    text_rendering.configure()
    assert 'QT_QPA_PLATFORM' not in os.environ


@pytest.mark.skipif(sys.platform != 'win32', reason='실제 Windows Qt 엔진 검사')
def test_windows_text_is_grayscale_and_rerenders_at_larger_sizes():
    # 기존 테스트 QApplication의 offscreen 엔진과 분리한 새 프로세스로 확인한다.
    code = r'''
from app.ui import text_rendering
text_rendering.configure()
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QImage, QPainter, QRawFont
from PySide6.QtWidgets import QApplication
from app.ui import theme
app = QApplication([])
theme.apply(app, mode='light')
assert app.platformName() == 'windows'
font = QFont(app.font())
font.setWeight(QFont.Weight.Bold)
for size in (18, 54):
    font.setPointSizeF(size)
    raw = QRawFont.fromFont(font)
    glyph = raw.glyphIndexesForString('업')[0]
    assert not raw.pathForGlyph(glyph).isEmpty(), '글꼴에 벡터 윤곽이 있어야 한다'
    image = QImage(900, 110, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    painter.setFont(font)
    painter.setPen(Qt.GlobalColor.black)
    painter.drawText(5, 85, '업무를 파악하고 월별 실적을 취합합니다')
    painter.end()
    edges = 0
    for y in range(image.height()):
        for x in range(image.width()):
            c = image.pixelColor(x, y)
            assert c.red() == c.green() == c.blue(), '서브픽셀 색 번짐이 남음'
            edges += 0 < c.red() < 255
    assert edges > 100, '이진 비트맵이 아닌 회색조 안티앨리어싱이어야 한다'
print('native grayscale and outline rendering OK')
'''
    env = dict(os.environ)
    for name in ('QT_QPA_PLATFORM', 'QT_SCALE_FACTOR', 'QT_FONT_DPI'):
        env.pop(name, None)
    result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
