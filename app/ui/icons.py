"""단색 선 아이콘.

이모지를 쓰지 않는 이유
    🗂📅📄💬는 OS가 제 색으로 칠해 버린다. 넷이 각각 다른 색으로 튀면
    메뉴가 아니라 스티커가 되고, 고른 메뉴를 강조색으로 물들여도 아이콘만
    혼자 원래 색으로 남아 "지금 여기"가 흐려진다. 화면 전체를 무채색 +
    강조색 하나로 묶기로 한 원칙(theme.py)과도 어긋난다.

    그래서 24×24 격자 위에 굵기 1.6의 선으로 직접 그린다. 색은 그릴 때
    주입하므로 상태(보통·선택·비활성)에 따라 글자와 같은 색으로 움직인다.

QtSvg가 없는 환경에서는 None을 돌려준다 — 호출부가 글자로 물러선다.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QPainter, QPixmap

try:
    from PySide6.QtSvg import QSvgRenderer
except ImportError:  # pragma: no cover — 패키징 환경 방어
    QSvgRenderer = None  # type: ignore[assignment]

# 선으로만 그린다(fill="none"). 채운 조각이 필요한 곳만 fill="{c}"를 준다.
_SHAPES = {
    # 업무 — 집게 달린 서류판에 체크. "해야 할 일 묶음"의 관용 기호.
    "tasks": (
        '<rect x="4.5" y="5" width="15" height="16" rx="2.5"/>'
        '<rect x="9" y="2.9" width="6" height="4.2" rx="1.3" fill="{c}" stroke="none"/>'
        '<path d="M8.6 13.4 l2.4 2.4 L15.8 11"/>'
    ),
    # 일정 — 달력. 위 고리 두 개와 머리 줄로 "달력"임을 한눈에 알린다.
    "calendar": (
        '<rect x="3.5" y="5" width="17" height="15.5" rx="2.5"/>'
        '<path d="M3.5 9.6 H20.5"/>'
        '<path d="M8 3.2 V6.6"/><path d="M16 3.2 V6.6"/>'
        '<rect x="7" y="12.4" width="2" height="2" rx="0.6" fill="{c}" stroke="none"/>'
        '<rect x="11" y="12.4" width="2" height="2" rx="0.6" fill="{c}" stroke="none"/>'
        '<rect x="15" y="12.4" width="2" height="2" rx="0.6" fill="{c}" stroke="none"/>'
        '<rect x="7" y="16.2" width="2" height="2" rx="0.6" fill="{c}" stroke="none"/>'
        '<rect x="11" y="16.2" width="2" height="2" rx="0.6" fill="{c}" stroke="none"/>'
    ),
    # 문서 — 모서리를 접은 낱장. 접힌 귀가 있어야 '파일'이 아니라 '문서'다.
    "documents": (
        '<path d="M6.5 3.25 H13.25 L18.75 8.75 V20 '
        'a1.5 1.5 0 0 1 -1.5 1.5 H6.5 A1.5 1.5 0 0 1 5 20 V4.75 '
        'A1.5 1.5 0 0 1 6.5 3.25 Z"/>'
        '<path d="M13 3.5 V8.4 a0.6 0.6 0 0 0 0.6 0.6 H18.5"/>'
        '<path d="M8.2 13.4 H15.4"/><path d="M8.2 16.8 H13"/>'
    ),
    # 질문 — 말풍선. 꼬리까지 한 획으로 그려야 이어 붙인 티가 나지 않는다.
    "ask": (
        '<path d="M6.75 4.25 H17.25 A3 3 0 0 1 20.25 7.25 V13.75 '
        'A3 3 0 0 1 17.25 16.75 H12.4 L8.4 20.05 V16.75 H6.75 '
        'A3 3 0 0 1 3.75 13.75 V7.25 A3 3 0 0 1 6.75 4.25 Z"/>'
        '<path d="M7.9 8.9 H16.1"/><path d="M7.9 12.1 H13"/>'
    ),
    # 읽기 전용 — 자물쇠. 약속을 말하는 자리라 글자 옆에 작게 붙는다.
    "lock": (
        '<rect x="5" y="10.4" width="14" height="9.6" rx="2.2"/>'
        '<path d="M8.3 10.4 V7.7 a3.7 3.7 0 0 1 7.4 0 V10.4"/>'
        '<circle cx="12" cy="15.2" r="1.15" fill="{c}" stroke="none"/>'
    ),
    # 자료 폴더 — 탭 달린 폴더. 설정의 '자료 폴더' 갈래 표지.
    "folder": (
        '<path d="M3.5 6.75 a1.75 1.75 0 0 1 1.75 -1.75 H9.4 l2 2.4 H18.75 '
        'A1.75 1.75 0 0 1 20.5 9.15 V17.75 A1.75 1.75 0 0 1 18.75 19.5 '
        'H5.25 A1.75 1.75 0 0 1 3.5 17.75 Z"/>'
    ),
    # 시스템 — 톱니. AI 현황·저장 위치·최근 활동이 모이는 갈래 표지.
    "system": (
        '<circle cx="12" cy="12" r="3.1"/>'
        '<path d="M12 2.9 V5.3"/><path d="M12 18.7 V21.1"/>'
        '<path d="M2.9 12 H5.3"/><path d="M18.7 12 H21.1"/>'
        '<path d="M5.6 5.6 L7.3 7.3"/><path d="M16.7 16.7 L18.4 18.4"/>'
        '<path d="M18.4 5.6 L16.7 7.3"/><path d="M7.3 16.7 L5.6 18.4"/>'
    ),

    # ── 테마 3종 ──
    # 셋이 한자리에 놓이므로 실루엣이 서로 겹치면 안 된다. 해(둥근 방사),
    # 달(초승달), 모니터(네모+받침) — 축소해도 윤곽만으로 구별된다.
    "theme-light": (
        '<circle cx="12" cy="12" r="4.2"/>'
        '<path d="M12 2.6 V5"/><path d="M12 19 V21.4"/>'
        '<path d="M2.6 12 H5"/><path d="M19 12 H21.4"/>'
        '<path d="M5.4 5.4 L7.1 7.1"/><path d="M16.9 16.9 L18.6 18.6"/>'
        '<path d="M18.6 5.4 L16.9 7.1"/><path d="M7.1 16.9 L5.4 18.6"/>'
    ),
    "theme-dark": (
        '<path d="M20.2 14.3 A8.6 8.6 0 0 1 9.7 3.8 '
        'A8.6 8.6 0 1 0 20.2 14.3 Z"/>'
    ),
    "theme-system": (
        '<rect x="3" y="4.6" width="18" height="12.2" rx="2"/>'
        '<path d="M9 20.2 H15"/><path d="M12 16.8 V20.2"/>'
        # 화면 절반만 칠한다 — "밝기를 OS에 맡긴다"를 그림 하나로 말한다.
        '<path d="M12 6.6 H19 V14.8 H12 Z" fill="{c}" stroke="none"/>'
    ),

    # ── 글자 크기 3종 ──
    # 같은 'A'를 크기만 달리 그린다. 그림이 곧 결과라서 아래 글자를 읽지
    # 않아도 무엇이 커지는지 안다. viewBox 안에서 아래쪽 기준선을 맞춰
    # 셋이 한 줄에 앉은 것처럼 보이게 한다.
    "text-small": (
        '<path d="M8.4 16.4 L12 8.9 L15.6 16.4"/>'
        '<path d="M9.9 13.8 H14.1"/>'
    ),
    "text-medium": (
        '<path d="M7.2 17.6 L12 7.2 L16.8 17.6"/>'
        '<path d="M9.2 14 H14.8"/>'
    ),
    "text-large": (
        '<path d="M5.6 19.2 L12 4.8 L18.4 19.2"/>'
        '<path d="M8.3 13.4 H15.7"/>'
    ),

    # ── 글자 옆에 붙는 작은 표지 ──
    # 🔁 ⚙ ⚠ ✕ 를 대신한다. 이모지는 OS가 제 색으로 칠해 화면의 색 규칙을 깨고,
    # 글꼴에 없으면 다른 글꼴의 그림이 끼어들어 줄 높이까지 흔든다.
    "repeat": (
        '<path d="M4.5 11.2 V9.6 A3.6 3.6 0 0 1 8.1 6 H18.2"/>'
        '<path d="M15.6 3.4 L18.2 6 L15.6 8.6"/>'
        '<path d="M19.5 12.8 V14.4 A3.6 3.6 0 0 1 15.9 18 H5.8"/>'
        '<path d="M8.4 20.6 L5.8 18 L8.4 15.4"/>'
    ),
    # 설정 — 손잡이 달린 조절 막대 셋. 톱니는 16px에서 뭉개진다.
    "settings": (
        '<path d="M4 7 H20"/><path d="M4 12 H20"/><path d="M4 17 H20"/>'
        '<circle cx="9" cy="7" r="2.2" fill="{c}" stroke="none"/>'
        '<circle cx="15.5" cy="12" r="2.2" fill="{c}" stroke="none"/>'
        '<circle cx="7.5" cy="17" r="2.2" fill="{c}" stroke="none"/>'
    ),
    "warning": (
        '<path d="M12 4.4 L20.4 19 H3.6 Z"/>'
        '<path d="M12 10.2 V14"/>'
        '<circle cx="12" cy="16.5" r="0.95" fill="{c}" stroke="none"/>'
    ),
    "close": '<path d="M6.5 6.5 L17.5 17.5"/><path d="M17.5 6.5 L6.5 17.5"/>',
    "chevron-down": '<path d="M6.5 9.5 L12 15 L17.5 9.5"/>',
    "check": '<path d="M5.5 12.6 L10.2 17 L18.5 7.6" stroke-width="2.4"/>',

    # ── 노트북 ──
    # 대화의 답 아래에 글자 없이 서는 단추들. 글자를 뗐으므로 윤곽만으로 읽혀야 한다.
    # 복사 — 겹친 두 장.
    "copy": (
        '<rect x="8.5" y="8.5" width="11.5" height="12" rx="2.2"/>'
        '<path d="M15.5 8.5 V5.7 A2.2 2.2 0 0 0 13.3 3.5 H6.2 A2.2 2.2 0 0 0 4 5.7 '
        'V13.8 A2.2 2.2 0 0 0 6.2 16 H8.5"/>'
    ),
    # 메모로 저장 — 책갈피에 더하기. '여기에 남겨 둔다'의 관용 기호.
    "bookmark-add": (
        '<path d="M7.2 3.5 H16.8 A1.7 1.7 0 0 1 18.5 5.2 V20.5 L12 16.3 L5.5 20.5 '
        'V5.2 A1.7 1.7 0 0 1 7.2 3.5 Z"/>'
        '<path d="M12 7.3 V12.3"/><path d="M9.5 9.8 H14.5"/>'
    ),
    # 이름 고치기 — 연필.
    "edit": (
        '<path d="M4.5 19.5 L5.4 15.6 L15.9 5.1 a1.9 1.9 0 0 1 2.7 0 l0.3 0.3 '
        'a1.9 1.9 0 0 1 0 2.7 L8.4 18.6 Z"/>'
        '<path d="M14.2 6.8 L17.2 9.8"/>'
    ),
    # 맞습니다 / 사실과 다릅니다 — 엄지. 아래는 위를 뒤집은 것이다.
    "thumb-up": (
        '<path d="M3.5 10.8 H7.2 V20 H3.5 Z"/>'
        '<path d="M7.2 10.8 L11.2 3.9 a2.05 2.05 0 0 1 2.55 2.45 L12.9 9.6 H18.3 '
        'a2 2 0 0 1 1.95 2.45 l-1.35 6.3 A2.1 2.1 0 0 1 16.85 20 H7.2"/>'
    ),
    "thumb-down": (
        '<g transform="translate(0 24) scale(1 -1)">'
        '<path d="M3.5 10.8 H7.2 V20 H3.5 Z"/>'
        '<path d="M7.2 10.8 L11.2 3.9 a2.05 2.05 0 0 1 2.55 2.45 L12.9 9.6 H18.3 '
        'a2 2 0 0 1 1.95 2.45 l-1.35 6.3 A2.1 2.1 0 0 1 16.85 20 H7.2"/>'
        '</g>'
    ),
    # 좌우 판 접기·펴기 — 창 안의 옆 칸과, 그 칸이 움직일 쪽을 가리키는 꺾쇠.
    "panel-left-close": (
        '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M9.5 4.5 V19.5"/>'
        '<path d="M16.2 9.4 L13.6 12 L16.2 14.6"/>'
    ),
    "panel-left-open": (
        '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M9.5 4.5 V19.5"/>'
        '<path d="M13.6 9.4 L16.2 12 L13.6 14.6"/>'
    ),
    "panel-right-close": (
        '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M14.5 4.5 V19.5"/>'
        '<path d="M7.8 9.4 L10.4 12 L7.8 14.6"/>'
    ),
    "panel-right-open": (
        '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M14.5 4.5 V19.5"/>'
        '<path d="M10.4 9.4 L7.8 12 L10.4 14.6"/>'
    ),
}

# 제품 표지. 검은 띠 위에 올라가므로 바탕 없이 선만 그린다 — 흰 바탕째 줄인
# 그림을 올리면 띠 위에 흰 네모가 뜬다. 서류(흰 선) 안에 자료를 잇는 점(청록).
_BRAND_ACCENT = "#35C7AD"
_BRAND = (
    '<path d="M7 2.8 H14 L19 7.8 V19.6 a1.6 1.6 0 0 1 -1.6 1.6 H7 '
    'a1.6 1.6 0 0 1 -1.6 -1.6 V4.4 A1.6 1.6 0 0 1 7 2.8 Z" stroke="{c}"/>'
    '<path d="M13.6 3 V8.2 H18.8" stroke="{c}"/>'
    '<g stroke="{a}" stroke-width="1.3">'
    '<path d="M12.2 14.3 L9.3 11.8"/><path d="M12.2 14.3 L15.4 12.4"/>'
    '<path d="M12.2 14.3 L10.2 17.6"/><path d="M12.2 14.3 L15.2 17.2"/>'
    '</g>'
    '<circle cx="12.2" cy="14.3" r="1.5" fill="{a}" stroke="none"/>'
    '<circle cx="9.1" cy="11.6" r="1" fill="{a}" stroke="none"/>'
    '<circle cx="15.6" cy="12.3" r="1" fill="{a}" stroke="none"/>'
    '<circle cx="10.1" cy="17.8" r="1" fill="{a}" stroke="none"/>'
    '<circle cx="15.4" cy="17.4" r="1" fill="{a}" stroke="none"/>'
)

_cache: dict[tuple[str, str, int], QPixmap] = {}


def pixmap(name: str, color: str, size: int = 20) -> QPixmap | None:
    """단색 아이콘 픽스맵. 없으면 None — 호출부는 글자로 물러선다.

    2배로 그린 뒤 devicePixelRatio를 올린다. 고해상도 화면에서 선 아이콘은
    등배로 그리면 획이 뭉개져 오히려 이모지보다 지저분해 보인다.
    """
    if QSvgRenderer is None or name not in _SHAPES:
        return None
    return _render(name, color, size, _SHAPES[name].format(c=color))


def brand(color: str, size: int = 24) -> QPixmap | None:
    """제품 표지. 서류의 선은 주어진 색, 안의 점은 고정된 청록이다."""
    if QSvgRenderer is None:
        return None
    return _render("brand", color, size, _BRAND.format(c=color, a=_BRAND_ACCENT))


def icon(name: str, color: str, size: int = 16):
    """버튼에 붙일 QIcon. 없으면 빈 아이콘 — 버튼은 글자만으로도 선다."""
    from PySide6.QtGui import QIcon

    art = pixmap(name, color, size)
    return QIcon(art) if art is not None else QIcon()


def label(name: str, color: str, size: int = 16):
    """글자 옆에 세울 작은 표지 한 칸. 그릴 수 없으면 None."""
    from PySide6.QtWidgets import QLabel

    art = pixmap(name, color, size)
    if art is None:
        return None
    mark = QLabel()
    mark.setPixmap(art)
    mark.setFixedSize(size, size)
    return mark


def png_path(name: str, color: str, size: int = 16) -> str:
    """스타일시트의 image: url()에 넣을 파일 경로. 그릴 수 없으면 빈 문자열.

    QSS는 그림을 파일로만 받는다. 색이 테마를 따라가야 하므로 그때그때 그려
    임시 폴더에 두고 그 경로를 돌려준다. 같은 (이름, 색, 크기)는 한 번만 쓴다.
    """
    import tempfile
    from pathlib import Path

    from PySide6.QtGui import QGuiApplication

    # 앱이 서기 전에 QPixmap을 만들면 Qt가 프로세스를 죽인다(theme.font_family와
    # 같은 사정). 그때는 그림 없이 간다.
    if QGuiApplication.instance() is None:
        return ""
    art = pixmap(name, color, size)
    if art is None:
        return ""
    folder = Path(tempfile.gettempdir()) / "nunchicoach-icons"
    path = folder / f"{name}-{color.lstrip('#')}-{size}.png"
    try:
        if not path.exists():
            folder.mkdir(parents=True, exist_ok=True)
            art.save(str(path), "PNG")
    except OSError:
        return ""
    return path.as_posix()


def _render(name: str, color: str, size: int, body: str) -> QPixmap:
    key = (name, color, size)
    if key in _cache:
        return _cache[key]

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'width="{size}" height="{size}" fill="none" stroke="{color}" '
        'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
        f'{body}</svg>'
    )
    ratio = 2
    canvas = QPixmap(size * ratio, size * ratio)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(painter)
    painter.end()
    canvas.setDevicePixelRatio(ratio)
    _cache[key] = canvas
    return canvas
