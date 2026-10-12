"""업무 복원본 — 저장한 판을 보고, 지금 판을 저장하고, 글로 내보낸다 (C06).

'지금 할 일' 화면은 계속 바뀐다. 여기는 **굳혀 둔 판**의 목록이다. 판을
고르면 그때의 요약이 보이고, 글 파일로 내보내면 그때의 내용이 그대로 나온다.

전임자의 확인을 요구하지 않는다. 확인하지 않은 값이 몇 개인지 숨기지도 않는다.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import handover_release, transfer, workcard, workstate
from ..core.output_policy import UnsafeOutputError, safe_text_writer
from . import theme
from .widgets import muted_label, section_title


def _local(stamp: str) -> str:
    """DB가 적은 저장 시각(세계 표준시)을 이 PC의 시각으로 바꿔 적는다.

    위 목록은 이 PC의 시각으로 적는데 아래 요약만 표준시로 적혀, 같은 판이
    목록에서는 23:16, 요약에서는 14:16으로 아홉 시간 어긋나 보였다. 읽을 수
    없는 값이면 고치지 않고 그대로 돌려준다.
    """
    try:
        moment = datetime.fromisoformat(stamp.replace(" ", "T"))
    except (TypeError, ValueError, AttributeError):
        return stamp
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


class SnapshotDialog(QDialog):
    def __init__(self, db, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("저장·옮기기 — 업무 복원본")
        self.setMinimumSize(640, 480)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(theme.SP_XL, theme.SP_XL, theme.SP_XL, theme.SP_LG)
        outer.setSpacing(theme.SP_MD)
        outer.addWidget(section_title("저장한 업무 복원본"))
        outer.addWidget(muted_label(
            "자료에서 복원한 업무·절차·확인할 정보와 수행 기록을 그 시점 그대로 저장한 "
            "것입니다. 분석이 끝날 때 자동으로 저장되고, 여기서 지금 상태를 저장할 수도 "
            "있습니다. 저장한 판은 바뀌지 않습니다.", small=True))

        self.list = QListWidget()
        # 한 줄이 길다(종류·시각·업무 수·확인 수·남은 것). 잘라 내거나 옆으로
        # 밀지 않고 접어서 다 보인다 — 예전에는 뒷부분이 창 밖으로 잘렸다.
        self.list.setWordWrap(True)
        self.list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setSpacing(2)
        self.list.currentItemChanged.connect(lambda *_: self._show_selected())
        outer.addWidget(self.list, 1)

        self.detail = muted_label("", small=True)
        outer.addWidget(self.detail)

        row = QHBoxLayout()
        row.setSpacing(theme.SP_SM)
        self.save_button = QPushButton("지금 상태 저장")
        self.save_button.setObjectName("Primary")
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.clicked.connect(lambda _=False: self.save_now())
        row.addWidget(self.save_button)
        self.export_button = QPushButton("글 파일로 내보내기")
        self.export_button.setObjectName("Quiet")
        self.export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_button.clicked.connect(lambda _=False: self._export_selected())
        row.addWidget(self.export_button)
        self.package_button = QPushButton("묶음으로 내보내기")
        self.package_button.setObjectName("Quiet")
        self.package_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.package_button.setToolTip(
            "다른 PC의 눈치코치에서 이어받을 수 있는 파일로 내보냅니다. 원문 파일은 "
            "들어가지 않고, 인용문은 들어갑니다")
        self.package_button.clicked.connect(lambda _=False: self._export_package_selected())
        row.addWidget(self.package_button)
        receive = QPushButton("묶음 가져오기")
        receive.setObjectName("Quiet")
        receive.setCursor(Qt.CursorShape.PointingHandCursor)
        receive.setToolTip("다른 곳에서 내보낸 복원본·인계본을 이 프로젝트로 가져옵니다. "
                           "여기서 이미 고치거나 수행한 것은 덮지 않습니다")
        receive.clicked.connect(lambda _=False: self._import_package())
        row.addWidget(receive)
        row.addStretch(1)
        close = QPushButton("닫기")
        close.setObjectName("Quiet")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        outer.addLayout(row)

        self.reload()

    # ── 목록 ────────────────────────────────────────────────────────
    def reload(self, select: int | None = None) -> None:
        self.list.clear()
        for snapshot in self.db.snapshots():
            item = QListWidgetItem(handover_release.describe(snapshot))
            item.setData(Qt.ItemDataRole.UserRole, snapshot["id"])
            self.list.addItem(item)
            if snapshot["id"] == select:
                self.list.setCurrentItem(item)
        if self.list.count() and self.list.currentItem() is None:
            self.list.setCurrentRow(0)
        self._show_selected()

    def selected_id(self) -> int | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _show_selected(self) -> None:
        snapshot_id = self.selected_id()
        self.export_button.setEnabled(snapshot_id is not None)
        self.package_button.setEnabled(snapshot_id is not None)
        if snapshot_id is None:
            self.detail.setText("아직 저장한 복원본이 없습니다. 분석이 끝나면 자동으로 "
                                "하나 저장됩니다.")
            return
        self.detail.setText(self.summary_text(snapshot_id))

    def summary_text(self, snapshot_id: int) -> str:
        row = self.db.snapshot(snapshot_id)
        summary = json.loads(row["summary"])
        unconfirmed = summary["items"] - summary["items_confirmed"]
        lines = [
            f"{workstate.SNAPSHOT_LABEL.get(row['kind'], row['kind'])} · "
            f"저장 {_local(row['created_at'])}",
            f"업무 {summary['tasks']}개 · 항목 {summary['items']}개 · 문서에 적힌 절차 "
            f"{summary['steps']}단계 중 {summary['steps_done']}단계 완료 기록 · "
            f"근거 문서 {summary['documents']}건",
            # 확인하지 않은 것을 먼저 말한다 — 이 판이 다음 사람에게 그대로 간다.
            f"사람이 확인하지 않은 항목 {unconfirmed}개 · 자료가 엇갈리는 항목 "
            f"{summary['conflicts']}개 · 확인할 것 {summary['gaps_open']}건",
        ]
        return "\n".join(lines)

    # ── 저장·내보내기 ───────────────────────────────────────────────
    def save_now(self) -> tuple[int, bool]:
        snapshot_id, created = handover_release.save(self.db, label="직접 저장")
        self.reload(select=snapshot_id)
        if not created:
            self.detail.setText("마지막으로 저장한 판과 달라진 것이 없어 새로 저장하지 "
                                "않았습니다.\n" + self.summary_text(snapshot_id))
        return snapshot_id, created

    def _export_selected(self) -> None:
        snapshot_id = self.selected_id()
        if snapshot_id is None:
            return
        folder = workcard.default_output_dir(self.db)
        name = f"{Path(self.db.path).parent.name}_업무복원본_{datetime.now():%Y%m%d}_{snapshot_id}.md"
        path, _filter = QFileDialog.getSaveFileName(
            self, "업무 복원본 저장", str(folder / name), "Markdown (*.md)")
        if not path:
            return
        try:
            self.export(snapshot_id, path)
        except (UnsafeOutputError, OSError) as exc:
            QMessageBox.warning(self, "저장하지 못했습니다", str(exc))
            return
        QMessageBox.information(self, "저장했습니다", path)

    def _export_package_selected(self) -> None:
        snapshot_id = self.selected_id()
        if snapshot_id is None:
            return
        folder = workcard.default_output_dir(self.db)
        name = f"{Path(self.db.path).parent.name}_인계묶음_{datetime.now():%Y%m%d}_{snapshot_id}.zip"
        path, _filter = QFileDialog.getSaveFileName(
            self, "묶음으로 내보내기", str(folder / name), "눈치코치 묶음 (*.zip)")
        if not path:
            return
        try:
            self.export_package(snapshot_id, path)
        except (UnsafeOutputError, transfer.PackageError, OSError) as exc:
            QMessageBox.warning(self, "내보내지 못했습니다", str(exc))
            return
        QMessageBox.information(self, "묶음을 만들었습니다", f"{path}\n\n{transfer.NOTICE}")

    def export_package(self, snapshot_id: int, path: str) -> Path:
        return transfer.export_package(self.db, snapshot_id, path)

    def _import_package(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "묶음 가져오기", "", "눈치코치 묶음 (*.zip)")
        if not path:
            return
        try:
            result = self.import_package(path)
        except transfer.PackageError as exc:
            QMessageBox.warning(self, "가져오지 못했습니다", str(exc))
            return
        QMessageBox.information(self, "가져왔습니다", self.receipt_text(result))

    def import_package(self, path: str) -> dict:
        result = transfer.import_package(self.db, path)
        self.reload(select=result["snapshot_id"])
        return result

    @staticmethod
    def receipt_text(result: dict) -> str:
        lines = [
            f"{workstate.SNAPSHOT_LABEL[result['kind']]}을 가져왔습니다.",
            f"업무 {result['tasks']}개 · 항목 {result['items']}개 · 단계 {result['steps']}개 · "
            f"확인할 정보 {result['gaps']}건",
            f"근거 문서: {transfer.describe_relink(result['relink'])}",
        ]
        if result["kept_local"]:
            lines.append(f"여기서 이미 고치거나 수행한 {result['kept_local']}건은 그대로 두었습니다.")
        decisions = result.get("decisions")
        if decisions:
            line = (f"연결 근거에 대한 사람의 판정 {decisions['received']}건을 받았습니다 — "
                    f"적용 {decisions['applied']}건 · 대상을 아직 못 찾음 {decisions['pending']}건")
            if decisions["kept_local"]:
                line += f" · 여기의 더 나중 판정을 지킴 {decisions['kept_local']}건"
            lines.append(line + ". 못 찾은 판정은 분석이 끝난 뒤 다시 잇습니다.")
        lines.append("자료 폴더를 지정하고 분석을 돌리면 아직 못 찾은 근거를 다시 찾습니다. "
                     "가져왔다는 것이 내용을 모두 확인했다는 뜻은 아닙니다.")
        return "\n".join(lines)

    def export(self, snapshot_id: int, path: str) -> int:
        """저장한 판을 그대로 글로 옮긴다. 지금 화면의 내용으로 다시 만들지 않는다."""
        row = self.db.snapshot(snapshot_id)
        text = handover_release.to_markdown(json.loads(row["payload"]), row["kind"])
        with safe_text_writer(path, self.db) as handle:
            handle.write(text)
        self.db.audit("work.snapshot.export", str(snapshot_id))
        return len(text)
