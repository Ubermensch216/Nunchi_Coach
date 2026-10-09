"""자료 폴더 연결 — 첫 실행 1회. 메뉴가 아니다.

두 걸음으로 끝낸다: 폴더 추가 → 분석 시작. 지금 눌러야 할 단추 하나만
강조색으로 칠해 순서를 화면이 말하게 한다.
읽기 전용 원칙을 여기서 명시적으로 알린다. 공직 자료를 다루는 프로그램에
대한 첫 신뢰가 이 문장에서 만들어진다.
"""

from __future__ import annotations

import stat
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...db import Database
from ...ingest.parsers import supported_extensions
from .. import icons, theme
from ..widgets import Card, SubPanel, body_label, center_column, muted_label, view_title

# 이 화면의 기둥 폭. 할 일이 하나뿐인 화면이라 좁게 세운다.
ONBOARDING_W = 760

# 사전 점검에서 훑을 최대 항목 수. 네트워크 공유 폴더에서 멈추지 않도록 상한을 둔다.
PRECHECK_CAP = 40_000
ONBOARDING_W = 680          # 첫 화면 기둥의 폭. 한 문장이 두 줄을 넘지 않는 폭이다.
SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", "node_modules", ".git"}


class OnboardingView(QWidget):
    started = Signal()

    def __init__(self, db: Database, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self._folders: list[Path] = []

        self.setObjectName("Canvas")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        # 첫 화면은 할 일이 하나뿐이다 — 폴더를 고르고 분석을 시작한다. 그 하나를
        # 화면 가운데의 좁은 기둥에 세운다. 예전에는 창 폭을 다 쓰는 빈 상자
        # 안에 빈 목록 상자가 또 들어 있어 화면이 덜 그려진 것처럼 보였다.
        frame = QVBoxLayout(self)
        frame.setContentsMargins(theme.SP_XL, theme.SP_XL * 2, theme.SP_XL, theme.SP_XL)
        row, outer = center_column(ONBOARDING_W)
        outer.setSpacing(theme.SP_LG)
        outer.setAlignment(Qt.AlignmentFlag.AlignTop)
        frame.addLayout(row)
        frame.addStretch(1)

        column = outer

        column.addWidget(view_title("전임자 자료가 있는 폴더를 연결하세요"))
        column.addWidget(
            muted_label(
                "정리되지 않은 폴더를 그대로 연결해도 됩니다. 내 PC, USB, 네트워크 "
                "공유 폴더를 여러 개 함께 연결할 수 있고, 흩어져 있어도 하나로 묶어 "
                "살펴봅니다."
            )
        )

        card = Card()

        # 고른 폴더가 없을 때는 빈 목록 대신 무엇을 하면 되는지를 적는다.
        self.placeholder = SubPanel()
        self.placeholder.body.setContentsMargins(
            theme.SP_LG, theme.SP_XL, theme.SP_LG, theme.SP_XL)
        hint = muted_label("‘폴더 추가’를 눌러 전임자 자료가 있는 폴더를 고르세요. "
                           "정리되지 않은 폴더를 그대로 지정해도 됩니다.")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.body.addWidget(hint)
        card.body.addWidget(self.placeholder)

        self.list = QListWidget()
        self.list.setFixedHeight(140)
        self.list.setAlternatingRowColors(True)
        self.list.setVisible(False)
        card.body.addWidget(self.list)

        buttons = QHBoxLayout()
        buttons.setSpacing(theme.SP_SM)
        self.add = QPushButton("+ 폴더 추가")
        self.add.setObjectName("Primary")
        self.add.clicked.connect(self._add_folder)
        buttons.addWidget(self.add)

        self.remove = QPushButton("선택한 폴더 빼기")
        self.remove.setObjectName("Quiet")
        self.remove.setEnabled(False)
        self.remove.hide()
        self.remove.clicked.connect(self._remove_selected)
        buttons.addWidget(self.remove)
        buttons.addStretch(1)
        card.body.addLayout(buttons)

        self.summary = muted_label("아직 선택한 폴더가 없습니다.")
        card.body.addWidget(self.summary)

        # 시작 버튼은 카드 안, 고른 폴더 바로 아래에 둔다. 카드 밖에 떨어져
        # 있으면 무엇을 시작하는 버튼인지가 카드와 끊긴다.
        foot = QFrame()
        foot.setObjectName("ActionBar")
        foot_row = QHBoxLayout(foot)
        foot_row.setContentsMargins(0, theme.SP_MD, 0, 0)
        foot_row.setSpacing(theme.SP_SM)
        lock = icons.label("lock", theme.CONFIRMED, 16)
        if lock is not None:
            foot_row.addWidget(lock, 0, Qt.AlignmentFlag.AlignTop)
        notice = body_label(
            "이 프로그램은 원본을 수정·이동·삭제·이름 변경하지 않습니다. "
            "읽기만 하고, 분석 결과는 따로 저장합니다."
        )
        notice.setObjectName("Small")
        foot_row.addWidget(notice, 1)

        self.start = QPushButton("분석 시작")
        self.start.setEnabled(False)
        self.start.setToolTip("폴더를 하나 이상 고르면 시작할 수 있습니다")
        self.start.clicked.connect(self._start)
        foot_row.addWidget(self.start, 0, Qt.AlignmentFlag.AlignVCenter)
        card.body.addWidget(foot)

        outer.addWidget(card)

        self.list.itemSelectionChanged.connect(
            lambda: self.remove.setEnabled(bool(self.list.selectedItems()))
        )

        # 시작 화면에서 자료 폴더를 이미 골랐다면 여기서 또 묻지 않는다.
        # 바로 분석을 돌리지 않고 목록에 올려만 두는 것은, 규모와 읽기 전용
        # 안내를 보고 '분석 시작'을 누르는 단계는 그대로 두기 위해서다.
        seed = self.db.get_meta("seed_source")
        if seed and not self.db.sources():
            self._append(Path(seed))

    # ── 폴더 ────────────────────────────────────────────────────────
    def _add_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "전임자 자료 폴더 선택")
        if not chosen:
            return
        self._append(Path(chosen))

    def _append(self, path: Path) -> None:
        if path in self._folders:
            return
        self._folders.append(path)
        self.list.addItem(QListWidgetItem(str(path)))
        self._precheck()

    def _remove_selected(self) -> None:
        for item in self.list.selectedItems():
            row = self.list.row(item)
            self.list.takeItem(row)
            del self._folders[row]
        self._precheck()

    def _precheck(self) -> None:
        """읽기 권한과 대략적인 규모를 실행 전에 보여준다 (PRJ-004)."""
        self.list.setVisible(bool(self._folders))
        self.placeholder.setVisible(not self._folders)
        if not self._folders:
            self.summary.setText("아직 선택한 폴더가 없습니다.")
            self.start.setEnabled(False)
            self._sync_actions()
            return

        supported = set(supported_extensions())
        files = 0
        target = 0
        size = 0
        blocked = 0                 # 읽을 수 없던 **파일** 수 (§23)
        denied: list[str] = []      # 아예 들어가지 못한 폴더
        capped = False

        for folder in self._folders:
            try:
                for path in folder.rglob("*"):
                    if files >= PRECHECK_CAP:
                        capped = True
                        break
                    if any(part in SKIP_DIRS for part in path.parts):
                        continue
                    # is_file()로 먼저 거르지 않는다 — 권한이 없으면 그 함수는
                    # 오류 대신 False를 돌려주어, 못 읽는 파일이 '없는 파일'과
                    # 구별되지 않는다. 사용자에게는 그 둘이 전혀 다르다.
                    try:
                        info = path.stat()
                    except OSError:
                        blocked += 1
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    files += 1
                    size += info.st_size
                    if path.suffix.lower() in supported:
                        target += 1
            except (PermissionError, OSError) as exc:
                denied.append(f"{folder} ({exc.strerror or exc})")

        prefix = "약 " if capped else ""
        text = (
            f"{prefix}파일 {files:,}개 · {_human(size)} · "
            f"분석 대상 문서 {target:,}건 · 접근 불가 {blocked:,}개"
        )
        if capped:
            text += f"  (사전 점검은 {PRECHECK_CAP:,}건까지만 훑습니다)"
        if denied:
            text += "\n⚠ 열 수 없는 폴더: " + ", ".join(denied)
        if blocked:
            text += (
                f"\nⓘ 파일 {blocked:,}개는 권한이나 잠금 때문에 지금 읽을 수 없습니다. "
                "나머지는 그대로 분석합니다."
            )
        if target == 0 and not denied:
            text += "\n⚠ 분석할 수 있는 문서를 찾지 못했습니다."

        self.summary.setText(text)
        self.start.setEnabled(target > 0)
        self._sync_actions()

    def _sync_actions(self) -> None:
        """지금 눌러야 할 단추 하나만 칠한다.

        폴더가 없으면 '폴더 추가'가, 분석할 문서가 잡히면 '분석 시작'이 그
        하나다. 둘을 다 칠해 두면 처음 보는 사람은 순서를 다시 읽어야 한다.
        """
        ready = self.start.isEnabled()
        has_folders = bool(self._folders)
        self.list.setVisible(has_folders)
        self.list.setFixedHeight(min(len(self._folders), 5) * theme.ROW_H + theme.SP_MD)
        self.remove.setVisible(has_folders)
        for button, primary in ((self.add, not ready), (self.start, ready)):
            button.setObjectName("Primary" if primary else "")
            button.style().unpolish(button)
            button.style().polish(button)

    def _start(self) -> None:
        for folder in self._folders:
            self.db.add_source(folder, kind=_source_kind(folder))
        self.db.audit("scan.request", detail=f"{len(self._folders)}개 자료원")
        self.started.emit()

    def refresh(self) -> None:
        pass


def _source_kind(path: Path) -> str:
    text = str(path)
    if text.startswith("\\\\"):
        return "unc"
    return "local"


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.1f}{unit}" if unit != "B" else f"{int(value):,}B"
        value /= 1024
    return f"{value:,.1f}TB"
