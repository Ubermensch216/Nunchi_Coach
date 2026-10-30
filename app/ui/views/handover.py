"""인수인계: 현재 매뉴얼 작성과 시점별 기록 보관·이동을 한 메뉴에서 구분한다."""
from __future__ import annotations

from datetime import date

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QMessageBox, QPushButton, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget

from ...core import handover, handover_folder, report, status
from ...core.output_policy import UnsafeOutputError, safe_text_writer
from .. import theme
from ..snapshot_dialog import SnapshotPanel
from ..widgets import TabBar, center_column, header_margins, muted_label, view_title


class HandoverView(QWidget):
    changed = Signal()

    def __init__(self, db, project_name='', parent=None):
        super().__init__(parent)
        self.db = db
        self.project_name = project_name or db.get_meta('project_name') or db.path.parent.name
        self._stamps = {}
        self.document_text = ''
        self.setObjectName('Canvas')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QWidget()
        header.setObjectName('ViewHeader')
        frame = QVBoxLayout(header)
        frame.setContentsMargins(*header_margins(theme.SP_XL))
        row, column = center_column(theme.CONTENT_MAX_W)
        column.addWidget(view_title('인수인계'))
        column.addWidget(muted_label('후임자에게 전달할 매뉴얼 작성 · 저장한 업무 기록의 보관·이동'))
        self.tabs = TabBar([('manual', '인수인계 매뉴얼'), ('archive', '기록 보관·이동')])
        self.tabs.switched.connect(self._switch)
        column.addWidget(self.tabs)
        frame.addLayout(row)
        outer.addWidget(header)
        self.stack = QStackedWidget()
        manual = QWidget()
        body = QVBoxLayout(manual)
        body.setContentsMargins(theme.SP_XL, theme.SP_LG, theme.SP_XL, theme.SP_LG)
        body.setSpacing(theme.SP_MD)
        self.summary = muted_label('')
        actions = QHBoxLayout()
        actions.addWidget(self.summary, 1)
        self.folder_button = QPushButton('사본 인계 폴더 만들기')
        self.folder_button.setObjectName('Quiet')
        self.folder_button.setToolTip('원본은 그대로 두고 별도 폴더에 <업무>/<연도>/공문·PC자료 사본과 안내 문서를 정리해 내보냅니다.')
        self.folder_button.clicked.connect(self._export_folder)
        actions.addWidget(self.folder_button)
        self.export_button = QPushButton('매뉴얼 파일로 저장')
        self.export_button.setObjectName('Primary')
        self.export_button.setToolTip('아래 미리보기와 같은 내용을 Markdown(.md) 파일로 저장합니다. 원문 파일은 포함하지 않습니다.')
        self.export_button.clicked.connect(self._export_manual)
        actions.addWidget(self.export_button)
        body.addLayout(actions)
        body.addWidget(muted_label('전체 업무의 개요·일정·절차·산출물·제출처·근거 포함 / 보완은 업무별 매뉴얼에서 진행', small=True))
        self.preview = QTextBrowser()
        self.preview.setObjectName('HandoverPreview')
        self.preview.setAccessibleName('인수인계 매뉴얼 미리보기')
        self.preview.setOpenLinks(False)
        self.preview.setOpenExternalLinks(False)
        body.addWidget(self.preview, 1)
        self.stack.addWidget(manual)
        self.archive = SnapshotPanel(db)
        self.archive.changed.connect(self.changed.emit)
        self.stack.addWidget(self.archive)
        outer.addWidget(self.stack, 1)
        self.tabs.select('manual')

    def _switch(self, key):
        self.stack.setCurrentIndex(0 if key == 'manual' else 1)
        self.tabs.select(key)
        self.refresh()

    def refresh(self, force=False):
        stamp = (self.db.con.total_changes, self.db.con.execute('PRAGMA data_version').fetchone()[0], date.today())
        tab = self.stack.currentIndex()
        if not force and stamp == self._stamps.get(tab):
            return
        self._stamps[tab] = stamp
        if tab == 1:
            self.archive.reload(select=self.archive.selected_id())
            return
        self.document_text = report.build_manual(self.db, self.project_name)
        position = self.preview.verticalScrollBar().value()
        self.preview.setMarkdown(self.document_text)
        self.preview.verticalScrollBar().setValue(position)
        tasks = self.db.tasks()
        progress = handover.summarize(self.db.handover_counts())
        self.summary.setText(f'현재 자료 기준 · 전체 업무 {len(tasks)}개 · {progress.headline()}')
        self.export_button.setEnabled(bool(tasks))
        self.folder_button.setEnabled(bool(tasks))

    def _export_manual(self):
        self.refresh(force=True)
        progress = handover.summarize(self.db.handover_counts())
        if not progress.done:
            pending = '\n'.join('· ' + a.sentence() for a in progress.measured if a.state != status.CONFIRMED)
            answer = QMessageBox.question(self, '미확인 내용이 포함되어 있습니다',
                '매뉴얼에 자료 추정·확인 필요 상태를 함께 표시합니다.\n\n' + pending + '\n\n현재 내용으로 저장할까요?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Yes)
            if answer != QMessageBox.StandardButton.Yes:
                return
        suggested = str(self.db.path.parent / f'인수인계_매뉴얼_{date.today():%Y%m%d}.md')
        path, _ = QFileDialog.getSaveFileName(self, '인수인계 매뉴얼 저장', suggested, 'Markdown (*.md)')
        if not path:
            return
        try:
            self.write_manual(path)
        except (UnsafeOutputError, OSError) as exc:
            QMessageBox.warning(self, '매뉴얼을 저장하지 못했습니다', str(exc))
            return
        QMessageBox.information(self, '매뉴얼을 저장했습니다', path)

    def write_manual(self, path):
        """사용자가 확인한 미리보기와 같은 글을 저장한다. 원본 덮어쓰기는 금지한다."""
        if not self.document_text:
            self.refresh(force=True)
        with safe_text_writer(path, self.db) as handle:
            handle.write(self.document_text)
        self.db.audit('handover.manual.export', path)
        return len(self.document_text)

    def _export_folder(self):
        self.refresh(force=True)
        suggested = str(self.db.path.parent / f'{self.project_name}_인계폴더_{date.today():%Y%m%d}')
        directory = QFileDialog.getExistingDirectory(self, '사본 인계 폴더를 만들 위치 선택', suggested)
        if not directory:
            return
        try:
            res = self.write_handover_folder(directory)
        except (UnsafeOutputError, OSError) as exc:
            QMessageBox.warning(self, '사본 인계 폴더를 만들지 못했습니다', str(exc))
            return
        QMessageBox.information(
            self,
            '사본 인계 폴더를 만들었습니다',
            f'{res.root}\n\n업무 {res.tasks_exported}개 · 사본 {res.files_copied:,}건'
            f'(공문 {res.official_copied:,}건 · PC자료 {res.local_copied:,}건)을 복사했습니다.\n'
            '원본 파일은 건드리지 않았습니다.',
        )

    def write_handover_folder(self, directory):
        """선택한 출력 폴더에 업무·연도별 공문/PC자료 사본 인계 폴더를 만든다."""
        return handover_folder.export_handover_folder(
            self.db, directory, project_name=self.project_name, today=date.today()
        )
