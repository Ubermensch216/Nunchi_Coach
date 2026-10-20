"""Windows의 글자 윤곽을 FreeType으로 래스터화한다. QApplication 이전에 적용."""
import os
import sys


def configure() -> None:
    """Qt 번들 엔진을 사용하고 사용자가 지정한 플랫폼/엔진은 보존한다.

    Qt 6.11 Windows DirectWrite 경로에서는 NoSubpixelAntialias를 지정해도
    RGB 색 테두리가 남았다. FreeType은 회색조 AA와 윤곽 힌팅을 적용한다.
    OS의 ClearType 설정이나 PC에 설치된 글꼴은 변경하지 않는다.
    """
    if sys.platform == 'win32':
        os.environ.setdefault('QT_QPA_PLATFORM', 'windows:fontengine=freetype')
