"""표·핵심 항목·벡터 흐름도로 표시하는 업무 안내. 확대해도 글자는 선명하다."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ...core.work_brief import Brief, points
from .common import WrapLabel, _WrapFrame, muted_label


def text(value, role='BriefText'):
    label = WrapLabel(value)
    label.setObjectName(role)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setMinimumWidth(0)
    return label


class BulletList(_WrapFrame):
    def __init__(self, values, parent=None):
        super().__init__(parent)
        self.setObjectName('BriefBullets')
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        for row, value in enumerate(values):
            marker = QLabel('•')
            marker.setObjectName('BriefBulletMark')
            grid.addWidget(marker, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(text(value), row, 1)
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.timeout.connect(self._reflow)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self._reflow_timer.start(0)

    def _reflow(self):
        # 패널 개폐로 폭이 줄면 WrapLabel이 각 행의 최소 높이를 바꾼다.
        # 크기 변경 이벤트가 끝난 뒤 배치해야 중간 행이 같은 위치에 겹치지 않는다.
        self.layout().invalidate()
        self.layout().activate()
        self.updateGeometry()


class FactTable(_WrapFrame):
    def __init__(self, facts, parent=None):
        super().__init__(parent)
        self.setObjectName('BriefFactTable')
        self.setAccessibleName('업무 핵심 정보 표')
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(1)
        for fact in facts:
            row = _WrapFrame()
            row.setObjectName('BriefFactRow')
            grid = QGridLayout(row)
            grid.setContentsMargins(12, 10, 12, 10)
            grid.setHorizontalSpacing(16)
            grid.setVerticalSpacing(4)
            key = text(fact.label, 'BriefFactKey')
            key.setFixedWidth(112)
            grid.addWidget(key, 0, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(text(fact.value, 'BriefFactValue'), 0, 1)
            if fact.state:
                grid.addWidget(text(fact.state, 'BriefState'), 1, 1)
            grid.setColumnStretch(1, 1)
            column.addWidget(row)


class ProcedureFlow(_WrapFrame):
    """확인된 선후관계 또는 명시적으로 제안한 순서에만 화살표를 그린다."""
    def __init__(self, stages, ordered=False, parent=None):
        super().__init__(parent)
        self.setObjectName('ProcedureFlow')
        self.setAccessibleName('업무 수행 흐름도' if ordered else '업무 수행 단계 목록')
        self.ordered = ordered
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self._stages = []
        for index, stage in enumerate(stages):
            holder = _WrapFrame()
            line = QHBoxLayout(holder)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(12)
            rail = QVBoxLayout()
            rail.setContentsMargins(0, 8, 0, 0)
            number = QLabel(f'{index + 1:02}')
            number.setObjectName('BriefStepNumber')
            number.setFixedSize(36, 36)
            number.setAlignment(Qt.AlignmentFlag.AlignCenter)
            rail.addWidget(number)
            rail.addStretch(1)
            line.addLayout(rail)
            panel = _WrapFrame()
            panel.setObjectName('BriefStepPanel')
            body = QVBoxLayout(panel)
            body.setContentsMargins(14, 12, 14, 12)
            body.setSpacing(6)
            heading = QHBoxLayout()
            heading.addWidget(text(stage.title, 'BriefStepTitle'), 1)
            heading.addWidget(text(stage.when, 'BriefStepWhen'), 1)
            body.addLayout(heading)
            body.addWidget(text('할 일 · ' + stage.action))
            if stage.output:
                body.addWidget(text('산출물 · ' + stage.output, 'BriefStepOutput'))
            if stage.state:
                body.addWidget(text(stage.state, 'BriefState'))
            line.addWidget(panel, 1)
            column.addWidget(holder)
            self._stages.append(panel)
            if index + 1 < len(stages):
                connector = QLabel('↓' if ordered else '·')
                connector.setObjectName('BriefFlowArrow')
                connector.setAlignment(Qt.AlignmentFlag.AlignVCenter)
                connector.setContentsMargins(12, 0, 0, 0)
                column.addWidget(connector)


class WorkBrief(_WrapFrame):
    def __init__(self, brief: Brief, parent=None):
        super().__init__(parent)
        self.setObjectName('WorkBrief')
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(12)
        if brief.caption:
            column.addWidget(muted_label(brief.caption, small=True))
        if brief.facts:
            column.addWidget(FactTable(brief.facts))
        if brief.bullets:
            column.addWidget(BulletList(brief.bullets))
        if brief.stages:
            column.addWidget(ProcedureFlow(brief.stages, brief.ordered))
        if brief.details:
            toggle = QPushButton(brief.details_label + ' 펼치기')
            toggle.setObjectName('Link')
            toggle.setCheckable(True)
            column.addWidget(toggle)
            detail_holder = _WrapFrame()
            detail_column = QVBoxLayout(detail_holder)
            detail_column.setContentsMargins(0, 0, 0, 0)
            detail_holder.hide()
            column.addWidget(detail_holder)
            def show_details(checked):
                if checked and detail_column.count() == 0:
                    detail_column.addWidget(BulletList(brief.details))
                detail_holder.setVisible(checked)
                toggle.setText(brief.details_label + (' 접기' if checked else ' 펼치기'))
            toggle.toggled.connect(show_details)


def section_content(section, lines):
    if section.brief is not None:
        return WorkBrief(section.brief)
    return BulletList([point for line in lines for point in points(line)])
