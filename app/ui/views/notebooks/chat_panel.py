"""가운데 판 — 선택한 자료만 근거로 묻고 답을 쌓아 가는 대화.

질문 화면(views/ask.py)은 단발 질의다. 여기는 지난 질문과 답이 위로 쌓이고
입력칸이 아래에 놓인다. 다만 과거의 AI 답변을 근거로 다시 쓰지는 않는다 —
답의 근거는 언제나 지금 선택한 원문이다(core/notebook.py query).

자료 선택이 달라지면 그 전에 받은 답은 가린다. 선택에서 뺀 자료의 내용이 지난
답에 남아 화면에 보이면 '이 자료만 근거로 한다'는 약속이 깨진다.
"""

from __future__ import annotations

import json
import time
from dataclasses import fields

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ....ai.settings import model_names
from ....core import notebook
from ....law import service as law_service
from ....jobs import AskRunner, WarmupRunner
from ....search.query import QueryScope
from ....search.rag import Answer, Citation, withheld_reason
from ... import icons, theme
from ...widgets import (
    Card,
    EmptyState,
    UnknownBlock,
    WrapLabel,
    clear_layout,
    muted_label,
    section_title,
)
from ...widgets.common import fit_wrapped_text, friendly_locator
from ..ask import ASK_INPUT_H
from .answer_html import answer_html
from .panel import ICON_PX, icon_button, set_icon

# 대화를 시작하기 전에 보여 주는 자료 안내 수. 자료가 많으면 앞의 몇 건만 보인다.
GUIDE_CARDS = 5
# 한 번에 그리는 대화 수. 긴 업무노트를 열 때마다 수백 장의 카드를 짓지 않는다.
THREAD_PAGE = 20
# 보내기·멈춤 단추 한 변. 입력칸(ASK_INPUT_H)보다 조금 작아 테두리 안에 여백이 남는다.
SEND_PX = 36
# 이보다 짧은 질문은 글자 폭만큼만 차지하는 한 줄 말풍선이 된다. 가장 좁은 대화 판
# (workspace.CHAT_MIN_W)에도 들어가는 폭이다.
BUBBLE_ONE_LINE_W = 320
# 답의 오른쪽에 비워 두는 폭. 질문은 오른쪽, 답은 왼쪽 — 누가 한 말인지 자리로 갈린다.
ANSWER_GUTTER = 48
_CITATION_FIELDS = {field.name for field in fields(Citation)}


def answer_of(row) -> Answer:
    """저장한 대화 한 줄을 화면이 쓰는 Answer로 되돌린다."""
    citations = [Citation(**{key: value for key, value in proof.items() if key in _CITATION_FIELDS})
                 for proof in json.loads(row["citations"])]
    generation = json.loads(row["generation"])
    generation["notebook_message_id"] = row["id"]
    return Answer(row["question"], row["answer"], bool(row["withheld"]),
                  citations=citations, generation=generation, model=row["model"])


class ChatPanel(QWidget):
    citation_requested = Signal(object)   # Citation — 왼쪽 판이 원문을 편다
    answered = Signal()
    note_saved = Signal()

    def __init__(self, db, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self.notebook_id: int | None = None
        self.editable = False
        self._answer: Answer | None = None       # 방금 받은 답 (저장되지 않은 오류 포함)
        self._pending: str | None = None         # 답을 기다리는 질문
        self._limit = THREAD_PAGE
        self._signature = None
        self._started_at = 0.0
        self._whole = False                      # 지금 기다리는 것이 전체 정리인가
        self._batches = (0, 0)                   # 전체 정리의 (끝난 묶음, 전체 묶음)

        self._runner = AskRunner(db.path, self)
        self._runner.done.connect(self._on_answer)
        self._runner.cancelled.connect(self._on_cancelled)
        self._runner.progress.connect(self._on_progress)
        self._warmup = WarmupRunner(self)
        self._elapsed = QTimer(self)
        self._elapsed.setInterval(1000)
        self._elapsed.timeout.connect(self._update_waiting)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(theme.SP_SM)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("AnswerPane")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName("PaneBody")
        self.thread = QVBoxLayout(holder)
        self.thread.setContentsMargins(0, 0, theme.SP_SM, 0)
        self.thread.setSpacing(theme.SP_MD)
        self.thread.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(holder)
        root.addWidget(self.scroll, 1)

        self.waiting = muted_label("", small=True)
        self.waiting.hide()
        root.addWidget(self.waiting)

        composer = QFrame()
        composer.setObjectName("ChatComposer")
        row = QHBoxLayout(composer)
        row.setContentsMargins(theme.SP_MD, theme.SP_SM, theme.SP_SM, theme.SP_SM)
        row.setSpacing(theme.SP_SM)
        self.input = QLineEdit()
        self.input.setObjectName("ChatInput")
        self.input.setPlaceholderText("선택한 자료에 대해 질문하세요")
        self.input.setMinimumHeight(ASK_INPUT_H)
        self.input.returnPressed.connect(self._ask)
        row.addWidget(self.input, 1)
        self.law = QCheckBox("법령")
        self.law.setToolTip(
            "이 질문에 대해 현행 법령 조문을 국가법령정보센터(law.go.kr)에서 함께 조회합니다.\n"
            "밖으로 보내는 것은 법령 이름·조문 번호와 이 질문 문장뿐입니다. 자료의 본문은 보내지 않습니다.\n"
            "질문마다 다시 골라야 합니다.")
        row.addWidget(self.law)
        self.scope = muted_label("", small=True, wrap=False)
        row.addWidget(self.scope)
        # 보내기와 멈춤은 한 단추다. 답을 만드는 동안에는 보낼 수 없으므로 같은 자리가
        # 멈춤이 된다 — 단추 둘이 번갈아 나타나면 입력칸의 폭이 그때마다 흔들린다.
        self.send = QPushButton()
        self.send.setObjectName("ChatSend")
        self.send.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send.setFixedSize(SEND_PX, SEND_PX)
        self.send.setIconSize(QSize(ICON_PX + 2, ICON_PX + 2))
        self.send.clicked.connect(self._send_or_stop)
        self.input.textChanged.connect(lambda *_: self._sync_send())
        self._stopping = False
        row.addWidget(self.send)
        root.addWidget(composer)

        self.notice = muted_label("", small=True)
        self.notice.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(self.notice)

    # ── 상태 ────────────────────────────────────────────────────────
    def set_notebook(self, notebook_id: int | None, editable: bool) -> None:
        if notebook_id != self.notebook_id:
            self._runner.cancel()
            self._answer = None
            self._pending = None
            self._limit = THREAD_PAGE
        self.notebook_id = notebook_id
        self.editable = editable
        self._signature = None
        self.refresh()

    def snapshot(self) -> dict:
        if self.notebook_id is None or not self.editable:
            return {}
        return notebook.selected_snapshot(self.db, self.notebook_id)

    def refresh(self) -> None:
        """입력칸의 준비 상태를 맞추고, 달라진 것이 있을 때만 대화를 다시 그린다."""
        snapshot = self.snapshot()
        ready = bool(snapshot)
        if ready:
            self._warmup.configure(*model_names(self.db))
            self._warmup.start()   # 한 번만 돈다
        busy = self._runner.running
        self.input.setEnabled(ready and not busy)
        self._sync_send(ready)
        # 설정에서 켜고 인증키를 넣은 인수인계에서만 보인다. 꺼져 있으면 자리도 차지하지 않는다.
        self.law.setVisible(law_service.available(self.db))
        self.law.setEnabled(ready and not busy)
        self.scope.setText(f"자료 {len(snapshot)}건" if ready else "")
        if self.notebook_id is None:
            self.notice.setText("")
        elif not self.editable:
            self.notice.setText("보관한 업무노트입니다. 보관을 해제하면 다시 질문할 수 있습니다.")
        elif not ready:
            self.notice.setText("왼쪽 출처에서 근거로 쓸 자료를 선택하세요.")
        else:
            self.notice.setText("선택한 원문만 근거로 답합니다. 답은 틀릴 수 있으니 근거 원문을 확인하세요.")

        rows = notebook.messages(self.db, self.notebook_id) if self.notebook_id is not None else []
        guides = self._guides(snapshot) if not rows else []
        signature = (self.notebook_id, self.editable, self._limit, self._pending, tuple(guides),
                     tuple((row["id"], row["rating"]) for row in rows),
                     notebook.dumps(snapshot), id(self._answer),
                     theme.BG)   # 답은 색을 직접 칠한다 — 테마가 바뀌면 다시 그린다
        if signature != self._signature:
            self._signature = signature
            self._render(rows, snapshot, guides)

    def _guides(self, snapshot: dict) -> list[tuple[str, str, tuple[str, ...]]]:
        """선택한 자료의 (파일명, 요약, 주제어). 자료를 올릴 때 만들어 둔 것이다(core/notebook_ingest.py)."""
        guides = []
        for row in notebook.sources(self.db, self.notebook_id) if snapshot else []:
            if str(row["doc_id"]) not in snapshot:
                continue
            topics = tuple(t for t in json.loads(row["topics"] or "[]") if isinstance(t, str))
            if row["summary"] or topics:
                guides.append((row["filename"], row["summary"], topics))
        return guides

    # ── 대화 그리기 ─────────────────────────────────────────────────
    def _render(self, rows, snapshot: dict, guides=()) -> None:
        clear_layout(self.thread)
        if self.notebook_id is None:
            return
        if not rows and self._pending is None and self._answer is None:
            self.thread.addWidget(EmptyState(
                "자료에 대해 물어보세요",
                "왼쪽에서 선택한 자료만 근거로 답합니다. 답의 번호를 누르면 "
                "그 문서의 원문이 왼쪽에 펼쳐집니다."))

            task_row = self.db.con.execute(
                "SELECT task_id FROM notebooks WHERE id=?", (self.notebook_id,)
            ).fetchone()
            task_id = task_row[0] if task_row and task_row[0] else None
            if task_id:
                try:
                    from ....core import notebook_tasks
                    questions = notebook_tasks.suggested_questions(self.db, task_id)
                    if questions:
                        self.thread.addWidget(muted_label("💡 추천 질문 (누르면 바로 입력창에 채워집니다)", small=True))
                        for q in questions:
                            btn = QPushButton(q)
                            btn.setObjectName("Quiet")
                            btn.setCursor(Qt.CursorShape.PointingHandCursor)
                            btn.setStyleSheet("text-align: left; padding: 6px 10px; margin-bottom: 2px;")
                            btn.clicked.connect(lambda _, text=q: self._fill(text))
                            self.thread.addWidget(btn)
                except Exception as exc:
                    from ....core import diagnostics
                    diagnostics.record("notebook.suggested_questions.error", exc)
            # 무엇을 물을지 정하기 전에 자료가 무엇인지부터 보인다.
            if guides:
                self.thread.addWidget(muted_label("AI가 만든 자료 안내 · 원문이 아닙니다", small=True))
            for filename, summary, topics in guides[:GUIDE_CARDS]:
                card = Card(tone="static")
                card.body.addWidget(section_title(filename))
                if summary:
                    card.body.addWidget(muted_label(summary))
                if topics:
                    card.body.addWidget(muted_label("주제어 — " + " · ".join(topics), small=True))
                self.thread.addWidget(card)
            if len(guides) > GUIDE_CARDS:
                self.thread.addWidget(muted_label(
                    f"그 밖의 자료 {len(guides) - GUIDE_CARDS}건의 안내는 왼쪽 출처에서 원문 보기로 볼 수 있습니다.",
                    small=True))
            return
        hidden = max(0, len(rows) - self._limit)
        if hidden:
            more = QPushButton(f"이전 대화 {hidden}건 더 보기")
            more.setObjectName("Fold")
            more.setCursor(Qt.CursorShape.PointingHandCursor)
            more.clicked.connect(self._show_more)
            self.thread.addWidget(more)
        for row in rows[hidden:]:
            self.thread.addWidget(self._question_block(row["question"]))
            if row["standalone_question"]:
                # 후속 질문을 어떻게 풀어 찾았는지 숨기지 않는다. 잘못 풀었으면 여기서 보인다.
                self.thread.addWidget(muted_label(
                    f"앞선 질문을 이어 이렇게 찾았습니다 — {row['standalone_question']}", small=True))
            if json.loads(row["source_snapshot"]) != snapshot:
                self.thread.addWidget(muted_label(
                    "이 답을 받은 뒤 자료 선택이나 원문 판본이 달라져 답을 가렸습니다. "
                    "같은 질문을 다시 하면 지금 선택한 자료로 답합니다.", small=True))
                continue
            self.thread.addWidget(self._answer_block(answer_of(row), row))
            statutes = json.loads(row["law_citations"] or "{}")
            if isinstance(statutes, dict) and (statutes.get("citations") or statutes.get("problems")):
                self.thread.addWidget(self._law_block(statutes))
        if self._pending is not None:
            self.thread.addWidget(self._question_block(self._pending))
            self.thread.addWidget(self._thinking_block())
        elif self._answer is not None and "notebook_message_id" not in self._answer.generation:
            # 저장되지 않은 결과(오류·취소)는 그 자리에서만 보여 준다.
            self.thread.addWidget(self._question_block(self._answer.question))
            self.thread.addWidget(self._answer_block(self._answer, None))
        QTimer.singleShot(0, self._scroll_to_end)

    def _fill(self, text: str) -> None:
        """추천 질문을 입력칸에 채운다. 바로 보내지 않고, 고쳐 쓸 수 있게 커서를 끝에 둔다."""
        self.input.setText(text)
        self.input.setFocus()
        self.input.setCursorPosition(len(text))

    def _scroll_to_end(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _show_more(self) -> None:
        self._limit += THREAD_PAGE
        self.refresh()

    def _question_block(self, question: str) -> QWidget:
        """내가 한 말 — 오른쪽에 붙는 말풍선."""
        holder = QWidget()
        fit_wrapped_text(holder)
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        bubble = QFrame()
        bubble.setObjectName("ChatQuestion")
        column = QVBoxLayout(bubble)
        column.setContentsMargins(theme.SP_LG, theme.SP_SM, theme.SP_LG, theme.SP_SM)
        # 짧은 말은 제 폭만큼만, 긴 말은 판의 4/5까지 쓰고 접힌다.
        long = "\n" in question or self.fontMetrics().horizontalAdvance(question) > BUBBLE_ONE_LINE_W
        label = WrapLabel(question) if long else QLabel(question)
        label.setObjectName("ChatQuestionText")
        label.setWordWrap(long)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(label)
        if long:
            fit_wrapped_text(bubble)
            row.addWidget(bubble, 4)
        else:
            bubble.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            row.addWidget(bubble)
        return holder

    def _answer_card(self) -> Card:
        """AI가 한 말 — 왼쪽에 붙는, 테두리 없는 글."""
        card = Card()
        card.setObjectName("ChatAnswer")
        card.body.setContentsMargins(theme.SP_XS, 0, ANSWER_GUTTER, theme.SP_SM)
        card.body.setSpacing(theme.SP_SM)
        return card

    def _thinking_block(self) -> QWidget:
        """생성 중 표시. 질문은 바로 위 말풍선에 있으므로 다시 적지 않는다."""
        card = self._answer_card()
        card.body.addWidget(muted_label("자료에서 찾아 답을 만들고 있습니다", small=True))
        bar = QProgressBar()
        bar.setObjectName("Thinking")
        bar.setRange(0, 0)          # 값 없는 진행 — 남은 시간을 아는 척하지 않는다
        bar.setTextVisible(False)
        bar.setFixedSize(220, 3)
        card.body.addWidget(bar)
        return card

    def _rich(self, text: str, answer: Answer | None = None) -> QLabel:
        """답 본문. 목록·표·굵은 글씨를 알아보고, 근거 번호는 누를 수 있게 한다."""
        label = WrapLabel(answer_html(text))
        label.setObjectName("AnswerBody")
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setOpenExternalLinks(False)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        if answer is not None:
            label.linkActivated.connect(lambda index, a=answer: self._open_index(a, index))
        return label

    def _answer_block(self, answer: Answer, row) -> QWidget:
        card = self._answer_card()
        body = card.body
        if answer.error:
            body.addWidget(UnknownBlock(answer.error))
            return card
        if answer.generation.get("mode") == "search_only":
            body.addWidget(section_title("자료 검색 결과"))
            body.addWidget(muted_label(answer.text))
            guidance = answer.guidance or answer.generation.get("guidance")
            if guidance:
                body.addWidget(muted_label(guidance, small=True))
            self._add_citations(body, answer)
            return card
        if answer.withheld:
            # 첫 줄이 '왜 확정하지 못했는가'이고, 그 아래는 견줄 값들이다(수량 대조).
            reason, _, detail = withheld_reason(answer).partition("\n")
            body.addWidget(UnknownBlock(reason))
            if detail.strip():
                body.addWidget(self._rich(detail, answer))
            if answer.guidance:
                body.addWidget(muted_label(answer.guidance, small=True))
            if answer.citations:
                body.addWidget(muted_label("확정하지 않은 원문 근거", small=True))
                self._add_citations(body, answer)
            if row is not None and self.editable and answer.generation.get("mode") != "overview":
                body.addWidget(self._whole_button(answer.question), 0, Qt.AlignmentFlag.AlignLeft)
            return card

        if answer.generation.get("mode") == "overview":
            body.addWidget(muted_label("선택한 자료 전체를 훑어 정리했습니다. 문장마다 근거 원문을 확인하세요.",
                                       small=True))
        body.addWidget(self._rich(answer.text, answer))
        for caveat in answer.generation.get("caveats") or []:
            body.addWidget(muted_label(caveat, small=True))
        self._add_citations(body, answer)
        if row is not None:
            body.addLayout(self._actions(answer, row))
        return card

    def _law_block(self, statutes: dict) -> QWidget:
        """현행 법령에서 조회한 답. 자료 답변과 다른 판에 둔다 — 근거의 성격이 다르다."""
        card = Card()
        card.setObjectName("AnswerCard")
        body = card.body
        body.addWidget(section_title("현행 법령"))
        if statutes.get("text"):
            body.addWidget(self._rich(statutes["text"]))
        for problem in statutes.get("problems") or []:
            body.addWidget(UnknownBlock(problem))
        for citation in statutes.get("citations") or []:
            label = f"법{citation['index']}  「{citation['law_name']}」 {citation['article']}"
            if citation.get("title"):
                label += f"({citation['title']})"
            if citation.get("effective_date"):
                label += f" · 시행 {citation['effective_date']}"
            if citation.get("origin") == "document":
                label += " · 자료가 인용한 조문"
            chip = QPushButton(label)
            chip.setObjectName("Chip")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setToolTip("조문 본문 보기")
            chip.clicked.connect(lambda _=False, c=citation: self._show_article(c))
            body.addWidget(chip)
        if statutes.get("citations"):
            when = (statutes["citations"][0].get("fetched_at") or "").replace("T", " ")
            body.addWidget(muted_label(
                f"국가법령정보센터 조회 {when} 기준입니다. 법률 자문이 아니며, 적용 여부는 조문 원문과 "
                "소관 부서의 해석으로 확인하세요.", small=True))
        return card

    def _show_article(self, citation: dict) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(f"「{citation['law_name']}」 {citation['article']}")
        layout = QVBoxLayout(dialog)
        head = citation.get("title") or ""
        if citation.get("effective_date"):
            head += f" · 시행 {citation['effective_date']}"
        if head:
            layout.addWidget(muted_label(head.strip(" ·")))
        text = QPlainTextEdit(citation.get("text") or "")
        text.setReadOnly(True)
        layout.addWidget(text, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        if citation.get("url"):
            site = buttons.addButton("law.go.kr에서 열기", QDialogButtonBox.ButtonRole.ActionRole)
            site.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(citation["url"])))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(640, 460)
        dialog.exec()

    def _add_citations(self, body, answer: Answer) -> None:
        for citation in answer.citations:
            where = friendly_locator(citation.locator)
            chip = QPushButton(f"{citation.index}  {citation.filename}" + (f" · {where}" if where else ""))
            chip.setObjectName("Chip")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setToolTip("\n".join(p for p in (citation.note, citation.snippet or citation.filename) if p))
            chip.clicked.connect(lambda _=False, c=citation: self.citation_requested.emit(c))
            body.addWidget(chip, 0, Qt.AlignmentFlag.AlignLeft)

    def _open_index(self, answer: Answer, index: str) -> None:
        for citation in answer.citations:
            if str(citation.index) == index:
                self.citation_requested.emit(citation)
                return

    def _actions(self, answer: Answer, row) -> QHBoxLayout:
        """답 아래의 작은 단추들. 글자 없이 그림만 두고, 뜻은 툴팁이 말한다."""
        line = QHBoxLayout()
        line.setSpacing(theme.SP_XS)
        copy = icon_button("copy", "답 복사", "복사")
        copy.clicked.connect(lambda _=False, b=copy, t=answer.text: self._copy(b, t))
        line.addWidget(copy)
        if self.editable:
            save = icon_button("bookmark-add",
                               "메모로 저장 — 이 답을 오른쪽 스튜디오의 메모로 남깁니다. 저장한 메모는 AI 초안입니다.",
                               "메모로 저장")
            save.clicked.connect(lambda _=False, r=row: self._save_note(r))
            line.addWidget(save)
            if answer.generation.get("mode") != "overview":
                line.addWidget(self._whole_button(answer.question))
            if answer.text and self._task_id() is not None:
                apply = icon_button("tasks",
                                    "업무에 반영 — 이 답에서 확인한 사실(기한·제출처 등)을 업무 항목으로 올립니다. "
                                    "사람이 보탠 내용으로 남고 다음 정리본 판에 실립니다.",
                                    "업무에 반영")
                apply.clicked.connect(lambda _=False, t=answer.text: self._ask_apply(t))
                line.addWidget(apply)
        for name, value, label, done in (("thumb-up", "right", "맞습니다", "맞다고 남김"),
                                         ("thumb-down", "wrong", "사실과 다릅니다", "사실과 다르다고 남김")):
            chosen = row["rating"] == value
            if self.editable:
                # 고른 쪽은 강조색으로 남는다. 다시 누르면 평가를 거둔다.
                button = icon_button(name, f"{done} · 다시 누르면 취소" if chosen else label, label,
                                     theme.PRIMARY if chosen else None)
                button.clicked.connect(
                    lambda _=False, key=row["id"], v=None if chosen else value: self._rate(key, v))
                line.addWidget(button)
            elif chosen:
                mark = icons.label(name, theme.PRIMARY, ICON_PX) or muted_label(done, small=True, wrap=False)
                mark.setToolTip(done)
                line.addWidget(mark)
        line.addStretch(1)
        return line

    def _task_id(self) -> int | None:
        if self.notebook_id is None:
            return None
        row = self.db.con.execute("SELECT task_id FROM notebooks WHERE id=?", (self.notebook_id,)).fetchone()
        return row["task_id"] if row is not None and row["task_id"] else None

    def _ask_apply(self, text: str) -> None:
        from ....core import notebook_tasks, reconstruction as rc
        kinds = [k for k in rc.KIND_LABEL if k not in (rc.RECORD, rc.PROCEDURE, rc.STATEMENT)]
        labels = [rc.KIND_LABEL[k] for k in kinds]
        label, ok = QInputDialog.getItem(self, "업무에 반영", "무엇에 관한 사실인가요?", labels,
                                         labels.index(rc.KIND_LABEL[rc.NOTE]), False)
        if not ok:
            return
        content, ok = QInputDialog.getText(
            self, "업무에 반영", "확인한 내용을 다듬어 적어 주세요. 사람이 보탠 내용으로 남습니다.",
            text=" ".join(text.split())[:200])
        if not ok or not content.strip():
            return
        notebook_tasks.apply_answer(self.db, self.notebook_id, kinds[labels.index(label)], content)
        QMessageBox.information(self, "업무에 반영",
                                "업무 항목으로 올렸습니다. 업무 화면과 다음 정리본 판에 실립니다.")

    def _whole_button(self, question: str) -> QPushButton:
        """보통 질문으로 받은 답이 모자랄 때 — 같은 질문을 자료 전체를 훑어 다시 받는다."""
        button = QPushButton("자료 전체를 훑어 다시 정리")
        button.setObjectName("Fold")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip("가장 관련 있는 대목만 보고 한 답입니다. 선택한 자료 전체를 차례로 훑어 다시 정리합니다.\n"
                          "보통 질문보다 훨씬 오래 걸립니다(자료가 많으면 몇 분).")
        button.setEnabled(not self._runner.running)
        button.clicked.connect(lambda _=False, q=question: self._start(q, whole=True))
        return button

    def _copy(self, button: QPushButton, text: str) -> None:
        QApplication.clipboard().setText(text)
        # 복사는 눈에 보이는 결과가 없다. 그림을 잠깐 체크로 바꿔 '됐다'를 알린다.
        set_icon(button, "check", theme.CONFIRMED)
        QTimer.singleShot(1200, button, lambda: set_icon(button, "copy"))

    def _save_note(self, row) -> None:
        try:
            notebook.save_note(self.db, self.notebook_id, row["id"], row["question"])
        except ValueError as exc:
            QMessageBox.information(self, "메모로 저장", str(exc))
            return
        self.note_saved.emit()

    def _rate(self, message_id: int, rating: str | None) -> None:
        notebook.rate(self.db, message_id, rating)
        self.refresh()

    # ── 질문 ────────────────────────────────────────────────────────
    def _sync_send(self, ready: bool | None = None) -> None:
        """단추의 그림과 하는 일을 지금 상태에 맞춘다.

        idle  보낼 글을 기다린다 — 옅은 Enter 그림
        ready 보낼 글이 있다 — 강조색 Enter 그림
        busy  답을 만드는 중 — 멈춤 그림. 누르면 질문을 멈춘다
        """
        if ready is None:
            ready = bool(self.snapshot())
        busy = self._runner.running
        if busy:
            state, name, color = "busy", "stop", theme.BG
            label = "멈추는 중" if self._stopping else "질문 멈춤"
            self.send.setEnabled(not self._stopping)
        else:
            self._stopping = False
            typed = ready and bool(self.input.text().strip())
            state, name = ("ready", "enter") if typed else ("idle", "enter")
            color = theme.TEXT_ON_PRIMARY if typed else theme.TEXT_MUTED
            label = "질문 보내기 (Enter)"
            self.send.setEnabled(ready)
        self.send.setAccessibleName(label)
        self.send.setToolTip(label)
        set_icon(self.send, name, color if self.send.isEnabled() else theme.TEXT_DISABLED)
        if self.send.property("state") != state:
            self.send.setProperty("state", state)
            self.send.style().unpolish(self.send)
            self.send.style().polish(self.send)

    def _send_or_stop(self) -> None:
        if self._runner.running:
            self._cancel_question()
        else:
            self._ask()

    def _ask(self) -> None:
        if self._start(self.input.text().strip()):
            self.input.clear()

    def _start(self, question: str, whole: bool = False) -> bool:
        if not question or self._runner.running or self.notebook_id is None or not self.snapshot():
            return False
        statutes = self.law.isChecked() and law_service.available(self.db)
        if not self._runner.start(question, QueryScope(
                notebook_id=self.notebook_id, mode="overview" if whole else "", law=statutes)):
            return False
        # 주제 없는 요약 요청은 고르지 않아도 전체 정리로 받는다(core/notebook.py). 기다리는 말을 맞춘다.
        self._whole = whole or notebook.effective_mode(self.db, self.notebook_id, question) == "overview"
        self._batches = (0, 0)
        self.law.setChecked(False)        # 밖으로 나가는 조회는 질문마다 다시 고르게 한다
        self._pending = question
        self._answer = None
        self._started_at = time.monotonic()
        self.waiting.show()
        self._update_waiting()
        self._elapsed.start()
        self.refresh()
        return True

    def _on_progress(self, done: int, total: int) -> None:
        self._batches = (done, total)
        self._update_waiting()

    def _update_waiting(self) -> None:
        seconds = int(time.monotonic() - self._started_at)
        done, total = self._batches
        if self._whole and total:
            # 남은 시간은 말하지 않는다 — 묶음마다 걸리는 시간이 고르지 않다.
            hint = f"선택한 자료 전체를 훑고 있습니다 · {total}묶음 중 {min(done + 1, total)}번째"
        elif self._whole:
            hint = "선택한 자료 전체를 차례로 훑고 있습니다. 자료가 많으면 몇 분 걸립니다."
        else:
            hint = ("처음 질문할 때는 모델을 준비하는 시간이 더 걸릴 수 있습니다." if seconds >= 10
                    else "선택한 자료에서 근거를 찾아 답변을 준비하고 있습니다.")
        self.waiting.setText(f"{seconds}초 경과 · {hint}")

    def _cancel_question(self) -> None:
        self._elapsed.stop()
        self.waiting.setText("질문을 멈추고 있습니다…")
        self._stopping = True
        self._sync_send()
        self._runner.cancel()

    def _finish_waiting(self) -> None:
        self._elapsed.stop()
        self._stopping = False
        self.waiting.hide()

    def _on_answer(self, answer: Answer) -> None:
        self._finish_waiting()
        self._pending = None
        self._answer = answer
        self.refresh()
        self.input.setFocus()
        self.answered.emit()

    def _on_cancelled(self) -> None:
        self._finish_waiting()
        question, self._pending = self._pending, None
        self._answer = None
        self.refresh()
        self.input.setText(question or "")
        self.input.setFocus()
