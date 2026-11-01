"""업무 화면의 교정 — 사람이 업무·문서·일정·처리 순서를 확정하는 조작.

AI가 지식을 만들고, 사람이 지식을 확정한다. 업무 화면(TasksView)이 이 믹스인을
받아 쓴다. 조작은 모두 db의 교정 함수를 지난다.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..widgets import muted_label, open_original


class TaskCorrectionsMixin:
    # ── 교정 ────────────────────────────────────────────────────────
    # AI가 지식을 만들고, 사람이 지식을 확정한다. 여기 있는 조작이 그
    # '확정' 쪽 절반이다 (계획서 §11). 모든 조작은 db의 교정 함수를 지나며,
    # 그 함수들이 재분석 보호와 기록을 함께 처리한다.
    #
    # 메뉴의 부모는 항상 그 메뉴를 여는 버튼이다. 뷰에 붙이면 두 가지가
    # 어긋난다. ① 분석 중에는 1.5초마다 다시 그리므로 뷰에 메뉴가 계속
    # 쌓인다. ② 부모 없이 만든 하위 메뉴는 파이썬 GC가 먼저 수거해 버려서
    # 눌렀을 때 빈 메뉴가 뜬다. 버튼에 붙이면 화면이 지워질 때 함께 사라진다.

    def _edit_menu(self, task_id: int, owner: QWidget) -> QMenu:
        menu = QMenu(owner)
        menu.addAction("업무명 수정", lambda: self._rename(task_id))
        menu.addAction("업무 설명 수정", lambda: self._edit_description(task_id))
        menu.addSeparator()
        merge = menu.addAction("다른 업무와 합치기", lambda: self._merge(task_id))
        merge.setEnabled(len(self.db.tasks()) > 1)
        menu.addAction("두 업무로 나누기", lambda: self._split(task_id))
        menu.addSeparator()
        menu.addAction("이건 업무가 아닙니다", lambda: self._not_a_task(task_id))
        return menu

    def _edit_description(self, task_id: int) -> None:
        row = self.db.task(task_id)
        text, ok = QInputDialog.getMultiLineText(
            self, "업무 설명 수정",
            "이 업무가 무엇인지 한두 문장으로 적어 주세요",
            row["description"] or "",
        )
        if not ok:
            return
        self.db.edit_task_description(task_id, text.strip())
        self.refresh()

    def _merge(self, task_id: int) -> None:
        others = [t for t in self.db.tasks() if t["id"] != task_id]
        if not others:
            return
        name = self.db.task(task_id)["name"]
        choice, ok = QInputDialog.getItem(
            self, "업무 합치기",
            f"'{name}'을(를) 어느 업무에 합칠까요?\n"
            f"문서가 모두 그쪽으로 옮겨지고, 이 업무는 목록에서 사라집니다.",
            [t["name"] for t in others], 0, False,
        )
        if not ok:
            return
        target = next(t for t in others if t["name"] == choice)
        counts = self.db.preview_task_edit(task_id)
        if QMessageBox.question(self, "합치기 미리보기",
                f"문서와 함께 항목 {counts['item']}건, 수행 단계 {counts['step']}개, "
                f"정보 공백 {counts['gap']}건을 '{choice}'로 옮깁니다.\n"
                "근거·검토·수행 기록을 보존하고, 변경 전 복원본을 저장합니다.\n계속할까요?") != QMessageBox.StandardButton.Yes:
            return
        if self.db.merge_tasks(task_id, target["id"]):
            self.open_task(target["id"])

    def _split(self, task_id: int) -> None:
        docs = self.db.task_documents(task_id)
        if len(docs) < 2:
            QMessageBox.information(
                self, "업무 나누기", "나누려면 문서가 2건 이상이어야 합니다."
            )
            return
        picked, name = SplitDialog.run(self, self.db.task(task_id)["name"], docs)
        if not picked:
            return
        counts = self.db.preview_task_edit(task_id, picked)
        if QMessageBox.question(self, "나누기 미리보기",
                f"선택한 문서의 항목 {counts['item']}건, 수행 단계 {counts['step']}개, "
                f"정보 공백 {counts['gap']}건을 새 업무로 옮깁니다.\n"
                f"양쪽 자료에 걸친 기록은 기존 업무에 남깁니다(재확인 {counts['recheck']}건).\n"
                "변경 전 복원본을 저장합니다. 계속할까요?") != QMessageBox.StandardButton.Yes:
            return
        new_id = self.db.split_task(task_id, picked, name)
        if new_id is None:
            QMessageBox.information(
                self, "업무 나누기", f"'{name}' 업무가 이미 있습니다. 다른 이름을 쓰세요."
            )
            return
        self.open_task(new_id)

    def _not_a_task(self, task_id: int) -> None:
        name = self.db.task(task_id)["name"]
        answer = QMessageBox.question(
            self, "업무 아님",
            f"'{name}'을(를) 업무 목록에서 내릴까요?\n\n"
            f"문서는 그대로 남고 삭제되지 않습니다. 다시 분석해도 이 판단은 "
            f"유지되므로 같은 묶음을 또 묻지 않습니다.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.db.mark_not_a_task(task_id)
        self.back()

    # ── 문서 교정 ───────────────────────────────────────────────────
    def _document_menu(self, task_id: int, doc_id: int, is_primary: bool,
                       owner: QWidget) -> QMenu:
        menu = QMenu(owner)
        if not is_primary:
            menu.addAction("대표 문서로 지정", lambda: self._set_primary(task_id, doc_id))
        others = [t for t in self.db.tasks() if t["id"] != task_id]
        if others:
            # addMenu(str)이 돌려주는 하위 메뉴는 주인이 없어 GC 대상이 된다.
            # 부모를 주고 만든 뒤에 붙인다.
            move = QMenu("다른 업무로 옮기기", menu)
            menu.addMenu(move)
            for other in others:
                move.addAction(
                    other["name"],
                    lambda _=False, t=other["id"]: self._move_document(task_id, t, doc_id),
                )
        menu.addSeparator()
        menu.addAction("이 업무 아님", lambda: self._detach(task_id, doc_id))
        return menu

    def _set_primary(self, task_id: int, doc_id: int) -> None:
        self.db.set_primary_document(task_id, doc_id)
        self.refresh()

    def _move_document(self, from_task: int, to_task: int, doc_id: int) -> None:
        self.db.move_document(from_task, to_task, doc_id)
        self.refresh()

    # ── When 교정 ───────────────────────────────────────────────────
    def _confirm_cycle(self, task_id: int) -> None:
        self.db.confirm_task_cycle(task_id)
        self.refresh()

    def _edit_cycle(self, task_id: int) -> None:
        cycle = self.db.task_cycle(task_id)
        current = cycle["months"] if cycle and cycle["kind"] == "yearly" else ""
        text, ok = QInputDialog.getText(
            self, "반복 주기 수정",
            "이 업무를 하는 달을 쉼표로 적어 주세요.  예: 9,10,11\n"
            "매달 하는 업무라면 '매월'이라고 적으세요.",
            text=current,
        )
        if not ok:
            return
        text = text.strip()
        if text in ("매월", "매달"):
            self.db.set_task_cycle(task_id, "monthly", "")
        else:
            months = sorted({
                int(p) for p in text.replace("월", "").split(",")
                if p.strip().isdigit() and 1 <= int(p) <= 12
            })
            if not months:
                QMessageBox.information(
                    self, "반복 주기 수정", "1~12 사이의 달을 쉼표로 구분해 적어 주세요."
                )
                return
            self.db.set_task_cycle(task_id, "yearly", ",".join(str(m) for m in months))
        self.refresh()

    def _no_cycle(self, task_id: int) -> None:
        self.db.mark_no_cycle(task_id)
        self.refresh()

    # ── How 교정 ────────────────────────────────────────────────────
    def _rename_step(self, step_id: int, current: str) -> None:
        text, ok = QInputDialog.getText(self, "단계 이름 수정", "단계 이름", text=current)
        if ok and text.strip():
            self.db.edit_step_label(step_id, text.strip())
            self.refresh()

    def _move_step(self, step_id: int, offset: int) -> None:
        self.db.move_step(step_id, offset)
        self.refresh()

    def _delete_step(self, step_id: int, label: str) -> None:
        answer = QMessageBox.question(self, "단계 삭제", f"'{label}' 단계를 뺄까요?")
        if answer == QMessageBox.StandardButton.Yes:
            self.db.delete_step(step_id)
            self.refresh()

    def _add_step(self, task_id: int, year: int, after_ordinal: int) -> None:
        text, ok = QInputDialog.getText(
            self, "단계 추가",
            "자료에는 없지만 실제로 있었던 단계를 적어 주세요.\n"
            "근거 문서가 없는 단계는 점선으로 표시됩니다.",
        )
        if ok and text.strip():
            self.db.add_step(task_id, year, text.strip(), after_ordinal)
            self.refresh()

    def _rename(self, task_id: int) -> None:
        row = self.db.task(task_id)
        name, ok = QInputDialog.getText(self, "업무명 수정", "업무 이름", text=row["name"])
        name = name.strip()
        if not ok or not name or name == row["name"]:
            return
        if not self.db.rename_task(task_id, name):
            QMessageBox.information(
                self, "업무명 수정", f"'{name}' 업무가 이미 있습니다. 다른 이름을 쓰세요."
            )
            return
        self.refresh()

    def _approve(self, task_id: int) -> None:
        self.db.confirm_task(task_id)
        self.refresh()

    def _detach(self, task_id: int, doc_id: int) -> None:
        self.db.detach_document(task_id, doc_id)
        self.refresh()

    def _mark_read(self, doc_id: int, done: bool) -> None:
        self.db.mark_reading(doc_id, done)
        self.refresh()

    def _open(self, path: str) -> None:
        open_original(self, self.db, path)


class SplitDialog(QDialog):
    """업무 나누기 — 떼어 낼 문서를 고르고 새 업무 이름을 짓는다.

    AI가 두 업무를 하나로 묶는 일은 흔하다(같은 부서, 비슷한 어휘). 그때
    사용자가 할 수 있는 일이 '삭제'뿐이면 자료를 잃는다. 그래서 나누기가
    교정 UX의 핵심 조작 중 하나다.
    """

    def __init__(self, parent: QWidget, task_name: str, docs) -> None:
        super().__init__(parent)
        self.setWindowTitle("업무 나누기")
        self.setMinimumWidth(520)

        column = QVBoxLayout(self)
        column.setSpacing(theme.SP_MD)
        column.addWidget(
            muted_label(f"'{task_name}'에서 떼어 낼 문서를 고르세요. "
                        f"고르지 않은 문서는 그대로 남습니다.")
        )

        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        for doc in docs:
            item = QListWidgetItem(f"{doc['filename']}   ({_when(doc)})")
            item.setData(Qt.ItemDataRole.UserRole, doc["id"])
            self.list.addItem(item)
        column.addWidget(self.list)

        column.addWidget(muted_label("새 업무 이름", small=True))
        self.name = QLineEdit()
        self.name.setPlaceholderText("예: 행감 질의대응")
        column.addWidget(self.name)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)

        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setEnabled(False)
        self.list.itemSelectionChanged.connect(self._revalidate)
        self.name.textChanged.connect(self._revalidate)

    def _revalidate(self) -> None:
        self._ok.setEnabled(
            bool(self.list.selectedItems()) and bool(self.name.text().strip())
        )

    @classmethod
    def run(cls, parent: QWidget, task_name: str, docs) -> tuple[list[int], str]:
        dialog = cls(parent, task_name, docs)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return [], ""
        picked = [
            item.data(Qt.ItemDataRole.UserRole) for item in dialog.list.selectedItems()
        ]
        return picked, dialog.name.text().strip()


def _when(row) -> str:
    if not row["eff_date"]:
        return "시점 미상"
    precision = row["eff_precision"] or "day"
    year, month, *_ = (row["eff_date"] or "").split("-") + ["", ""]
    if precision == "year":
        text = f"{year}년"
    elif precision == "month":
        text = f"{year}.{int(month):02d}"
    else:
        text = row["eff_date"]
    if row["eff_date_kind"] == "fs":
        text += " (파일 날짜)"
    return text
