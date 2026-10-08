"""시험 전체에 적용되는 안전장치.

UI 시험은 실제 버튼을 누른다. '원본 열기' 버튼은
app/ui/widgets/common.py 의 open_original() → os.startfile() 로 이어져
OS에 등록된 프로그램(.hwp 라면 한글)을 **진짜로** 띄운다.
시험이 tmp_path 에 만드는 문서는 내용이 "dummy" 인 가짜라서 한글이 형식을
알아보지 못하고 인코딩 선택 대화상자를 띄우며, 시험을 돌릴 때마다 그 창이
쌓인다.

시험이 확인해야 하는 것은 "앱이 원본을 열어 달라고 요청했는가"까지지
OS가 실제로 프로그램을 띄우는지가 아니므로, 외부 실행만 가로막고 호출
기록은 남긴다. 기록이 필요하면 시험에서 no_external_launch 를 인자로 받으면
된다.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest


def pytest_configure(config):
    # 사용자 TEMP의 예전 실행 권한에 의존하지 않고 테스트마다 격리한다.
    root = Path(__file__).resolve().parents[1] / ".test_runs"
    root.mkdir(exist_ok=True)
    if not config.option.basetemp:
        config.option.basetemp = str(root / f"tmp-{uuid4().hex[:12]}")


# ── 건너뛴 시험을 숨기지 않는다 ─────────────────────────────────────
# NUNCHICOACH_STRICT_SKIPS=1 (CI)이면 아래 사유 말고는 건너뛴 시험·xfail을 실패로
# 친다. 표본을 안 만들어 파서 시험이 통째로 빠진 실행이 녹색으로 끝나면 안 된다.
# 허용하는 것은 이 장비에서 할 수 없는 것뿐이고, 보고서에는 '미실행'으로 적는다.
STRICT_SKIPS_ENV = "NUNCHICOACH_STRICT_SKIPS"
ALLOWED_SKIPS = (
    "NUNCHICOACH_LIVE_AI",          # 실제 모델 시험 — 별도 수동 실행
    "실문서 표본이 없습니다",          # 실제 기관 문서 표본 — 저장소에 두지 않는다
    "프로젝트 .venv가 아닌 환경",      # CI는 lockfile --check 단계로 따로 검사한다
)
_unexpected_skips: list[str] = []


def pytest_runtest_logreport(report):
    if not report.skipped or os.environ.get(STRICT_SKIPS_ENV) != "1":
        return
    reason = getattr(report, "wasxfail", None)
    if reason is None:
        longrepr = report.longrepr
        reason = longrepr[2] if isinstance(longrepr, tuple) else str(longrepr)
        if any(allowed in reason for allowed in ALLOWED_SKIPS):
            return
    else:
        reason = f"xfail: {reason}"
    _unexpected_skips.append(f"{report.nodeid} — {reason}")


def pytest_sessionfinish(session, exitstatus):
    if _unexpected_skips and session.exitstatus == 0:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter):
    if not _unexpected_skips:
        return
    terminalreporter.section("허용되지 않은 건너뜀 — 실패로 처리")
    for line in _unexpected_skips:
        terminalreporter.write_line(line)


@pytest.fixture(autouse=True)
def release_closed_qt_objects():
    yield
    # 테스트는 app.exec() 없이 processEvents()만 부른다. 이 경우
    # deleteLater 요청이 남아 다음 테마 변경이 닫힌 창들까지 다시 그린다.
    from PySide6.QtCore import QCoreApplication, QEvent
    app = QCoreApplication.instance()
    if app is not None:
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

# "기본 프로그램으로 열기" 계열 명령. 이것만 막고 나머지 subprocess 사용
# (test_crash_resume.py 가 앱을 직접 띄우는 등)은 그대로 통과시킨다.
_LAUNCHER_COMMANDS = {"explorer", "explorer.exe", "open", "xdg-open"}


class _NotLaunched:
    """subprocess.Popen 자리를 대신하는 빈 프로세스."""

    returncode = 0
    pid = -1

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def communicate(self, input=None, timeout=None):
        return (b"", b"")

    def terminate(self):
        pass

    def kill(self):
        pass


@pytest.fixture(autouse=True)
def no_external_launch(monkeypatch):
    """외부 프로그램 실행을 가로막고 요청 내역을 돌려준다."""
    launched: list[str] = []

    # os.startfile 은 Windows 에만 있다 (raising=False).
    monkeypatch.setattr(
        os, "startfile",
        lambda path, *args, **kwargs: launched.append(str(path)),
        raising=False,
    )

    real_popen = subprocess.Popen

    def guarded_popen(args, *rest, **kwargs):
        head = args[0] if isinstance(args, (list, tuple)) and args else args
        if os.path.basename(str(head)).lower() in _LAUNCHER_COMMANDS:
            launched.append(" ".join(str(a) for a in args) if isinstance(args, (list, tuple)) else str(args))
            return _NotLaunched()
        return real_popen(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)

    return launched
