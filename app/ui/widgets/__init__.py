"""재사용 위젯.

신뢰 표현 3종(EvidenceChip / ConfidenceBadge / UnknownBlock)이 이 제품의
공통 언어다. 근거 칩 없는 AI 주장은 화면에 올리지 않는다.
"""

from .common import (
    MORE,
    Badge,
    Card,
    ElidedLabel,
    EmptyState,
    EvidenceChip,
    IconChoice,
    InfoDot,
    ListRow,
    Page,
    SectionHeader,
    SubPanel,
    TabBar,
    UnknownBlock,
    WrapLabel,
    body_label,
    center_column,
    clear_layout,
    divider,
    doc_icon,
    header_margins,
    hint_row,
    muted_label,
    note_label,
    open_original,
    section_title,
    view_title,
)
from .evidence_drawer import Evidence, EvidenceDrawer
from .flow_grid import FlowGrid
from .task_card import MonthStrip, TaskCard
from .timeline_grid import TimelineGrid, legend_text
from .work_folder_card import WorkFolderCard
from .work_year_dashboard import WorkYearDashboard
from .work_sidebar import WorkSidebar

__all__ = [
    "MORE",
    "Badge",
    "Card",
    "ElidedLabel",
    "EmptyState",
    "Evidence",
    "EvidenceDrawer",
    "EvidenceChip",
    "FlowGrid",
    "IconChoice",
    "InfoDot",
    "ListRow",
    "MonthStrip",
    "Page",
    "SectionHeader",
    "SubPanel",
    "TabBar",
    "TaskCard",
    "TimelineGrid",
    "WorkFolderCard",
    "WorkYearDashboard",
    "WorkSidebar",
    "UnknownBlock",
    "WrapLabel",
    "body_label",
    "center_column",
    "clear_layout",
    "divider",
    "doc_icon",
    "header_margins",
    "hint_row",
    "legend_text",
    "muted_label",
    "note_label",
    "open_original",
    "section_title",
    "view_title",
]
