"""진입점.

    python -m app.main [--project 이름] [--data 경로]

인자 없이 실행하면 **시작 화면**이 먼저 뜬다 — 이 PC에 있는 인수인계 목록에서
하나를 고르거나 새로 만든다(계획서 §22). `--project`는 개발·시험용으로
남겨 둔 지름길이고, 주면 그 프로젝트를 곧장 연다.

완전 로컬로 동작한다. 외부 네트워크를 호출하지 않으며, AI(Ollama)도
127.0.0.1로만 연결한다.
"""

from __future__ import annotations

import argparse
import faulthandler
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from .bridge import service as bridge_service
from .bridge.store import FILE_NAME as BRIDGE_FILE, BridgeStore
from .db import ForeignProjectError, default_project_dir, open_project_at, registry
from .db.registry import ProjectEntry
from .ui import motion, theme, text_rendering
from .ui.shell import MainWindow
from .ui.views.launcher import LauncherDialog
from .core import diagnostics
from . import __version__

_LOGO_PATH = Path(__file__).parent / "ui" / "assets" / "logo.ico"
_WINDOWS_APP_ID = "NunchiCoach.Desktop"


def _set_windows_app_id() -> None:
    """작업표시줄에서 Python 대신 눈치코치의 아이콘과 창 묶음을 쓴다."""
    if sys.platform != "win32":
        return

    import ctypes
    from ctypes import wintypes

    # Windows에는 첫 창을 만들기 전에 제품 식별자를 알려야 한다.
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    set_app_id = shell32.SetCurrentProcessExplicitAppUserModelID
    set_app_id.argtypes = [wintypes.LPCWSTR]
    set_app_id.restype = ctypes.c_long
    result = set_app_id(_WINDOWS_APP_ID)
    if result < 0:
        print(f"Windows taskbar identity failed: 0x{result & 0xffffffff:08X}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    # 네이티브 라이브러리가 프로세스를 죽이면(Windows 종료 코드 -1073740791 = 0xC0000409) 파이썬은
    # 아무 말도 못 하고 사라진다. 죽기 직전의 파이썬 호출 위치를 콘솔에 남긴다.
    if sys.stderr is not None:                 # 콘솔 없는 묶음 실행물에서는 stderr가 없다
        faulthandler.enable()
    args = _parse_args(argv)

    _set_windows_app_id()
    text_rendering.configure()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("눈치코치")
    app.setOrganizationName("NunchiCoach")
    if _LOGO_PATH.exists():
        app.setWindowIcon(QIcon(str(_LOGO_PATH)))

    _speak_korean(app)
    root = Path(args.data) if args.data else None
    theme.apply(app, "medium", "system")   # 시작 화면에도 같은 옷을 입힌다
    target = _from_args(args, root)

    # onNara.sAIde 브리지는 인수인계가 아니라 앱에 딸려 있다. 설정에서 켜 둔 경우에만 포트를 연다.
    bridge = bridge_service.BridgeService(BridgeStore((root or default_project_dir().parent) / BRIDGE_FILE))
    bridge_service.install(bridge)
    bridge.start_if_enabled()
    try:
        # 프로젝트를 바꾸면 창을 새로 만든다. MainWindow가 db를 생성자에서 받아
        # 뷰마다 들고 있어(shell.py), 열린 창의 db만 갈아 끼우는 것은 안전하지
        # 않다 — 백그라운드 스레드까지 옛 DB를 가리키고 있다.
        while True:
            if target is None:
                target = _choose(root)
                if target is None:
                    return 0
            try:
                target = _run(app, target, root)
            except ForeignProjectError as exc:
                QMessageBox.warning(None, "프로젝트를 열 수 없습니다", str(exc))
                target = None   # 시작 화면으로 돌아가 다른 프로젝트를 고르게 한다
            if target is None:
                return 0
    finally:
        bridge.stop()
        bridge_service.install(None)


def _speak_korean(app: QApplication) -> bool:
    """Qt가 스스로 그리는 버튼(확인·취소·예·아니요)을 우리말로 바꾼다.

    번역을 싣지 않으면 확인 창과 입력 창의 버튼이 OK/Cancel/Yes/No로 나온다 —
    본문은 전부 우리말인데 정작 누르는 자리만 영어였다. Qt에 딸려 오는 번역
    파일을 읽을 뿐이며, 못 찾으면 영어 그대로 두고 넘어간다.
    """
    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator

    folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    translator = QTranslator(app)          # 앱이 붙들어야 번역이 살아 있다
    if not translator.load(QLocale(QLocale.Language.Korean), "qtbase", "_", folder):
        return False
    return app.installTranslator(translator)


def _run(app: QApplication, entry: ProjectEntry, root: Path | None) -> ProjectEntry | None:
    """프로젝트 하나를 연다. 돌려주는 값이 있으면 그 프로젝트로 갈아탄다."""
    db = open_project_at(entry.path)
    diagnostics.configure(db.path.parent / "logs")
    sys.excepthook = diagnostics.exception_hook
    diagnostics.record("app.start", version=__version__)
    db.set_meta("project_name", entry.name)
    if entry.seed is not None:
        # 시작 화면에서 이미 고른 자료 폴더. DB에 적어 두어야 분석을 시작하기
        # 전에 앱을 껐다 켜도 같은 폴더를 다시 묻지 않는다.
        db.set_meta("seed_source", str(entry.seed))
    registry.touch(entry.path, root)
    db.audit("app.start", detail=f"project={entry.name}")
    theme.apply(app, _text_size(db), theme.normalize_mode(db.get_meta("theme_mode")))
    motion.configure(db)

    window = MainWindow(db, project_name=entry.name)
    bridge = bridge_service.current()
    if bridge is not None:
        bridge.describe = lambda: {"project": entry.name}
        bridge.project_db = db.path
    switch_to: list[ProjectEntry] = []

    def request_switch() -> None:
        chosen = _choose(root, parent=window, current=entry.path)
        if chosen is None or chosen.path == entry.path:
            return
        switch_to.append(chosen)
        window.close()

    window.switch_requested.connect(request_switch)
    disconnect = _follow_system_theme(app, db, window)
    window.show()
    try:
        app.exec()
    finally:
        disconnect()
        if bridge is not None:
            bridge.describe = dict
            bridge.project_db = None
        db.audit("app.stop")
        diagnostics.record("app.stop")
        db.close()
    return switch_to[0] if switch_to else None


def _from_args(args, root: Path | None) -> ProjectEntry | None:
    """`--project 이름`으로 곧장 열기. 목록에도 올려 다음부터 화면에서 보인다."""
    if not args.project:
        return None
    return registry.register(
        (root or default_project_dir()) / args.project, name=args.project, root=root
    )


def _choose(root: Path | None, parent=None, current: Path | None = None) -> ProjectEntry | None:
    dialog = LauncherDialog(root, parent, current=current)
    dialog.exec()
    return dialog.chosen


def _follow_system_theme(app, db, window):
    """테마가 '시스템'이면 윈도우 설정이 바뀔 때 같이 바뀐다.

    시작할 때 한 번만 읽으면 '시스템'은 사실상 '그때의 시스템'이다. 야근하다
    윈도우를 어둡게 바꿨는데 이 창만 하얗게 남아 있으면 설정이 거짓말을 한 게
    된다. Qt 6.5부터 알려주는 신호가 있으니 그것에 붙인다.

    끊는 함수를 돌려준다. 프로젝트를 갈아탈 때마다 붙이기만 하면, 닫힌 창과
    이미 닫은 DB를 가리키는 연결이 앱이 사는 내내 쌓인다.
    """
    hints = app.styleHints()
    if not hasattr(hints, "colorSchemeChanged"):   # pragma: no cover — 옛 Qt 방어
        return lambda: None

    def repaint(_scheme=None) -> None:
        if theme.normalize_mode(db.get_meta("theme_mode")) != "system":
            return
        theme.apply(app, _text_size(db), "system")
        window.restyle()

    hints.colorSchemeChanged.connect(repaint)
    return lambda: hints.colorSchemeChanged.disconnect(repaint)


def _text_size(db) -> str:
    """저장된 글자 크기. 옛 설정(large_text)을 3단계로 옮겨 읽는다.

    쓰던 사람이 켜 둔 '글자 크게 보기'가 갱신 후 조용히 꺼져 있으면, 그
    사람에게는 기능이 사라진 것이 아니라 앱이 망가진 것으로 보인다.
    """
    stored = db.get_meta("text_size")
    if stored in theme.TEXT_SIZES:
        return stored
    return "large" if db.get_meta("large_text") == "1" else "medium"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="눈치코치")
    parser.add_argument(
        "--project",
        default=None,
        help="프로젝트 이름 (생략하면 시작 화면에서 고릅니다)",
    )
    parser.add_argument(
        "--data",
        default=None,
        help=f"프로젝트 저장 위치 (기본: {default_project_dir()})",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
