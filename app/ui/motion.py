"""짧은 전환 효과와 '애니메이션 줄이기' (개편계획서 §14.2).

움직임은 상태를 알리는 보조일 뿐이다. 선택·완료·초점은 움직임 없이도 정적으로
분명해야 하고, 여기서는 새 본문이 나타날 때의 짧은 페이드만 맡는다.

- 설정 `reduce_motion`: `on`(줄이기) · `off`(그대로) · 비어 있으면 윈도우의
  '애니메이션 효과' 설정을 따른다.
- 줄이기 모드에서는 어떤 효과도 만들지 않는다. 입력을 막는 일도 없다.
- 연속 입력이 오면 이전 효과를 즉시 정리하고 최신 위젯만 남긴다.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QEasingCurve, QObject, QPropertyAnimation
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget
from shiboken6 import isValid

# 구현 초깃값(ms)이며 측정된 성능 수치가 아니다.
DURATIONS = {
    "enter": 200,    # 홈 → 업무 화면
    "select": 125,   # 좌측 업무 선택, 업무 → 홈
    "tab": 150,      # 세부 탭
    "value": 320,    # 상단 진행 막대 값 이동
}
START_OPACITY = 0.2   # 완전히 비우면 깜박임으로 읽힌다

MODES = ("system", "on", "off")
_mode = "system"


def normalize(value: str | None) -> str:
    return value if value in ("on", "off") else "system"


def set_mode(value: str | None) -> str:
    global _mode
    _mode = normalize(value)
    return _mode


def mode() -> str:
    return _mode


def configure(db) -> str:
    """프로젝트에 저장된 설정을 읽어 적용한다."""
    return set_mode(db.get_meta("reduce_motion"))


def system_prefers_reduced() -> bool:
    """윈도우의 '애니메이션 효과'가 꺼져 있으면 True. 알 수 없으면 False."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        enabled = wintypes.BOOL(True)
        SPI_GETCLIENTAREAANIMATION = 0x1042
        if not ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0):
            return False
        return not enabled.value
    except (AttributeError, OSError, ImportError):  # pragma: no cover — 비윈도우·권한 방어
        return False


def is_reduced() -> bool:
    if _mode == "on":
        return True
    if _mode == "off":
        return False
    return system_prefers_reduced()


def duration(kind: str) -> int:
    """줄이기 모드에서는 0 — 호출한 쪽이 값을 바로 놓는다."""
    return 0 if is_reduced() else DURATIONS[kind]


class MotionController(QObject):
    """한 화면이 쓰는 전환 효과의 수명 관리. 동시에 하나만 돈다."""

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._animation: QPropertyAnimation | None = None
        self._target: QWidget | None = None

    @property
    def running(self) -> bool:
        return self._animation is not None

    def reveal(self, widget: QWidget, kind: str = "tab") -> bool:
        """새 본문을 짧게 나타낸다. 효과를 걸었으면 True."""
        self.cancel()
        if widget is None or is_reduced():
            return False
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(START_OPACITY)
        widget.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(DURATIONS[kind])
        animation.setStartValue(START_OPACITY)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.finished.connect(self._finish)
        self._animation = animation
        self._target = widget
        animation.start()
        return True

    def cancel(self) -> None:
        """진행 중인 효과를 멈추고 위젯을 원래 상태로 돌린다."""
        animation, self._animation = self._animation, None
        target, self._target = self._target, None
        if animation is not None and isValid(animation):
            animation.stop()
            animation.deleteLater()
        if target is not None and isValid(target):
            target.setGraphicsEffect(None)

    def _finish(self) -> None:
        self.cancel()
