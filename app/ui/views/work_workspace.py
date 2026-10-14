"""업무 화면의 홈·매뉴얼·열람 상태. 원본 자료와 수행 기록은 읽기만 한다."""
from __future__ import annotations

import json
from datetime import date

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QWidget,
                              QLineEdit, QPushButton, QScrollArea, QVBoxLayout)

from ...core import manual_versions, matters, work_schedule, workcard, workstate as ws
from ...core.knowledge import review
from ..widgets import Card, Evidence, WorkYearDashboard, clear_layout, muted_label, section_title

STATE_KEY = 'ui.work_workspace.v1'


class WorkspaceMixin:
    def _init_workspace(self):
        try:
            self._ui_state = json.loads(self.db.get_meta(STATE_KEY) or '{}')
        except (ValueError, TypeError):
            self._ui_state = {}
        if not isinstance(self._ui_state, dict):
            self._ui_state = {}
        for key in ('tasks', 'home'):
            if not isinstance(self._ui_state.get(key), dict):
                self._ui_state[key] = {}
        self._rendering = False
        self._data_stamp = None
        self._render_generation = 0
        self._home_cards = []
        self._restore_callback = None
        self._restore_phase = 0
        self._restore_timer = QTimer(self)
        self._restore_timer.setSingleShot(True)
        self._restore_timer.timeout.connect(self._apply_restore)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.refresh)

    def _stamp(self):
        return self.db.con.total_changes, self.db.con.execute('PRAGMA data_version').fetchone()[0]

    def _state(self):
        if self._task_id is None:
            return self._ui_state['home']
        key = str(self._task_id)
        state = self._ui_state['tasks'].get(key)
        if not isinstance(state, dict):
            state = {}
            self._ui_state['tasks'][key] = state
        return state

    def _persist(self):
        # ui meta 쓰기는 화면 데이터 변경으로 세지 않는다.
        before = self.db.con.total_changes
        self.db.set_meta(STATE_KEY, json.dumps(self._ui_state, ensure_ascii=False))
        if self._data_stamp is not None:
            total, version = self._data_stamp
            self._data_stamp = (total + self.db.con.total_changes - before, version)

    def _capture_workspace(self):
        if self._rendering:
            return
        state = self._state()
        if self._task_id is not None:
            state['tab'] = self._tab
            state['how_year'] = self._how_year.get(self._task_id)
            state['how_against'] = self._how_against.get(self._task_id)
            pages = self.pages
        else:
            pages = {'home': self.list_page}
        scrolls = state.get('scroll')
        if not isinstance(scrolls, dict):
            scrolls = {}
            state['scroll'] = scrolls
        key = self._tab if self._task_id is not None else 'home'
        scrolls[key] = pages[key].findChild(QScrollArea).verticalScrollBar().value()
        self._persist()

    def _scroll_changed(self):
        if not self._rendering:
            self._capture_workspace()

    def _restore_workspace(self):
        generation = self._render_generation
        scrolls = self._state().get('scroll', {})
        scrolls = scrolls if isinstance(scrolls, dict) else {}
        def restore():
            if generation != self._render_generation:
                return
            pages = self.pages if self._task_id is not None else {'home': self.list_page}
            key = self._tab if self._task_id is not None else 'home'
            page = pages[key]
            value = scrolls.get(key, 0)
            page.findChild(QScrollArea).verticalScrollBar().setValue(value if isinstance(value, int) else 0)
            self._rendering = False
        # 화면이 타이머를 소유한다. 파괴된 Page에는 복원 콜백이 도착하지 않는다.
        self._restore_callback = restore
        self._restore_phase = 0
        self._restore_timer.start(0)

    def _apply_restore(self):
        if self._restore_callback is None:
            return
        if self._restore_phase == 0:
            self._restore_phase = 1
            self._restore_timer.start(0)
            return
        restore, self._restore_callback = self._restore_callback, None
        restore()

    def _set_filter(self, key, value):
        self._state()[key] = value
        self._persist()

    def _open_schedule(self, task_id, month, year, case_id):
        self._capture_workspace()
        state = self._ui_state['tasks'].setdefault(str(task_id), {})
        state.update(year=year, month=month, case=case_id)
        self.open_task(task_id, 'work')

    def _home_workspace(self, tasks, cycles):
        tasks = sorted(tasks, key=lambda t: t['id'])
        state = self._ui_state['home']
        controls = QHBoxLayout()
        self.home_search = QLineEdit(str(state.get('search') or ''))
        self.home_search.setPlaceholderText('업무명·설명 검색')
        self.home_search.setAccessibleName('업무 홈 검색')
        controls.addWidget(self.home_search, 1)
        self.home_year = QComboBox()
        self.home_year.setAccessibleName('홈 일정 기록 연도')
        self.home_year.addItem('전체 기록', None)
        found = work_schedule.marks(self.db)
        for year in sorted({m.year for m in found if m.year} | {date.today().year}, reverse=True):
            self.home_year.addItem(f'{year}년', year)
        self.home_year.setCurrentIndex(max(0, self.home_year.findData(state.get('year'))))
        controls.addWidget(self.home_year)
        self.column.addLayout(controls)
        self._dashboard_layout = QVBoxLayout()
        self.column.addLayout(self._dashboard_layout)
        def dashboard():
            clear_layout(self._dashboard_layout)
            query = self.home_search.text().strip().casefold()
            matching = [t for t in tasks if query in (t['name'] + ' ' + (t['description'] or '')).casefold()]
            self.dashboard = WorkYearDashboard(cycles, self.home_year.currentData(), tasks=matching, marks=found)
            self.dashboard.open_task.connect(self.open_task)
            self.dashboard.open_month.connect(self._open_schedule)
            self._dashboard_layout.addWidget(self.dashboard)
            for task, card in self._home_cards:
                card.setVisible(task['id'] in {t['id'] for t in matching})
            self._ui_state['home'].update(search=self.home_search.text(), year=self.home_year.currentData())
            self._persist()
        self.home_search.textChanged.connect(dashboard)
        self.home_year.currentIndexChanged.connect(dashboard)
        dashboard()
        self._update_home_filter = dashboard

    def _workspace_evidence(self, found):
        return [Evidence(label=e.get('filename') or '원본 없음',
                         locator=workcard.evidence_line(self.db, e),
                         snippet=e.get('quote') or '', path=e.get('path') or '',
                         note='' if e.get('current') else '이 근거를 읽은 뒤 원문이 바뀌었거나 사라졌습니다') for e in found]

    def _show_schedule(self):
        if self._task_id is not None:
            self._switch('work')
            return
        self.back()
        self._state().setdefault('scroll', {})['home'] = 0
        self._persist()
        self.list_page.findChild(QScrollArea).verticalScrollBar().setValue(0)

    def _reset_guide(self):
        self._switch('guide')
        self._state().setdefault('scroll', {})['guide'] = 0
        self._persist()
        self.pages['guide'].findChild(QScrollArea).verticalScrollBar().setValue(0)

    def _toggle_sidebar(self):
        sizes = self.splitter.sizes()
        if sizes[0] == 0:
            self.splitter.setSizes([240, max(320, sum(sizes) - 240)])
        else:
            self.splitter.setSizes([0, sum(sizes)])
        self._save_splitter()

    def stop(self):
        self._capture_workspace()
        self._shutting_down = True
        self._render_generation += 1
        self._restore_timer.stop()
        self._refresh_timer.stop()
        self._restore_callback = None
        for dialog in self._workboxes.values():
            dialog._disposed = True
            dialog._job.stop()
        return not any(d._job.running for d in self._workboxes.values())

    def _render_manual(self, column, manual, compact=False):
        column.addWidget(muted_label('현재 자료 기준 · 업무 안내와 매뉴얼은 같은 내용을 사용합니다.'))
        sections = manual.sections
        if compact:
            priority = ['overview', 'output', 'first', 'timing', 'prepare', 'evaluation', 'procedure', 'cases', 'notes', 'past', 'basis']
            sections = sorted(sections, key=lambda s: priority.index(s.key))
        for section in sections:
            card = Card()
            card.body.addWidget(section_title(section.title))
            # 안내는 요약 범위만 줄이며 내용·상태·근거를 다시 생성하지 않는다.
            lines = section.lines[:2] if compact else section.lines
            for line in lines:
                label = QLabel(line)
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setWordWrap(True)
                label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                card.body.addWidget(label)
            if section.evidence:
                card.body.addWidget(self._evidence_button(
                    f'근거 {len(section.evidence)}건', '원문과 위치·확인 상태를 봅니다',
                    lambda s=section: self._show_evidence(self._workspace_evidence(s.evidence), s.title + '의 근거')))
            if not compact and section.more:
                card.body.addWidget(self._fold(section))
            if not compact and section.key == 'cases' and (
                    matters.for_task(self.db, manual.task_id, include_excluded=True)
                    or matters.waiting_for_locality(self.db, manual.task_id)):
                button = QPushButton('건 확인·고치기')
                button.setToolTip('건마다 제출·수용·시행을 확인해 적고, 건이 아닌 행을 뺍니다. 다시 분석해도 남습니다.')
                button.clicked.connect(lambda _=False, tid=manual.task_id: self._open_matters(tid))
                card.body.addWidget(button)
            if compact and len(section.lines) > 2:
                button = QPushButton('매뉴얼에서 모두 보기')
                button.clicked.connect(lambda: self._switch('manual'))
                card.body.addWidget(button)
            column.addWidget(card)
        if not compact:
            column.addWidget(self._version_card(manual.task_id))
            if review.enabled(self.db):
                column.addWidget(self._link_card(manual.task_id))
        shortcuts = QHBoxLayout()
        for text, key in (('먼저 읽을 문서', 'read'), ('일정·진행', 'work'),
                          ('업무 자료', 'docs'), ('매뉴얼', 'manual')):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, k=key: self._switch(k))
            shortcuts.addWidget(button)
        column.addLayout(shortcuts)

    def _open_matters(self, task_id):
        from .matter_dialog import MatterDialog

        dialog = MatterDialog(self, self.db, task_id, lambda found, title: self._show_evidence(
            self._workspace_evidence(found), title))
        dialog.exec()
        if dialog.changed:
            self.refresh(force=True)

    def _fold(self, section):
        """접어 둔 나머지 줄. 누르면 그 자리에서 편다."""
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        button = QPushButton(f'나머지 {len(section.more):,}건 펼치기')
        button.setObjectName('Link')

        def unfold():
            button.setVisible(False)
            for line in section.more:
                label = QLabel(line)
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setWordWrap(True)
                label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                layout.addWidget(label)
        button.clicked.connect(unfold)
        layout.addWidget(button)
        return holder

    def _version_card(self, task_id):
        """노트북에 넣은 매뉴얼 판본과 현재 내용의 차이. 열람만으로 새 판을 만들지 않는다."""
        card = Card()
        card.body.addWidget(section_title('판본·노트북'))
        state = manual_versions.current_state(self.db, task_id)
        if state['version']:
            same = '현재 내용과 같습니다.' if state['current'] else '넣은 뒤 업무 내용이 바뀌었습니다. 새 판을 넣을 수 있습니다.'
            card.body.addWidget(muted_label(f"노트북에 넣은 판: 판본 {state['version']} · {state['created_at']} · {same}"))
        else:
            card.body.addWidget(muted_label('아직 노트북에 넣은 판이 없습니다. 노트북에서 묻기를 누르면 현재 내용으로 첫 판을 만듭니다.'))
        if state['failure']:
            card.body.addWidget(muted_label(f"최근 시도가 실패했습니다: {state['failure']}"))
        rows = manual_versions.history(self.db, task_id, 5)
        for row in rows:
            mark = '실패' if row['status'] == 'failed' else f"판본 {row['version']}"
            where = row['notebook_title'] or '삭제된 노트북'
            card.body.addWidget(muted_label(f"{row['created_at']} · {mark} · {where}", small=True))
        card.body.addWidget(muted_label('옛 판은 보관소에 남아 지난 답의 인용을 그대로 열 수 있습니다.', small=True))
        button = QPushButton('새 판을 노트북에 넣기' if state['version'] and not state['current'] else '노트북에서 묻기')
        button.clicked.connect(lambda _=False, tid=task_id: self.open_notebook.emit(tid))
        card.body.addWidget(button)
        return card

    def _link_card(self, task_id):
        """연결 근거 — 필요한 서식·제출처·기한을 원문과 함께 보고 맞음/아님을 남긴다.

        판정은 이 앱의 기록이다. 수행 완료·공식 기한·외부 제출의 승인이 아니다.
        """
        card = Card()
        card.body.addWidget(section_title('연결 근거'))
        card.body.addWidget(muted_label(
            '자료에서 읽어 연결한 내용입니다. 맞음·아님은 이 앱에 남기는 확인이며 실행 승인이 아닙니다.', small=True))
        items = review.items_for_task(self.db, task_id)
        if not items:
            card.body.addWidget(muted_label('아직 연결된 근거가 없습니다. 분석이 끝나면 여기에 나타납니다.'))
        for item in items:
            card.body.addWidget(self._link_row(item))
        pairs = review.alias_candidates(self.db)
        if pairs:
            card.body.addWidget(section_title('같은 이름의 다른 개체'))
            card.body.addWidget(muted_label('이름이 같아도 같은 기관·서식이라는 증거는 아닙니다. 합치기 전에 근거를 비교하세요.', small=True))
            for pair in pairs:
                card.body.addWidget(self._pair_row(pair))
        return card

    def _evidence_for(self, proofs):
        found = []
        for proof in proofs:
            document = self.db.document(proof.doc_id) if proof.doc_id else None
            found.append(Evidence(label=proof.filename, locator=proof.locator, snippet=proof.quote,
                                  path=document['path'] if document else '',
                                  note='' if proof.current else '이 근거를 읽은 뒤 원문이 바뀌었거나 사라졌습니다'))
        return found

    def _link_row(self, item):
        row = Card(tone='static')
        year = f' · {item.year}년' if item.year else ''
        state = {'confirm': '맞다고 확인함', 'reject': '아니라고 거절함'}.get(item.decision, '확인 전')
        row.body.addWidget(QLabel(f'{item.title} · {item.value}{year}'))
        row.body.addWidget(muted_label(
            f'{state} · {ws.basis_label(item.basis_state)} · {ws.applicability_label(item.applicability)}', small=True))
        for link in item.links:
            kind = '이 값과 엇갈리는' if link['kind'] == 'conflicts' else '이 값과 이어지는 이전/이후'
            row.body.addWidget(muted_label(
                f"{kind} 기록: {link['value']}{f' · {link['year']}년' if link['year'] else ''} — 대체 여부는 확정하지 않았습니다.", small=True))
        buttons = QHBoxLayout()
        if item.proofs:
            buttons.addWidget(self._evidence_button(
                '원문 열기', '이 연결의 원문과 위치', lambda i=item: self._show_evidence(self._evidence_for(i.proofs), f'{i.title}의 근거')))
        else:
            buttons.addWidget(muted_label('원문 인용 없음', small=True))
        for text, action in (('맞음', review.confirm), ('아님', review.reject)):
            button = QPushButton(text)
            button.setEnabled(item.decision is None or item.lifecycle != 'retracted' or text == '맞음')
            button.clicked.connect(lambda _=False, a=action, i=item: self._decide_link(a, i.claim_uuid))
            buttons.addWidget(button)
        if item.decision:
            undo = QPushButton('되돌리기')
            undo.clicked.connect(lambda _=False, i=item: self._decide_link(review.undo_claim, i.claim_uuid))
            buttons.addWidget(undo)
        buttons.addStretch(1)
        row.body.addLayout(buttons)
        return row

    def _pair_row(self, pair):
        row = Card(tone='static')
        row.body.addWidget(QLabel(f"{pair['type']} · {pair['first']['label']}"))
        for side in (pair['first'], pair['second']):
            row.body.addWidget(muted_label(
                f"업무: {', '.join(side['tasks']) or '없음'} · 근거 {len(side['proofs'])}건", small=True))
        buttons = QHBoxLayout()
        for text, action in (('같은 것', review.merge), ('다른 것', review.separate)):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, a=action, p=pair: self._decide_pair(a, p))
            buttons.addWidget(button)
        for index, side in enumerate((pair['first'], pair['second']), 1):
            buttons.addWidget(self._evidence_button(
                f'{index}번 근거', '이 개체의 원문 근거',
                lambda s=side: self._show_evidence(self._evidence_for(s['proofs']), s['label'] + '의 근거')))
        buttons.addStretch(1)
        row.body.addLayout(buttons)
        return row

    def _decide_link(self, action, claim_uuid):
        action(self.db, claim_uuid)
        self.refresh(force=True)

    def _decide_pair(self, action, pair):
        action(self.db, pair['first']['uuid'], pair['second']['uuid'])
        self.refresh(force=True)

    def _render_schedule_controls(self, column, task_id):
        state = self._state()
        found = work_schedule.select(work_schedule.marks(self.db), task_id)
        cases = self.db.cases(task_id=task_id)
        years = {m.year for m in found if m.year} | {c['period_year'] for c in cases} | set(self.db.task_years(task_id)) | {date.today().year}
        controls = QHBoxLayout()
        year = QComboBox()
        year.setAccessibleName('업무 일정 기록 연도')
        year.addItem('전체 기록', None)
        for value in sorted(years, reverse=True):
            year.addItem(f'{value}년', value)
        year.setCurrentIndex(max(0, year.findData(state.get('year'))))
        month = QComboBox()
        month.setAccessibleName('업무 일정 월')
        month.addItem('전체 월', None)
        for value in range(1, 13):
            month.addItem(f'{value}월', value)
        month.setCurrentIndex(max(0, month.findData(state.get('month'))))
        case = QComboBox()
        case.setAccessibleName('업무 수행 건')
        case.addItem('수행 건 선택', None)
        controls.addWidget(year)
        controls.addWidget(month)
        controls.addWidget(case, 1)
        column.addLayout(controls)
        details = QVBoxLayout()
        column.addLayout(details)
        def update(*_, refill=True):
            clear_layout(details)
            selected_id = case.currentData() if case.count() > 1 else state.get('case')
            eligible = [c for c in cases if year.currentData() is None or c['period_year'] == year.currentData()
                        or c['due_state'] in ('official', 'user') and (c['due_date'] or '').startswith(f'{year.currentData()}-')]
            if refill:
                case.blockSignals(True)
                case.clear()
                case.addItem('수행 건 선택', None)
                for row in eligible:
                    case.addItem(f"{row['period_year']}년 · {row['label'] or '기본 회차'} · {row['state']}", row['id'])
                case.setCurrentIndex(max(0, case.findData(selected_id)))
                case.blockSignals(False)
            chosen = next((c for c in eligible if c['id'] == case.currentData()), None)
            state.update(year=year.currentData(), month=month.currentData(), case=chosen['id'] if chosen else None)
            self._persist()
            for mark in work_schedule.select(found, year=year.currentData(), month=month.currentData()):
                details.addWidget(muted_label(('✓ ' if mark.kind == 'confirmed' else '◇ ') + f'{mark.month}월 {mark.text}'))
                if mark.evidence:
                    details.addWidget(self._evidence_button('일정 근거', '이 날짜의 원문', lambda m=mark: self._show_evidence(self._workspace_evidence(m.evidence), '일정 근거')))
            if not work_schedule.select(found, year=year.currentData(), month=month.currentData()):
                details.addWidget(muted_label('선택한 범위에 확인된 날짜·문서 기록이 없습니다. 과거 날짜를 올해로 옮기지 않습니다.'))
            if chosen:
                runs = self.db.runs(task_id=task_id, case_id=chosen['id'])
                details.addWidget(muted_label(f"선택한 수행 건: {chosen['label'] or '기본 회차'} · 기한 {chosen['due_date'] or '미확인'} · {chosen['due_state']}"))
                for run in runs:
                    details.addWidget(muted_label(f"준비 작업 #{run['id']} · {run['state']}"))
            else:
                details.addWidget(muted_label('수행 건을 선택해 진행 상태를 확인하세요. 열람만으로 새 수행 건을 만들지 않습니다.'))
        year.currentIndexChanged.connect(update)
        month.currentIndexChanged.connect(update)
        case.currentIndexChanged.connect(lambda *_: update(refill=False))
        update()
        links = QHBoxLayout()
        for text, key in (('언제 하는 일인가', 'when'), ('어떻게 처리했나', 'how')):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, k=key: self._switch(k))
            links.addWidget(button)
        column.addLayout(links)

    def _render_document_browser(self, column, task_id, rows):
        state = self._state()
        controls = QHBoxLayout()
        search = QLineEdit(str(state.get('doc_search') or ''))
        search.setPlaceholderText('이 업무의 자료 검색 · 파일명·경로')
        search.setAccessibleName('업무 자료 검색')
        year = QComboBox()
        year.setAccessibleName('업무 자료 연도')
        year.addItem('전체 연도', None)
        for value in sorted({r['eff_year'] for r in rows if r['eff_year']}, reverse=True):
            year.addItem(f'{value}년', value)
        year.addItem('연도 미상', 'unknown')
        year.setCurrentIndex(max(0, year.findData(state.get('doc_year'))))
        extension = QComboBox()
        extension.setAccessibleName('업무 자료 형식')
        extension.addItem('전체 형식', None)
        for value in sorted({r['ext'] for r in rows if r['ext']}):
            extension.addItem(value, value)
        extension.setCurrentIndex(max(0, extension.findData(state.get('doc_ext'))))
        controls.addWidget(search, 1)
        controls.addWidget(year)
        controls.addWidget(extension)
        column.addLayout(controls)
        count = QLabel()
        column.addWidget(count)
        results = QVBoxLayout()
        column.addLayout(results)
        paging = QHBoxLayout()
        previous, following = QPushButton('이전 자료'), QPushButton('다음 자료')
        paging.addWidget(previous)
        paging.addWidget(following)
        column.addLayout(paging)
        value = state.get('doc_offset')
        offset = [max(0, value) if isinstance(value, int) else 0]
        def populate(*_, reset=True):
            if reset:
                offset[0] = 0
            clear_layout(results)
            query = search.text().strip().casefold()
            filtered = [r for r in rows if query in (r['filename'] + ' ' + r['path']).casefold()
                        and (year.currentData() is None or r['eff_year'] == year.currentData()
                             or year.currentData() == 'unknown' and not r['eff_year'])
                        and (extension.currentData() is None or r['ext'] == extension.currentData())]
            offset[0] = min(offset[0], max(0, ((len(filtered) - 1) // 100) * 100))
            count.setText(f"이 업무 자료 {len(filtered):,}건 / 전체 {len(rows):,}건 · {offset[0] + 1 if filtered else 0}–{min(offset[0] + 100, len(filtered))}")
            self._render_by_year(results, task_id, filtered[offset[0]:offset[0]+100])
            previous.setEnabled(offset[0] > 0)
            following.setEnabled(offset[0] + 100 < len(filtered))
            state.update(doc_search=search.text(), doc_year=year.currentData(), doc_ext=extension.currentData(), doc_offset=offset[0])
            self._persist()
        search.textChanged.connect(populate)
        year.currentIndexChanged.connect(populate)
        extension.currentIndexChanged.connect(populate)
        def move(delta):
            offset[0] += delta
            populate(reset=False)
        previous.clicked.connect(lambda: move(-100))
        following.clicked.connect(lambda: move(100))
        populate(reset=False)
