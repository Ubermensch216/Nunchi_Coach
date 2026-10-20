"""요약 표의 사실 보존, 흐름도 순서, 5색 제한과 확대 시 가독성을 검증한다."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QScrollArea

from app.core import reconstruction as rc, task_manual, workcard, workstate as ws
from app.core.work_brief import Brief, Fact, points, suggested_stages
from app.db import Database
from app.ui import theme
from app.ui.widgets.work_brief import ProcedureFlow, WorkBrief
from app.ui.widgets.task_card import color_for


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    theme.apply(app, 'medium', 'light')


def test_summary_keeps_conflicts_values_and_dates_without_promoting_them(tmp_path):
    db = Database(tmp_path / 'p.db')
    db.init()
    try:
        db.con.execute("INSERT INTO tasks(id,name,origin) VALUES (1,'행정사무감사','ai')")
        item = db.add_task_item(1, rc.SCOPE, '2025.11.1.~2026.10.31.', slot='감사대상기간')
        db.con.execute("UPDATE task_items SET basis_state='conflict', human_state='none' WHERE id=?", (item,))
        table = {s.key: s for s in task_manual.build(db, 1).sections}
        fact = next(f for f in table['overview'].brief.facts if f.label == '감사 대상 기간')
        assert fact.value == '2025.11.1.~2026.10.31.'
        assert '자료가 서로 다름' in fact.state
        assert table['procedure'].brief.ordered
        assert '제안' in table['procedure'].brief.caption
    finally:
        db.close()


def test_steps_without_predecessor_links_are_not_drawn_as_confirmed_sequence(tmp_path):
    db = Database(tmp_path / 'p.db')
    db.init()
    try:
        db.con.execute("INSERT INTO tasks(id,name) VALUES (1,'실적보고')")
        steps = [dict(id=n, label=f'단계 {n}', inputs='원본 실적', output='검토본', forms='검토 서식',
                      done_criteria='접수 확인', basis_state=ws.STATED, applicability=ws.CURRENT,
                      human_state=ws.NONE, procedure_kind=ws.PROCEDURE_STATED) for n in (1, 2)]
        card = workcard.Card(1, '실적보고', stated_steps=steps)
        flow = next(s.brief for s in task_manual.build(db, 1, card).sections if s.key == 'procedure')
        assert not flow.ordered and '선후관계' in flow.caption
        assert '검토 서식' in flow.stages[0].action and '접수 확인' in flow.stages[0].output
        steps[1]['after_step_id'] = 1
        linked = next(s.brief for s in task_manual.build(db, 1, card).sections if s.key == 'procedure')
        assert linked.ordered
    finally:
        db.close()


def test_sentence_points_do_not_split_korean_dates():
    assert points('기한: 2026. 11. 3.까지입니다. 수신처는 기획실입니다.') == [
        '기한: 2026. 11. 3.까지입니다', '수신처는 기획실입니다.']


def test_condensed_first_action_preserves_full_detail_and_access_note(tmp_path, qapp):
    db = Database(tmp_path / 'p.db')
    db.init()
    try:
        db.con.execute("INSERT INTO tasks(id,name) VALUES (1,'실적보고')")
        card = workcard.Card(1, '실적보고', precedent_steps=[dict(
            id=8, label='취합 (2025년 12월 중)', output='2025_실적보고_실적취합_최종.xlsx',
            basis_state=ws.INFERRED, applicability=ws.PAST, human_state=ws.NONE,
            procedure_kind=ws.PROCEDURE_PRECEDENT, evidence=[])],
            actions=[dict(type='do', step_id=8, gap_id=None, text='과거 취합 단계의 세부 설명과 원본 문서 확인')])
        first = next(s.brief for s in task_manual.build(db, 1, card).sections if s.key == 'first')
        assert first.facts[0].label == '지난 수행 사례' and '.xlsx' not in first.facts[0].value
        assert '과거 사례' in first.facts[0].state
        widget = WorkBrief(first)
        toggle = widget.findChild(QPushButton)
        assert toggle is not None
        assert not any('세부 설명' in l.text() for l in widget.findChildren(QLabel))
        toggle.click()
        assert any('세부 설명과 원본 문서 확인' in l.text() for l in widget.findChildren(QLabel))
        widget.close()
    finally:
        db.close()


@pytest.mark.parametrize('mode', ['light', 'dark'])
def test_palette_and_task_accents_share_only_five_colors(qapp, mode):
    theme.apply(qapp, 'medium', mode)
    tokens = theme.DARK if mode == 'dark' else theme.LIGHT
    assert set(tokens.values()) == set(theme.PALETTES[mode].values())
    assert len(set(tokens.values())) == 5
    assert {color_for(name) for name in ('행정사무감사', '예산관리', '계약관리', '월간실적')} == {theme.PRIMARY}
    def luminance(value):
        color = QColor(value)
        channels = [x / 12.92 if x <= .04045 else ((x + .055) / 1.055) ** 2.4
                    for x in (color.redF(), color.greenF(), color.blueF())]
        return sum(x * k for x, k in zip(channels, (.2126, .7152, .0722)))
    for foreground, background in ((theme.TEXT, theme.BG), (theme.TEXT, theme.SURFACE),
                                   (theme.PRIMARY, theme.SURFACE), (theme.TEXT_ON_PRIMARY, theme.PRIMARY)):
        a, b = sorted((luminance(foreground), luminance(background)))
        assert (b + .05) / (a + .05) >= 4.5


@pytest.mark.parametrize('width,size,mode', [(900, 'medium', 'light'), (560, 'large', 'dark')])
def test_fact_table_and_flow_wrap_without_clipping(qapp, width, size, mode):
    theme.apply(qapp, size, mode)
    brief = Brief(facts=[Fact('담당 역할', '부서별 실적과 증빙 취합 및 검토·제출 ' * 5, '과거 사례 · 적용 여부 미상')],
                  stages=suggested_stages(True), ordered=True, caption='준비 순서 제안')
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    content = WorkBrief(brief)
    scroll.setWidget(content)
    scroll.resize(width, 900)
    scroll.show()
    for _ in range(10):
        qapp.processEvents()
    try:
        assert scroll.horizontalScrollBar().maximum() == 0
        for label in content.findChildren(QLabel):
            if label.wordWrap():
                assert label.height() >= label.heightForWidth(label.width())
        flow = content.findChild(ProcedureFlow)
        assert flow.ordered and len(flow._stages) == 5
        arrows = [label.text() for label in flow.findChildren(QLabel) if label.objectName() == 'BriefFlowArrow']
        assert arrows == ['↓'] * 4
    finally:
        scroll.close()
