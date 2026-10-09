"""디자인 토큰과 전역 스타일시트.

대상은 공직 사무 환경이고, 처음 여는 사람이 3초 안에 "여기서 무엇을 보면
되는가"를 알아야 한다. 그래서 화면 전체가 다음 다섯 가지를 지킨다.

  1. 답이 먼저, 근거와 조작은 그 뒤에. 값을 가장 크게, 단추는 조용하게.
  2. 예외만 표시한다. 기본 상태(자료에서 추정)에는 뱃지를 붙이지 않는다 —
     전부 표시하면 정작 주의할 것이 묻힌다.
  3. 같은 사실은 한 자리에서 한 번만 말한다.
  4. 구획은 테두리가 아니라 여백과 글자 위계로 만든다. 카드는 한 겹이다.
  5. 무채색 바탕에 강조색 하나. 색만으로 뜻을 전하지 않는다(PRD §18.4).

색은 두 벌(밝게·어둡게)이다. 모듈 전역 이름(TEXT, PRIMARY …)은 그대로 두고
apply_mode()가 그 이름이 가리키는 값을 바꾼다. 화면 코드는 theme.TEXT라고만
쓰면 되고, 어느 벌을 쓰는지는 신경 쓰지 않는다.

한 가지 규칙만 지키면 된다: **색을 모듈 수준 상수에 미리 담아 두지 않는다.**
import 시점에 값을 복사해 두면 테마를 바꿔도 그 사본은 옛 색으로 남는다.
색이 필요한 표는 값 대신 이름을 담고 theme.color("TEXT")로 꺼낸다.
"""

from __future__ import annotations

from pathlib import Path

# ── 색 두 벌 ────────────────────────────────────────────────────────
# 이름은 역할이지 색이 아니다. 어두운 벌에서 BG가 검어지듯, 같은 이름이
# 같은 역할을 계속 맡는다.
LIGHT = {
    "BG": "#FFFFFF",             # 카드·머리 띠 — 내용이 놓이는 흰 판
    "SURFACE": "#F6F7F9",        # 카드 안에서 한 단계 물린 자리(인용·보조)
    "SURFACE_ALT": "#ECEEF2",    # 조용한 단추·칩의 바탕
    "BORDER": "#E4E7EC",         # 카드 윤곽과 줄 사이 가는 선
    "BORDER_STRONG": "#CDD2DA",  # 입력칸처럼 '여기에 넣는다'가 보여야 하는 테두리

    # 본문 바탕. 흰 카드가 카드로 보이려면 바탕이 한 단계 낮아야 한다.
    "CANVAS": "#F3F4F6",
    "BAND": "#FAFBFC",           # 표의 줄무늬

    "NAV_BG": "#F3F4F6",         # 설정 창 왼쪽 갈래 목록
    "NAV_HOVER": "#E8EBEF",

    "TEXT": "#14171C",
    # 보조 글자. 작은 회색 글자가 화면의 절반을 차지하므로 흐리면 통째로
    # 안 읽힌다 — 본문보다 한 단계만 물린다.
    "TEXT_MUTED": "#4A5361",
    "TEXT_SUBTLE": "#737C89",    # 이름표·부기. 값 옆에서 물러서는 글자
    "TEXT_DISABLED": "#A3AAB5",
    "TEXT_ON_PRIMARY": "#FFFFFF",

    "PRIMARY": "#1F5FBF",        # 강조색은 이것 하나
    "PRIMARY_HOVER": "#184E9F",
    "PRIMARY_SOFT": "#EAF1FB",

    "ATTENTION": "#A8540A",      # 확인 필요
    "ATTENTION_SOFT": "#FCF2E2",
    "DANGER": "#B42318",         # 오류·되돌릴 수 없는 동작
    "DANGER_SOFT": "#FCECEA",
    "CONFIRMED": "#16763B",      # 사람이 확인함
    "CONFIRMED_SOFT": "#E7F4EB",

    # 연간 패턴 격자의 막대. 농도가 "얼마나 단단한 근거인가"를 말하고,
    # 막대 안 기호가 같은 뜻을 한 번 더 말한다.
    "CYCLE_STRONG": "#1F5FBF",   # 담당자가 확인함
    "CYCLE_SOFT": "#A9C4E8",     # 자료에서 추정
    "CYCLE_WEAK": "#DDE3EB",     # 자료가 부족

    # 상단 띠. 흰 띠에 가는 밑줄 하나 — 길(메뉴)은 조용히 있고 내용이 앞에 선다.
    "NAVBAR_BG": "#FFFFFF",
    "NAVBAR_TEXT": "#14171C",
    "NAVBAR_MUTED": "#5A6371",
    "NAVBAR_DISABLED": "#B4BAC4",
    "NAVBAR_HOVER": "#F1F3F6",
    "NAVBAR_ACTIVE": "#EAF1FB",
    "NAVBAR_LINE": "#E4E7EC",
}

# 어두운 벌. 밝은 벌을 그대로 뒤집지 않는다 — 순수한 검정 바탕에 순백 글자는
# 대비가 지나쳐 잔상이 남는다. 바탕은 짙은 회청색, 글자는 살짝 낮춘 흰색으로
# 두고, 강조색은 어두운 바탕 위에서 대비가 서도록 한 단계 밝힌다.
DARK = {
    "BG": "#1D2026",
    "SURFACE": "#252930",
    "SURFACE_ALT": "#2E333B",
    "BORDER": "#30353D",
    "BORDER_STRONG": "#454C57",

    "CANVAS": "#15181C",
    "BAND": "#21252B",

    "NAV_BG": "#181B20",
    "NAV_HOVER": "#262B33",

    "TEXT": "#E7EAEE",
    "TEXT_MUTED": "#B3BBC6",
    "TEXT_SUBTLE": "#8D97A4",
    "TEXT_DISABLED": "#687180",
    "TEXT_ON_PRIMARY": "#0F1216",

    "PRIMARY": "#74ABE8",
    "PRIMARY_HOVER": "#93C0F0",
    "PRIMARY_SOFT": "#1F2E40",

    "ATTENTION": "#E3A85F",
    "ATTENTION_SOFT": "#352A1B",
    "DANGER": "#EC8079",
    "DANGER_SOFT": "#361F1D",
    "CONFIRMED": "#6CC187",
    "CONFIRMED_SOFT": "#1C2F23",

    "CYCLE_STRONG": "#4585C6",
    "CYCLE_SOFT": "#325273",
    "CYCLE_WEAK": "#2E333B",

    "NAVBAR_BG": "#1D2026",
    "NAVBAR_TEXT": "#E7EAEE",
    "NAVBAR_MUTED": "#A4ADB9",
    "NAVBAR_DISABLED": "#5C6572",
    "NAVBAR_HOVER": "#272B33",
    "NAVBAR_ACTIVE": "#1F2E40",
    "NAVBAR_LINE": "#30353D",
}

# 정적 분석기와 예전 호출부를 위해 밝은 벌의 값을 모듈 전역으로 펼쳐 둔다.
# apply_mode()가 여기를 덮어쓴다.
globals().update(LIGHT)

BG: str
SURFACE: str
SURFACE_ALT: str
BORDER: str
BORDER_STRONG: str
CANVAS: str
BAND: str
NAV_BG: str
NAV_HOVER: str
TEXT: str
TEXT_MUTED: str
TEXT_DISABLED: str
TEXT_SUBTLE: str
TEXT_ON_PRIMARY: str
PRIMARY: str
PRIMARY_HOVER: str
PRIMARY_SOFT: str
ATTENTION: str
ATTENTION_SOFT: str
DANGER: str
DANGER_SOFT: str
CONFIRMED: str
CONFIRMED_SOFT: str
CYCLE_STRONG: str
CYCLE_SOFT: str
CYCLE_WEAK: str
NAVBAR_BG: str
NAVBAR_TEXT: str
NAVBAR_MUTED: str
NAVBAR_DISABLED: str
NAVBAR_HOVER: str
NAVBAR_ACTIVE: str
NAVBAR_LINE: str

_mode = "light"          # 지금 칠해져 있는 벌 (light | dark)


# ── 테마 모드 ───────────────────────────────────────────────────────
# 저장값은 셋뿐이다. system은 "지금 OS가 뭘 쓰는가"를 따라간다 — 사용자가
# 저녁에 OS를 어둡게 바꾸면 이 앱도 같이 어두워져야 한다.
THEME_MODES = ("system", "light", "dark")
THEME_LABELS = {"system": "시스템", "light": "밝게", "dark": "어둡게"}
THEME_ICONS = {"system": "theme-system", "light": "theme-light", "dark": "theme-dark"}
THEME_HINTS = {
    "system": "윈도우 설정을 따라갑니다",
    "light": "밝은 배경에 어두운 글자",
    "dark": "어두운 배경에 밝은 글자",
}


def normalize_mode(value: str | None) -> str:
    return value if value in THEME_MODES else "system"


def resolve_mode(mode: str) -> str:
    """system을 실제로 칠할 벌(light|dark)로 바꾼다.

    Qt 6.5부터 styleHints().colorScheme()이 OS 설정을 알려준다. 그보다 낮은
    환경에서는 알 길이 없으므로 밝은 벌로 물러선다 — 어둡게가 필요하면
    사용자가 직접 고르면 된다.
    """
    mode = normalize_mode(mode)
    if mode != "system":
        return mode
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication

        app = QGuiApplication.instance()
        if app is not None:
            scheme = app.styleHints().colorScheme()
            if scheme == Qt.ColorScheme.Dark:
                return "dark"
    except (ImportError, AttributeError):  # pragma: no cover — 옛 Qt 방어
        pass
    return "light"


def apply_mode(mode: str) -> str:
    """색 이름이 가리키는 값을 갈아 끼운다. 실제로 칠한 벌을 돌려준다."""
    global _mode
    resolved = resolve_mode(mode)
    globals().update(DARK if resolved == "dark" else LIGHT)
    _mode = resolved
    return resolved


def current_mode() -> str:
    return _mode


def color(name: str) -> str:
    """지금 칠해져 있는 벌에서 색 하나를 꺼낸다.

    표(어떤 상태 → 어떤 색)를 모듈 수준에 두어야 할 때 값 대신 이 함수로
    미룬다. 값을 미리 담아 두면 테마를 바꿔도 그 표만 옛 색으로 남는다.
    """
    return globals().get(name, globals()["TEXT"])


# ── 타이포 ──────────────────────────────────────────────────────────
# 글꼴 후보. **설치된 것 하나만 골라 쓴다** — 목록을 그대로 스타일시트에 넣으면
# 없는 글꼴(Pretendard)이 QFont의 이름이 되고, 재는 것은 그 이름의 대체 글꼴,
# 그리는 것은 한글 대체 글꼴이 되어 **줄 높이가 어긋난다.** 실제로 안내 문구의
# 둘째 줄이 판 밖으로 잘려 나갔다(문서 화면 오른쪽 패널).
FONT_CANDIDATES = ("Pretendard", "Malgun Gothic", "맑은 고딕", "Segoe UI")
_family: str | None = None

# 함께 싣는 글꼴(SIL OFL 1.1, app/licenses/texts/OFL-1.1-Pretendard.txt).
# 굵기 셋(400·600·700)만 싣는다 — 스타일시트가 쓰는 굵기가 그 셋이다. 맑은
# 고딕에는 600이 없어 '조금 굵게'로 적은 글자가 그냥 굵게 나오거나 흐려졌다.
_FONT_DIR = Path(__file__).parent / "assets" / "fonts"
_fonts_loaded = False


def _load_bundled_fonts() -> None:
    """함께 싣는 글꼴을 이 프로세스에만 등록한다. PC에 설치하지 않는다.

    파일이 없거나 읽지 못해도 조용히 넘어간다 — 그때는 font_family()가 다음
    후보(맑은 고딕)를 고른다. 앱이 선 뒤에만 불러야 한다(font_family 참고).
    """
    global _fonts_loaded
    if _fonts_loaded:
        return
    _fonts_loaded = True
    from PySide6.QtGui import QFontDatabase

    for path in sorted(_FONT_DIR.glob("*.ttf")):
        QFontDatabase.addApplicationFont(str(path))


def font_family() -> str:
    """이 PC에 실제로 설치된 첫 글꼴. 없으면 시스템 기본에 맡긴다.

    **앱이 서기 전에는 묻지 않는다.** QFontDatabase를 QGuiApplication 없이
    건드리면 Qt가 예외가 아니라 프로세스를 죽인다 — try/except로 못 막는다.
    그리고 못 찾은 답은 캐시하지 않는다. 한 번 빈 값을 담아 두면 앱이 선
    뒤에도 계속 빈 값을 돌려주어, 글꼴이 영영 안 맞는다(시험에서 잡혔다).
    """
    global _family
    if _family:
        return _family
    try:
        from PySide6.QtGui import QFontDatabase, QGuiApplication

        if QGuiApplication.instance() is None:
            return ""
        _load_bundled_fonts()
        installed = set(QFontDatabase.families())
    except Exception:                      # pragma: no cover — Qt 없이 부를 때
        return ""
    _family = next((name for name in FONT_CANDIDATES if name in installed), "")
    return _family


FONT_FAMILY = '"Pretendard", "Malgun Gothic", "맑은 고딕", sans-serif'
<<<<<<< Updated upstream
# 다섯 단계. 예전에는 20·16·14·12 넷이었는데 이웃한 단계의 차가 2~4px뿐이라
# 제목·구획·본문이 한 덩어리로 읽혔다. 화면 제목과 구획 제목을 벌리고, 본문과
# 구획 사이에 '답'의 자리(FS_LEAD)를 따로 둔다 — 기한·제출처처럼 화면이 내놓는
# 값이 그것을 둘러싼 표지·부기보다 커야 눈이 값부터 읽는다.
FS_TITLE = 24
FS_SECTION = 18
FS_LEAD = 16
FS_BODY = 14
FS_SMALL = 13                # Pretendard는 맑은 고딕보다 글자가 좁아 12에서는 부기가 잘다
=======
# 위계는 네 단이다. 화면 제목 → 구획 제목 → 본문 → 부기. 단마다 크기와
# 무게를 함께 바꿔야 훑는 눈이 어디서 멈출지 안다.
FS_TITLE = 22
FS_SECTION = 16
FS_BODY = 14
FS_SMALL = 13
>>>>>>> Stashed changes
LINE_HEIGHT = 1.6            # 한글은 1.5 이하면 답답하다

# 글자 크기 3단계. 소·중·대이며 저장값은 이 키다.
# 큰 쪽을 1.2로 둔 근거는 PRD §18.4(저시력 지원)이고, 작은 쪽은 그 대칭이
# 아니라 0.9까지만 내린다 — 그 아래는 한글 자소가 뭉개져 오히려 안 읽힌다.
TEXT_SIZES = {"small": 0.9, "medium": 1.0, "large": 1.2}
TEXT_SIZE_ORDER = ("small", "medium", "large")
TEXT_SIZE_LABELS = {"small": "작게", "medium": "보통", "large": "크게"}
TEXT_SIZE_ICONS = {"small": "text-small", "medium": "text-medium", "large": "text-large"}


# 한글 글자는 자소 셋이 한 칸에 들어간다. 이 아래로 내려가면 획이 서로
# 붙어 뭉개진다 — 알파벳은 10px에서도 읽히지만 한글은 아니다. '작게'를
# 골랐을 때 부기 글자가 11px이 되어 실제로 뭉개졌다.
MIN_FONT_PX = 12


def _qss_family() -> str:
    """스타일시트에 넣을 글꼴 이름. 설치된 것이 없으면 항목 자체를 비운다."""
    name = font_family()
    return f'"{name}"' if name else "sans-serif"


def _scaled(base: int, scale: float) -> int:
    """글자 크기를 비율로 줄이되 읽을 수 있는 하한 아래로는 내리지 않는다.

    '작게'가 모든 글자를 똑같이 줄이지는 못한다는 뜻이다 — 본문·제목은
    줄어들고 부기는 그대로다. 안 읽히는 글자를 만드는 것보다 낫다.
    """
    return max(MIN_FONT_PX, round(base * scale))


def normalize_text_size(value: str | None) -> str:
    return value if value in TEXT_SIZES else "medium"


def body_px(text_size: str) -> int:
    """설정 화면이 '(13px)'처럼 보여줄 실제 본문 크기. 하한을 함께 반영한다."""
    return _scaled(FS_BODY, TEXT_SIZES[normalize_text_size(text_size)])


# ── 간격·크기 (4px 그리드) ──────────────────────────────────────────
SP_XS, SP_SM, SP_MD, SP_LG, SP_XL = 4, 8, 12, 16, 24
SP_2XL = 32                  # 구획과 구획 사이. 테두리 대신 이 여백이 구획을 가른다.
RADIUS = 8                   # 단추·입력칸
RADIUS_SM = 6                # 칩·뱃지
RADIUS_LG = 12               # 카드

# 상단 띠. 메뉴가 여기 산다(세로 사이드바는 걷어 냈다 — shell.TopBar 참고).
TOPBAR_H = 56
ROW_H = 38
# 입력칸·드롭다운·버튼의 공통 높이. 한 줄에 서면 같은 키여야 한 벌로 보인다.
CONTROL_H = 36
DETAIL_PANEL_W = 400
# 근거 서랍(계획서 §28: 360~420px). 인용문이 서너 줄로 접히는 최소 폭이다.
DRAWER_W = 380
<<<<<<< Updated upstream
# 읽는 화면(업무·일정)의 본문 기둥. 목록과 상세가 같은 폭을 써야 화면을
# 오갈 때 제목이 제자리에 있다. 표와 대조가 일인 화면(문서·질문)은 묶지 않는다.
CONTENT_MAX_W = 1120
SCROLLBAR_W = 10             # 아래 스타일시트의 QScrollBar:vertical 폭과 같아야 한다
=======
# 본문 기둥의 폭. 창이 넓어도 이 폭을 넘지 않고 **가운데에** 선다 — 왼쪽에
# 몰아 두면 넓은 화면의 오른쪽 3분의 1이 빈 채로 남아 화면이 기울어 보인다.
CONTENT_MAX_W = 1080
>>>>>>> Stashed changes
WINDOW_MIN = (1120, 720)

# 연간 패턴 격자 (일정 화면). 가장 좁은 창(WINDOW_MIN)의 본문 기둥 1062px에
# 들어가야 한다: 200 + 68×12 + 여백 24 = 1040.
GRID_NAME_W = 200            # 업무 이름 칸. 이보다 좁으면 이름이 자주 잘린다.
GRID_CELL_W = 68
GRID_ROW_H = 40
GRID_BAR_H = 24              # 막대 높이. 줄 높이보다 낮아야 행이 분리되어 보인다.

# 설정 창 왼쪽 갈래 목록
SETTINGS_NAV_W = 168


def apply(app, text_size: str = "medium", mode: str = "system") -> str:
    """앱 전체에 테마를 적용한다. 실제로 칠한 벌(light|dark)을 돌려준다.

    색 교체와 스타일시트 재적용은 항상 붙어 다녀야 한다 — 한쪽만 하면
    QSS는 어두운데 위젯이 직접 칠한 색은 밝은, 반쪽짜리 화면이 나온다.
    """
    resolved = apply_mode(mode)
    # 앱 기본 글꼴도 같은 이름으로 맞춘다. 스타일시트만 바꾸면 위젯이 들고
    # 있는 QFont는 옛 이름 그대로라, **재는 글꼴과 그리는 글꼴이 갈린다.**
    name = font_family()
    if name:
        from PySide6.QtGui import QFont

        font = QFont(app.font())
        font.setFamily(name)
        app.setFont(font)
    app.setStyleSheet(stylesheet(text_size))
    return resolved


def _combo_arrow() -> str:
    """드롭다운 화살표를 직접 그린 그림으로 바꾼다.

    기본 화살표를 그대로 두면 Qt가 플랫폼의 버튼 틀까지 함께 그려, 둥근
    테두리 오른쪽에 각진 선이 겹쳐 깨진 것처럼 보였다. QSS로 drop-down을
    건드리면 Qt가 플랫폼 화살표를 그리지 않으므로 그림을 꼭 대 줘야 한다 —
    border 삼각형 기법은 Qt에서 회색 네모로 나오고, image를 비우면 화살표가
    아예 사라진다. 그림을 만들 수 없는 환경(앱이 서기 전, QtSvg 없음)에서는
    손대지 않고 기본 화살표로 물러선다.
    """
    from . import icons

    arrow = icons.png_path("chevron-down", TEXT_MUTED, 14)
    if not arrow:
        return ""
    return f"""
    QComboBox {{ padding-right: {SP_XL}px; }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: center right;
        width: {SP_XL}px;
        border: none;
        background: transparent;
    }}
    QComboBox::down-arrow {{
        image: url({arrow});
        width: 14px;
        height: 14px;
        margin-right: {SP_SM}px;
    }}
    """


def _check_marks() -> str:
    """체크 상자를 강조색으로 맞춘다.

    맡겨 두면 윈도우의 강조색(사용자마다 다르다 — 노랑일 수도 청록일 수도)으로
    칠해져, 화면에서 이것만 다른 색으로 튄다. 그림을 만들 수 없는 환경에서는
    손대지 않는다 — 테두리만 바꾸고 체크 표시를 못 그리면 켠 것과 끈 것이
    구분되지 않는다.
    """
    from . import icons

    mark = icons.png_path("check", TEXT_ON_PRIMARY, 14)
    if not mark:
        return ""
    return f"""
    QCheckBox::indicator {{
        width: 16px;
        height: 16px;
        border: 1px solid {TEXT_SUBTLE};
        border-radius: {RADIUS_SM}px;
        background: {BG};
    }}
    QCheckBox::indicator:hover {{ border-color: {PRIMARY}; }}
    QCheckBox::indicator:checked {{
        background: {PRIMARY};
        border-color: {PRIMARY};
        image: url({mark});
    }}
    QCheckBox::indicator:disabled {{ border-color: {BORDER}; background: {SURFACE}; }}
    """


def stylesheet(text_size: str = "medium") -> str:
    """지금 칠해져 있는 색 벌로 전역 스타일시트를 짓는다.

    글자 크기는 소·중·대 3단계다(PRD §18.4 접근성). 별도 '고대비 테마'를
    두지 않는 이유: 이미 상태를 색상 하나로 구분하지 않고 기호+글자를 함께
    쓰도록 설계했다. 그래서 저시력 사용자에게 가장 먼저 도움이 되는 것은
    대비 반전보다 글자 크기다.

    단추는 세 무게뿐이다.
      Primary  칠한 강조색 — 한 구획에 하나. 그 자리에서 가장 권하는 동작.
      (기본)   흰 바탕에 가는 테두리 — 그다음 동작.
      Quiet    테두리 없는 옅은 판 — 줄마다 반복되는 동작. 여럿이 늘어서도
               화면이 단추밭이 되지 않는다.
    """
    scale = TEXT_SIZES[normalize_text_size(text_size)]
    body = _scaled(FS_BODY, scale)
    small = _scaled(FS_SMALL, scale)
    section = _scaled(FS_SECTION, scale)
    lead = _scaled(FS_LEAD, scale)
    title = _scaled(FS_TITLE, scale)
    lead = _scaled(FS_BODY + 1, scale)
    return f"""
    * {{
        font-family: {_qss_family()};
        font-size: {body}px;
        color: {TEXT};
    }}
    QMainWindow, QWidget#Content, QDialog {{ background: {BG}; }}

    /* ── 상단 띠 (길 + 상태) ──
       흰 띠에 밑줄 하나. 메뉴는 넷뿐이고 하루에 몇 번 누른다 — 내내 읽는
       본문보다 무겁게 칠할 이유가 없다. 고른 메뉴만 강조색 판으로 세운다. */
    QWidget#TopBar {{
        background: {NAVBAR_BG};
        border-bottom: 1px solid {NAVBAR_LINE};
    }}
    QLabel#AppName {{
        font-size: {lead}px;
        font-weight: 700;
        color: {NAVBAR_TEXT};
    }}
    QFrame#TopDivider {{ background: {NAVBAR_LINE}; border: none; }}
    QPushButton#NavTab {{
        background: transparent;
        border: none;
        border-radius: {RADIUS}px;
        color: {NAVBAR_MUTED};
        font-size: {body}px;
        font-weight: 600;
        padding: {SP_SM}px {SP_LG}px;
    }}
    QPushButton#NavTab:hover {{ background: {NAVBAR_HOVER}; color: {NAVBAR_TEXT}; }}
    QPushButton#NavTab:checked {{
        background: {NAVBAR_ACTIVE};
        color: {PRIMARY};
        font-weight: 700;
    }}
    QPushButton#NavTab:disabled {{ color: {NAVBAR_DISABLED}; }}
    /* 띠 오른쪽의 상태 문구들. 누를 수 있지만 평소엔 물러서 있다. */
    QPushButton#TopLink {{
        background: transparent;
        border: none;
        border-radius: {RADIUS_SM}px;
        color: {NAVBAR_MUTED};
        font-size: {small}px;
        font-weight: 400;
        padding: {SP_XS}px {SP_SM}px;
    }}
    QPushButton#TopLink:hover {{ background: {NAVBAR_HOVER}; color: {NAVBAR_TEXT}; }}
    /* 원본을 건드리지 않는다는 약속. 늘 보이되 소리치지 않는다. */
    QLabel#TopPromise {{
        color: {NAVBAR_MUTED};
        font-size: {small}px;
    }}
    /* 인수인계 진행도. 어느 화면에서든 "얼마나 남았나"가 보인다. */
    QPushButton#TopHandover {{
        background: {SURFACE_ALT};
        border: none;
        border-radius: 11px;
        color: {TEXT_MUTED};
        font-size: {small}px;
        font-weight: 600;
        padding: {SP_XS}px {SP_MD}px;
    }}
    QPushButton#TopHandover:hover {{ background: {PRIMARY_SOFT}; color: {PRIMARY}; }}
    QLabel#StatusText {{
        color: {NAVBAR_MUTED};
        font-size: {small}px;
    }}
    QProgressBar#TopProgress {{
        background: {SURFACE_ALT};
        border: none;
        border-radius: 3px;
        height: 6px;
        text-align: center;
    }}
    QProgressBar#TopProgress::chunk {{
        background: {PRIMARY};
        border-radius: 3px;
    }}

    /* ── 화면 머리 ──
       제목·설명·탭을 흰 띠에 묶는다. 본문은 그 아래 회색 바탕에서 스크롤되고
       머리는 남는다 — "지금 무엇을 보고 있는가"는 사라지면 안 된다. */
    QWidget#ViewHeader {{
        background: {BG};
        border-bottom: 1px solid {BORDER};
    }}
    QWidget#HeaderColumn {{ background: transparent; }}

    /* ── 화면 안 탭 ──
       글자와 밑줄뿐이다. 상자로 그리면 탭 다섯이 상자 다섯이 되어 그 아래
       카드들과 다툰다. 고른 탭은 글자를 세우고 강조색 밑줄을 긋는다. */
    QPushButton#Tab {{
        background: transparent;
        border: none;
        border-bottom: 2px solid transparent;
        border-radius: 0;
<<<<<<< Updated upstream
        padding: {SP_SM}px 2px {SP_SM + 2}px 2px;
        color: {TEXT_MUTED};
        font-weight: 600;
        min-height: 24px;
    }}
    QPushButton#Tab:hover {{
        color: {TEXT};
        border-bottom-color: {BORDER_STRONG};
    }}
    /* 고른 탭은 색과 밑줄이 함께 선다. 굵기는 바꾸지 않는다 — 굵기가 바뀌면
       탭 폭이 달라져 누를 때마다 옆 탭이 밀린다. */
    QPushButton#Tab:checked {{
        color: {PRIMARY};
=======
        padding: {SP_SM}px 2px {SP_MD}px 2px;
        margin-right: {SP_LG}px;
        color: {TEXT_SUBTLE};
        font-weight: 600;
        min-height: 24px;
    }}
    QPushButton#Tab:hover {{ color: {TEXT}; background: transparent; }}
    QPushButton#Tab:checked {{
        color: {TEXT};
        font-weight: 700;
>>>>>>> Stashed changes
        border-bottom: 2px solid {PRIMARY};
    }}
    QPushButton#Tab:focus {{ color: {PRIMARY}; }}
    QWidget#TabBar {{ background: transparent; }}

    /* ── 근거 서랍 ──
       본문에서 떼어 낸 판이다. 왼쪽 선으로 경계를 긋는다. */
    QFrame#EvidenceDrawer {{
        background: {BG};
<<<<<<< Updated upstream
        border-left: 1px solid {BORDER_STRONG};
=======
        border-left: 1px solid {BORDER};
>>>>>>> Stashed changes
    }}
    QFrame#EvidenceEntry {{
        border: 1px solid transparent;
        border-radius: {RADIUS}px;
    }}
    QFrame#EvidenceEntry[picked="true"] {{
        background: {PRIMARY_SOFT};
        border: 1px solid {PRIMARY};
    }}

    /* 질문 가이드 칩. 누르면 그 질문을 바로 묻는다. */
    QPushButton#Chip {{
<<<<<<< Updated upstream
        background: {BG};
        border: 1px solid {BORDER_STRONG};
        /* 반지름이 높이의 절반을 넘으면 Qt는 둥근 모서리를 아예 그리지 않는다.
           글꼴이 바뀌어 칩이 낮아지자 각진 상자가 되었다 — 높이를 못 박는다. */
        border-radius: 14px;
        min-height: 20px;
        padding: {SP_XS}px {SP_MD}px;
=======
        background: {SURFACE_ALT};
        border: none;
        border-radius: 12px;
        padding: 5px {SP_MD}px;
        min-height: 16px;
>>>>>>> Stashed changes
        color: {TEXT_MUTED};
        font-size: {small}px;
        font-weight: 400;
        text-align: left;
    }}
    QPushButton#Chip:hover {{ color: {PRIMARY}; background: {PRIMARY_SOFT}; }}
    QLabel#ChipText {{ color: {TEXT_MUTED}; font-size: {small}px; }}
    QPushButton#Chip:hover QLabel#ChipText {{ color: {PRIMARY}; }}

    /* ── 질문 화면: 근거 대조 ──
       왼쪽에서 근거를 고르면 오른쪽에 그 문서의 원문이 펴진다. 고른 것은
       색과 테두리 굵기를 함께 바꾼다 — 색만으로 구분하지 않는다. */
    QLabel#SourceMark {{
        background: {SURFACE_ALT}; color: {TEXT_MUTED};
        border-radius: {RADIUS_SM}px; font-weight: 700; font-size: {small}px;
    }}
    QLabel#SourceMarkOn {{
        background: {PRIMARY}; color: {TEXT_ON_PRIMARY};
        border-radius: {RADIUS_SM}px; font-weight: 700; font-size: {small}px;
    }}
    QFrame#EvidencePick {{
        background: {SURFACE};
        border: 1px solid transparent;
        border-radius: {RADIUS}px;
    }}
    QFrame#EvidencePick:hover {{ border-color: {BORDER_STRONG}; }}
    QFrame#EvidencePick[picked="true"] {{
        border: 2px solid {PRIMARY};
        background: {PRIMARY_SOFT};
    }}
    QLabel#EvidenceName {{ color: {TEXT}; font-weight: 600; }}
    QFrame#SubPanel[picked="true"] {{ border: 2px solid {CONFIRMED}; }}

    QWidget#ReaderPane {{
        background: {BG};
        border: 1px solid {BORDER};
        border-radius: {RADIUS_LG}px;
    }}
    QWidget#PaneBody {{ background: transparent; }}
    /* 답변 칸의 스크롤 영역. 바탕을 비워 회색 캔버스가 드러나게 한다 —
       카드가 어디서 끝나는지가 보여야 한다. */
    QScrollArea#AnswerPane,
    QScrollArea#AnswerPane > QWidget#qt_scrollarea_viewport {{
        background: transparent;
    }}
<<<<<<< Updated upstream
    /* 문서 화면 오른쪽 상세. 표와 나란히 서는 흰 판이라 같은 테두리를 쓴다. */
    QFrame#DetailFrame {{
=======
    QScrollArea#DetailPane {{
>>>>>>> Stashed changes
        background: {BG};
        border: 1px solid {BORDER};
        border-radius: {RADIUS_LG}px;
    }}
    QScrollArea#DetailPane {{ background: transparent; border: none; }}
    QLabel#ParaNumber {{ color: {TEXT_DISABLED}; font-size: {small}px; }}
    QLabel#ParaText {{ color: {TEXT_MUTED}; line-height: 180%; }}
    /* 인용된 대목. 원문에서 눈이 이 대목을 바로 찾는 것이 이 칸의 목적이다. */
    QLabel#ParaCited {{
        color: {TEXT};
        background: {PRIMARY_SOFT};
        border-radius: {RADIUS_SM}px;
        padding: 3px 8px;
        line-height: 180%;
    }}
    QPushButton#Fold {{
        background: transparent;
        border: 1px dashed {BORDER_STRONG};
        border-radius: {RADIUS_SM}px;
        color: {TEXT_SUBTLE};
        font-size: {small}px;
        font-weight: 400;
        padding: {SP_XS}px {SP_SM}px;
    }}
    QPushButton#Fold:hover {{ color: {PRIMARY}; border-color: {PRIMARY}; }}

    QFrame#AnswerCard {{
        background: {BG};
        border: 1px solid {BORDER};
        border-radius: {RADIUS_LG}px;
    }}
    /* 답이 이 화면에서 가장 큰 글자다. */
    QLabel#AnswerBody {{
        color: {TEXT};
        font-size: {lead}px;
        line-height: 175%;
    }}

    QProgressBar#Thinking {{
        background: {SURFACE_ALT};
        border: none;
        border-radius: 2px;
        height: 3px;
        text-align: center;
    }}
    QProgressBar#Thinking::chunk {{
        background: {PRIMARY};
        border-radius: 2px;
    }}

    /* 인용문은 '이 앱이 쓴 말'이 아니라 '문서에 있던 말'이다. 옅은 판에
       얹고 왼쪽에 띠를 둔다. */
    QLabel#EvidenceQuote {{
        background: {SURFACE};
        border-left: 3px solid {BORDER_STRONG};
        border-radius: {RADIUS_SM}px;
        padding: {SP_SM}px {SP_MD}px;
        color: {TEXT};
    }}

    /* ── 글자 위계 ── */
    QLabel#ViewTitle {{
        font-size: {title}px;
        font-weight: 700;
<<<<<<< Updated upstream
    }}
    /* 화면이 내놓는 값(기한·제출처·서식). 표지와 부기는 small로 물러서고
       값만 한 단계 크게 선다 — 한 줄에서 눈이 먼저 닿아야 하는 것이 이것이다. */
    QLabel#Value {{
        font-size: {lead}px;
        font-weight: 600;
        color: {TEXT};
    }}
    /* 값 위에 붙는 표지("기한 · 제출기한"). 부기와 같은 크기지만 굵게 세워
       '이름표'로 읽히게 한다. */
    QLabel#FieldName {{
        color: {TEXT_MUTED};
        font-size: {small}px;
        font-weight: 700;
=======
>>>>>>> Stashed changes
    }}
    QLabel#ViewLead {{
        color: {TEXT_MUTED};
        font-size: {body}px;
    }}
    QLabel#SectionTitle {{
        font-size: {section}px;
        font-weight: 700;
    }}
    QLabel#SectionTag {{
        background: {PRIMARY_SOFT};
        color: {PRIMARY};
        border-radius: {RADIUS_SM}px;
        padding: 2px {SP_SM}px;
        font-size: {small}px;
        font-weight: 700;
    }}
    /* 화면이 내놓는 답 한 줄. 근거·부연과 같은 크기로 두면 답이 묻힌다. */
    QLabel#Answer {{ font-size: {lead}px; font-weight: 700; color: {TEXT}; }}
    QLabel#Muted {{ color: {TEXT_MUTED}; }}
    QLabel#Small {{ color: {TEXT_SUBTLE}; font-size: {small}px; }}
    QLabel#Mono {{
        font-family: "Consolas", "D2Coding", monospace;
        font-size: {small}px;
        color: {TEXT_SUBTLE};
    }}
    QPlainTextEdit#Mono {{
        font-family: "Consolas", "D2Coding", monospace;
        font-size: {small}px;
        color: {TEXT_MUTED};
        background: {BG};
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
    }}
    /* 구획 위에 얹는 작은 이름표("이렇게 판단한 근거"). */
    QLabel#BlockLabel {{
        color: {TEXT_SUBTLE};
        font-size: {small}px;
        font-weight: 700;
        padding-top: {SP_XS}px;
    }}
    /* 규모·시점처럼 "몇 건, 몇 년치"를 말하는 중립 조각. */
    QLabel#MetaChip {{
        background: {SURFACE_ALT};
        color: {TEXT_MUTED};
        border-radius: {RADIUS_SM}px;
        padding: 2px {SP_SM}px;
        font-size: {small}px;
        font-weight: 600;
    }}
    QFrame#ActionBar {{
        background: transparent;
        border: none;
    }}
    QLabel#PageNote {{
        background: {SURFACE_ALT};
        border-radius: {RADIUS_SM}px;
        padding: {SP_SM}px {SP_MD}px;
        color: {TEXT_MUTED};
        font-size: {small}px;
    }}

    /* ── 항목 한 줄 (이름표 · 값) ──
       '지금 할 일'처럼 값을 늘어놓는 자리. 이름표는 물러서고 값이 선다. */
    QLabel#FieldKey {{ color: {TEXT_SUBTLE}; font-size: {small}px; font-weight: 600; }}
    QLabel#FieldValue {{ color: {TEXT}; font-size: {lead}px; }}
    /* 값에 붙는 단서. 기본 상태에는 붙지 않고, 알아야 할 때만 붙는다. */
    QLabel#Qualifier {{ color: {ATTENTION}; font-size: {small}px; }}
    QLabel#QualifierOk {{ color: {CONFIRMED}; font-size: {small}px; }}
    /* 카드 안에서 줄과 줄을 가르는 가는 선. 줄마다 상자를 두르지 않는다. */
    QFrame#Row {{ background: transparent; border: none; border-top: 1px solid {BORDER}; }}
    QFrame#RowFirst {{ background: transparent; border: none; }}

    /* ── 지금 할 수 있는 일 ──
       이 화면이 내놓는 답. 강조색 옅은 판에 얹어 가장 먼저 읽히게 한다. */
    QFrame#Hero {{
        background: {PRIMARY_SOFT};
        border: none;
        border-radius: {RADIUS_LG}px;
    }}
    QLabel#Eyebrow {{ color: {PRIMARY}; font-size: {small}px; font-weight: 700; }}
    QLabel#HeroText {{ color: {TEXT}; font-size: {section}px; font-weight: 600; }}
    QLabel#HeroNote {{ color: {TEXT_MUTED}; font-size: {small}px; }}

    /* 순서를 말하는 둥근 번호. */
    QLabel#StepNumber, QLabel#StepNumberDone, QLabel#StepNumberNow {{
        border-radius: 12px;
        font-size: {small}px;
        font-weight: 700;
    }}
    QLabel#StepNumber {{ background: {SURFACE_ALT}; color: {TEXT_MUTED}; }}
    QLabel#StepNumberDone {{ background: {CONFIRMED_SOFT}; color: {CONFIRMED}; }}
    QLabel#StepNumberNow {{ background: {PRIMARY}; color: {TEXT_ON_PRIMARY}; }}

    /* ── 접어 둔 설명 ── */
    QLabel#InfoDot {{
        background: transparent;
        color: {TEXT_SUBTLE};
        border: 1px solid {BORDER_STRONG};
        border-radius: 8px;
        font-size: {MIN_FONT_PX}px;
        font-weight: 700;
    }}
    QLabel#InfoDot:hover, QLabel#InfoDot:focus {{
        background: {PRIMARY_SOFT};
        border-color: {PRIMARY};
        color: {PRIMARY};
    }}
    QToolTip {{
        background: {BG};
        color: {TEXT};
        border: 1px solid {BORDER_STRONG};
        border-radius: {RADIUS_SM}px;
        padding: {SP_SM}px {SP_MD}px;
        font-size: {small}px;
    }}

<<<<<<< Updated upstream
    /* 카드는 회색 바탕 위의 흰 판이다. 바탕과 색이 이미 갈리므로 테두리는
       옅게만 둔다 — 짙은 테두리를 판마다 두르면 화면이 양식 문서의 칸처럼
       보인다. 마우스가 스친다고 테두리가 파래지지도 않는다: 카드 대부분은
       누르는 것이 아니라 읽는 것이고, 누를 수 있는 카드는 제가 따로 말한다. */
    QFrame#Card, QFrame#CardStatic {{
        background: {BG};
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
    }}
    /* 왼쪽 굵은 띠로 급한 것을 표시한다 — 뱃지 하나보다 멀리서 보인다. */
    QFrame#CardAttention {{
        background: {BG};
        border: 1px solid {BORDER};
        border-left: 4px solid {ATTENTION};
        border-radius: {RADIUS}px;
    }}
    QFrame#CardPrimary {{
        background: {BG};
        border: 1px solid {BORDER};
        border-left: 4px solid {PRIMARY};
        border-radius: {RADIUS}px;
    }}
=======
    /* ── 카드 ──
       흰 판에 가는 윤곽 하나. 마우스가 스쳐도 변하지 않는다 — 누를 수 없는
       판이 파래지면 "눌러야 하나?" 싶어진다. 누를 수 있는 카드만 반응한다. */
    QFrame#Card, QFrame#CardStatic, QFrame#CardAttention, QFrame#CardPrimary {{
        background: {BG};
        border: 1px solid {BORDER};
        border-radius: {RADIUS_LG}px;
    }}
    /* 급한 것은 왼쪽 띠로 먼저 말한다 — 뱃지 하나보다 멀리서 보인다. */
    QFrame#CardAttention {{ border-left: 3px solid {ATTENTION}; }}
    QFrame#CardPrimary {{ border-left: 3px solid {PRIMARY}; }}

    /* 업무 카드. 눌러서 들어가는 카드라 이것만 마우스에 반응한다. */
    QFrame#TaskCard {{
        background: {BG};
        border: 1px solid {BORDER};
        border-radius: {RADIUS_LG}px;
    }}
    QFrame#TaskCard:hover {{ border-color: {PRIMARY}; }}
    QLabel#CardName {{ font-size: {section}px; font-weight: 700; color: {TEXT}; }}
>>>>>>> Stashed changes

    QPushButton#CardTitle {{
        background: transparent;
        border: none;
        padding: 0;
        text-align: left;
        font-size: {lead}px;
        font-weight: 700;
        color: {TEXT};
    }}
    QPushButton#CardTitle:hover {{ color: {PRIMARY}; }}

<<<<<<< Updated upstream
    /* 카드 안의 근거 묶음. 본문과 같은 흰 바탕에 두면 어디까지가 근거인지
       경계가 사라진다. 경계는 바탕 톤만으로 만든다 — 카드 안에 테두리 상자를
       또 두면 상자 속 상자가 되어, 줄마다 칸을 친 서식처럼 보인다. */
    QFrame#SubPanel {{
        background: {SURFACE};
        border: 1px solid {SURFACE};
        border-radius: 6px;
=======
    /* 카드 안에서 한 단계 물린 자리. 테두리 없이 옅은 바탕만으로 묶는다. */
    QFrame#SubPanel {{
        background: {SURFACE};
        border: 1px solid transparent;
        border-radius: {RADIUS}px;
>>>>>>> Stashed changes
    }}

    /* ── 목록 한 줄 ── */
    QFrame#ListRow {{
        background: {BG};
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
    }}
    QFrame#ListRow:hover {{ background: {BAND}; }}
    QLabel#WhenChip {{
        background: {PRIMARY_SOFT};
        color: {PRIMARY};
        border-radius: {RADIUS_SM}px;
        padding: {SP_XS}px {SP_SM}px;
        font-size: {small}px;
        font-weight: 700;
    }}
    QPushButton#RowTitle {{
        background: transparent;
        border: none;
        padding: 0;
        text-align: left;
        font-weight: 600;
        color: {TEXT};
    }}
    QPushButton#RowTitle:hover {{ color: {PRIMARY}; }}

    /* ── 연간 패턴 격자 ── */
    QFrame#GridPanel {{
        background: {BG};
        border: 1px solid {BORDER};
        border-radius: {RADIUS_LG}px;
    }}
    QFrame#GridHeadRow {{
        background: transparent;
        border-bottom: 1px solid {BORDER};
    }}
    QLabel#GridHeadCell {{
        color: {TEXT_SUBTLE};
        font-size: {small}px;
        font-weight: 600;
    }}
    QLabel#GridHeadCellNow {{
        background: {PRIMARY};
        color: {TEXT_ON_PRIMARY};
        font-size: {small}px;
        font-weight: 700;
        border-radius: {RADIUS_SM}px;
    }}
    QFrame#GridRow, QFrame#GridRowAlt {{
        background: transparent; border-bottom: 1px solid {BORDER};
    }}
    /* 마지막 줄은 밑줄을 지운다 — 판 테두리와 겹쳐 두 줄로 보인다. */
    QFrame#GridRowLast, QFrame#GridRowAltLast {{ background: transparent; border: none; }}
    QFrame#GridRow:hover, QFrame#GridRowAlt:hover,
    QFrame#GridRowLast:hover, QFrame#GridRowAltLast:hover {{
        background: {BAND};
    }}
    QPushButton#GridName {{
        background: transparent;
        border: none;
        padding: 0;
        text-align: left;
        font-weight: 600;
        color: {TEXT};
    }}
    QPushButton#GridName:hover {{ color: {PRIMARY}; }}
    QLabel#GridName {{ color: {TEXT_SUBTLE}; font-size: {small}px; font-weight: 600; }}

    /* ── 업무 화면 조각 ── */
    QPushButton#BackLink {{
        background: transparent;
        border: none;
        padding: 0;
        color: {TEXT_SUBTLE};
        font-size: {small}px;
        font-weight: 600;
        text-align: left;
    }}
    QPushButton#BackLink:hover {{ color: {PRIMARY}; }}
    /* 글자로 이미 ▾를 쓰고 있다. Qt가 그리는 화살표까지 나오면 둘이 된다. */
    QPushButton#MenuButton::menu-indicator {{ image: none; width: 0; }}

    QLabel#RankChip {{
        background: {SURFACE_ALT};
        color: {TEXT_MUTED};
        border-radius: 12px;
        font-size: {small}px;
        font-weight: 700;
    }}
    QLabel#StepMark {{
        color: {TEXT_MUTED};
        font-weight: 700;
        font-size: {lead}px;
    }}
    QLabel#StepWhen {{
        color: {TEXT_SUBTLE};
        font-size: {small}px;
    }}
    QLabel#StepLabel {{ font-weight: 600; }}
    QLabel#StepLabelInferred {{ color: {TEXT_MUTED}; font-style: italic; }}
    QLabel#StepLabelHere {{ color: {PRIMARY}; font-weight: 700; }}
    QLabel#CompareChanged {{ color: {PRIMARY}; font-weight: 700; }}
    QLabel#YearChip {{
        background: {SURFACE_ALT};
        color: {TEXT_MUTED};
        border-radius: {RADIUS_SM}px;
        padding: 2px {SP_SM}px;
        font-size: {small}px;
        font-weight: 600;
    }}
    QLabel#PrimaryMark {{ color: {ATTENTION}; font-weight: 700; }}
    QLabel#LeftoverHead {{ color: {TEXT_MUTED}; font-size: {small}px; }}
    QLabel#StartHereHead {{ color: {TEXT}; font-weight: 600; }}
    QLabel#StageDone {{ color: {CONFIRMED}; font-weight: 600; }}

    /* 줄 끝에 붙는 작은 조작(⋯ ▲ ▼). */
    QPushButton#IconButton {{
        background: transparent;
        border: none;
        border-radius: {RADIUS_SM}px;
        padding: 4px 0;
        color: {TEXT_SUBTLE};
        font-size: {small}px;
        font-weight: 700;
    }}
    QPushButton#IconButton:hover {{ background: {SURFACE_ALT}; color: {TEXT}; }}
    QPushButton#IconButton:disabled {{ color: {BORDER_STRONG}; }}
    /* 화살표가 글자를 밀어내면 ⋯이 사라진다 — 무엇을 누르는지 알 수 없다. */
    QPushButton#IconButton::menu-indicator {{ image: none; width: 0; }}

    QFrame#Divider {{ background: {BORDER}; max-height: 1px; border: none; }}

    /* ── 단추 ── */
    QPushButton {{
        background: {BG};
        border: 1px solid {BORDER_STRONG};
        border-radius: {RADIUS}px;
        padding: 7px {SP_LG}px;
        color: {TEXT};
        font-weight: 600;
    }}
    QPushButton:hover {{ background: {SURFACE}; border-color: {TEXT_SUBTLE}; }}
    QPushButton:pressed {{ background: {SURFACE_ALT}; }}
    QPushButton:disabled {{ color: {TEXT_DISABLED}; border-color: {BORDER}; }}
    QPushButton#Primary {{
        background: {PRIMARY};
        border: 1px solid {PRIMARY};
        color: {TEXT_ON_PRIMARY};
        font-weight: 700;
    }}
    QPushButton#Primary:hover {{ background: {PRIMARY_HOVER}; border-color: {PRIMARY_HOVER}; }}
    QPushButton#Primary:disabled {{
        background: {SURFACE_ALT}; border-color: {SURFACE_ALT}; color: {TEXT_DISABLED};
    }}
<<<<<<< Updated upstream
    QPushButton#Primary:hover {{ background: {PRIMARY_HOVER}; }}
    /* 못 누르는 주 버튼은 못 누르게 보여야 한다. 이 규칙이 없으면 파란 채움이
       그대로 남아, 자료 폴더를 고르기 전의 '분석 시작'이 눌릴 것처럼 보였다. */
    QPushButton#Primary:disabled {{
        background: {SURFACE_ALT};
        border: 1px solid {BORDER};
        color: {TEXT_DISABLED};
    }}
    /* ── 확인 창·입력 창 ──
       Qt가 지어 주는 창이라 배치는 못 바꾼다. 입력 창에는 최소 폭을 주고,
       기본으로 눌리는 버튼(Enter를 치면 눌리는 것)을 채운 버튼으로 세워 어느
       쪽이 기본인지 보이게 한다. 지우기처럼 되돌릴 수 없는 물음은 기본이
       '아니요'라서 안전한 쪽이 강조된다. 확인 창의 글에는 폭을 주지 않는다 —
       최소 폭을 주면 Qt가 줄바꿈을 다시 재지 않아 긴 문장이 잘린다. */
    QInputDialog {{ min-width: 440px; }}
    QMessageBox QPushButton, QInputDialog QPushButton {{ min-width: 72px; }}
    QMessageBox QPushButton:default, QInputDialog QPushButton:default {{
        background: {PRIMARY};
        border: 1px solid {PRIMARY};
        color: {TEXT_ON_PRIMARY};
        font-weight: 600;
    }}
    QMessageBox QPushButton:default:hover, QInputDialog QPushButton:default:hover {{
        background: {PRIMARY_HOVER};
    }}
    /* 키보드로 옮겨 다닐 때 지금 어디에 있는지 보여야 한다. 버튼에는 포커스
       표시가 아예 없어서 Tab을 눌러도 화면에 아무 변화가 없었다. */
    QPushButton:focus {{ border: 1px solid {PRIMARY}; }}
    QPushButton#Primary:focus {{ border: 2px solid {TEXT}; }}
    QPushButton#Link:focus, QPushButton#BackLink:focus, QPushButton#RowTitle:focus,
    QPushButton#CardTitle:focus, QPushButton#GridName:focus {{
        border: none;
        color: {PRIMARY};
        text-decoration: underline;
    }}
    QPushButton#Quiet:focus, QPushButton#IconButton:focus, QPushButton#Chip:focus,
    QPushButton#Destructive:focus, QPushButton#MenuButton:focus,
    QPushButton#ChoiceItem:focus {{ border: 1px solid {PRIMARY}; }}
    QPushButton#Confirm:focus {{ border: 2px solid {PRIMARY}; }}
    QPushButton#SettingsNavItem:focus {{ color: {PRIMARY}; }}
    QPushButton#NavTab:focus {{ border: 1px solid {NAVBAR_MUTED}; }}
    QPushButton#TopLink:focus {{ border: none; color: {NAVBAR_TEXT}; }}
    QPushButton#TopHandover:focus {{ border: 1px solid {NAVBAR_MUTED}; }}
    QPushButton#Tab:focus {{ border: none; border-bottom: 2px solid {PRIMARY}; }}
=======
>>>>>>> Stashed changes
    QPushButton#Link {{
        background: transparent;
        border: none;
        color: {PRIMARY};
        padding: {SP_XS}px 0;
        text-align: left;
        font-weight: 600;
    }}
    QPushButton#Link:hover {{ text-decoration: underline; }}
    /* 지우는 단추. 파란 단추와 같은 모양이면 손이 먼저 가고 눈이 나중에 온다. */
    QPushButton#Destructive {{
        background: transparent;
        border: none;
        color: {DANGER};
        padding: 5px {SP_MD}px;
    }}
    QPushButton#Destructive:hover {{ background: {DANGER_SOFT}; }}
    /* 확인 동작. 옅은 강조색 판 — 권하되 채근하지 않는다. */
    QPushButton#Confirm {{
        background: {PRIMARY_SOFT};
        border: none;
        color: {PRIMARY};
        font-weight: 700;
        padding: 5px {SP_MD}px;
    }}
    QPushButton#Confirm:hover {{ background: {PRIMARY}; color: {TEXT_ON_PRIMARY}; }}
    /* 줄마다 반복되는 동작. 테두리 없는 옅은 판이라 여럿이 늘어서도 조용하다. */
    QPushButton#Quiet {{
        background: {SURFACE_ALT};
        border: none;
        color: {TEXT_MUTED};
        padding: 5px {SP_MD}px;
    }}
    QPushButton#Quiet:hover {{ background: {BORDER_STRONG}; color: {TEXT}; }}
    QPushButton#Quiet:disabled {{ color: {TEXT_DISABLED}; }}

    QMenu {{
        background: {BG};
        border: 1px solid {BORDER_STRONG};
        border-radius: {RADIUS}px;
        padding: {SP_XS}px;
    }}
    QMenu::item {{
        padding: 7px {SP_XL}px 7px {SP_MD}px;
        border-radius: {RADIUS_SM}px;
        color: {TEXT};
    }}
    QMenu::item:selected {{ background: {PRIMARY_SOFT}; color: {PRIMARY}; }}
    QMenu::item:disabled {{ color: {TEXT_DISABLED}; }}
    QMenu::separator {{ height: 1px; background: {BORDER}; margin: {SP_XS}px {SP_SM}px; }}

    /* ── 설정 창 ── */
    QWidget#SettingsNav {{
        background: {NAV_BG};
        border-right: 1px solid {BORDER};
    }}
    QPushButton#SettingsNavItem {{
        background: transparent;
        border: none;
        border-radius: {RADIUS}px;
        padding: {SP_MD}px {SP_MD}px;
        text-align: left;
        color: {TEXT_MUTED};
        font-weight: 600;
    }}
    QPushButton#SettingsNavItem:hover {{ background: {NAV_HOVER}; color: {TEXT}; }}
    QPushButton#SettingsNavItem:checked {{
        background: {BG};
        color: {PRIMARY};
        font-weight: 700;
    }}
    QWidget#SettingsBody {{ background: {CANVAS}; }}
    QScrollArea#SettingsScroll {{ background: {CANVAS}; border: none; }}
    QScrollArea#SettingsScroll::viewport {{ background: {CANVAS}; }}
    QWidget#SettingsPane {{ background: {CANVAS}; }}
    QFrame#SettingsFooter {{
        background: {BG};
        border: none;
        border-top: 1px solid {BORDER};
    }}
<<<<<<< Updated upstream
    QLabel#SettingsHeading {{ font-size: {round(title * 0.84)}px; font-weight: 700; }}
    QLabel#SettingsLead {{ color: {TEXT_MUTED}; font-size: {small}px; }}
    /* 설정 항목 하나의 이름. 카드 제목(SectionTitle)보다 한 단계 작다 —
       카드 안에 항목이 둘 이상 들어가므로 위계가 한 층 더 필요하다. */
=======
    QLabel#SettingsHeading {{ font-size: {title}px; font-weight: 700; }}
    QLabel#SettingsLead {{ color: {TEXT_SUBTLE}; font-size: {small}px; }}
>>>>>>> Stashed changes
    QLabel#FieldLabel {{ font-weight: 700; }}
    QLabel#FieldHint {{ color: {TEXT_SUBTLE}; font-size: {small}px; }}

    /* ── 아이콘 선택 묶음 (글자 크기·테마) ──
       QPushButton은 제 글자를 기준으로 크기를 잡는다 — 안에 레이아웃을 넣어도
       그 사실은 변하지 않아, 최소 높이를 주지 않으면 그림과 글자가 겹친다. */
    QWidget#ChoiceGroup {{ background: transparent; }}
    QPushButton#ChoiceItem {{
        background: {BG};
        border: 1px solid {BORDER_STRONG};
        border-radius: {RADIUS}px;
        padding: 0;
        min-width: 88px;
        min-height: {round(72 * scale)}px;
    }}
    QPushButton#ChoiceItem:hover {{ border-color: {PRIMARY}; }}
    QPushButton#ChoiceItem:checked {{
        background: {PRIMARY_SOFT};
        border: 2px solid {PRIMARY};
    }}
    QLabel#ChoiceCaption {{ color: {TEXT_MUTED}; font-size: {small}px; font-weight: 600; }}
    QPushButton#ChoiceItem:checked QLabel#ChoiceCaption {{
        color: {PRIMARY};
        font-weight: 700;
    }}

    /* ── 입력 컨트롤 ──
       입력칸·드롭다운·단추가 한 줄에 서면 높이와 모서리가 같아야 한 벌로 보인다. */
    QLineEdit, QComboBox {{
        background: {BG};
        border: 1px solid {BORDER_STRONG};
        border-radius: {RADIUS}px;
        padding: 0 {SP_MD}px;
        min-height: {CONTROL_H - 2}px;
        color: {TEXT};
        selection-background-color: {PRIMARY_SOFT};
        selection-color: {TEXT};
    }}
    QLineEdit:hover, QComboBox:hover {{ border-color: {TEXT_SUBTLE}; }}
    QLineEdit:focus, QComboBox:focus {{
        border: 2px solid {PRIMARY};
        padding: 0 {SP_MD - 1}px;
    }}
    QLineEdit:disabled, QComboBox:disabled {{
        background: {SURFACE};
        color: {TEXT_DISABLED};
        border-color: {BORDER};
    }}
<<<<<<< Updated upstream
    {_combo_arrow()}
=======
    /* 화살표는 손대지 않는다. QSS로 drop-down·down-arrow를 건드리는 순간
       Qt가 플랫폼 화살표 그리기를 멈춘다 — 기본 화살표가 가장 낫다. */
>>>>>>> Stashed changes
    QComboBox QAbstractItemView {{
        background: {BG};
        border: 1px solid {BORDER_STRONG};
        border-radius: {RADIUS_SM}px;
        padding: {SP_XS}px;
        outline: none;
        selection-background-color: {PRIMARY_SOFT};
        selection-color: {TEXT};
    }}
    QComboBox QAbstractItemView::item {{
        min-height: {ROW_H - 6}px;
        padding: 0 {SP_SM}px;
        border-radius: {RADIUS_SM}px;
    }}
<<<<<<< Updated upstream
    QCheckBox {{ spacing: {SP_SM}px; }}
    {_check_marks()}
=======
    QCheckBox {{ spacing: {SP_SM}px; color: {TEXT_MUTED}; }}
    QRadioButton {{ spacing: {SP_SM}px; }}
>>>>>>> Stashed changes

    QTreeView, QTableView, QListView {{
        background: {BG};
        border: 1px solid {BORDER};
        border-radius: {RADIUS_LG}px;
        alternate-background-color: {BAND};
        selection-background-color: {PRIMARY_SOFT};
        selection-color: {TEXT};
        outline: none;
    }}
    QHeaderView::section {{
        background: {BG};
        border: none;
        border-bottom: 1px solid {BORDER};
        padding: {SP_SM}px {SP_MD}px;
        color: {TEXT_SUBTLE};
        font-size: {small}px;
        font-weight: 600;
    }}
    /* 고른 줄. 글자색은 건드리지 않는다 — 상태 칸의 색(정상·읽기 실패)이 지워진다. */
    QTableView::item:selected {{ background: {PRIMARY_SOFT}; }}
    QSplitter::handle {{ background: transparent; }}

    /* 스크롤 영역은 뷰포트와 내부 위젯까지 배경을 지정해야 한다.
       transparent로 두면 칠해지지 않은 영역이 검게 남는다. */
    QScrollArea {{ border: none; background: {BG}; }}
    QScrollArea > QWidget > QWidget {{ background: {BG}; }}
    QAbstractScrollArea::viewport {{ background: {BG}; }}

    /* 본문 바탕을 한 단계 낮춘 화면. ID 선택자라 위의 일반 규칙을 이긴다 —
       뷰포트까지 지정하지 않으면 스크롤할 때 흰 띠가 남는다. */
    QWidget#Canvas, QWidget#PageBody {{ background: {CANVAS}; }}
    /* 본문 기둥은 판이 아니라 폭 제한용 그릇이다. 위의 일반 규칙
       (QScrollArea > QWidget > QWidget)이 흰색을 칠하지 못하게 막는다. */
    QWidget#PageColumn {{ background: transparent; }}
    QScrollArea#PageScroll {{ background: {CANVAS}; }}
    QScrollArea#PageScroll::viewport {{ background: {CANVAS}; }}
    QScrollBar:vertical {{
<<<<<<< Updated upstream
        background: transparent; width: {SCROLLBAR_W}px; margin: 0;
=======
        background: transparent; width: 10px; margin: 2px;
>>>>>>> Stashed changes
    }}
    QScrollBar::handle:vertical {{
        background: {BORDER_STRONG}; border-radius: 3px; min-height: 32px;
    }}
<<<<<<< Updated upstream
    /* 나눔 손잡이. 손대지 않으면 플랫폼이 짙은 띠를 그려, 표와 상세 사이에
       굵은 검은 선이 선 것처럼 보였다. 평소엔 비워 두고 올렸을 때만 보인다. */
    QSplitter::handle {{ background: transparent; }}
    QSplitter::handle:horizontal {{ width: {SP_MD}px; }}
    QSplitter::handle:horizontal:hover {{ background: {BORDER}; border-radius: 3px; }}
=======
    QScrollBar::handle:vertical:hover {{ background: {TEXT_SUBTLE}; }}
>>>>>>> Stashed changes
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

    /* ── 상태 뱃지 ──
       예외에만 붙인다. 색만으로 구분하지 않도록 기호와 글자를 함께 쓴다. */
    QLabel#BadgeNeutral, QLabel#BadgeAttention,
    QLabel#BadgeDanger, QLabel#BadgeOk, QLabel#BadgeAccent {{
        border-radius: {RADIUS_SM}px;
        padding: 2px {SP_SM}px;
        font-size: {small}px;
        font-weight: 600;
    }}
    QLabel#BadgeNeutral   {{ background: {SURFACE_ALT}; color: {TEXT_MUTED}; }}
    QLabel#BadgeAttention {{ background: {ATTENTION_SOFT}; color: {ATTENTION}; }}
    QLabel#BadgeDanger    {{ background: {DANGER_SOFT}; color: {DANGER}; }}
    QLabel#BadgeOk        {{ background: {CONFIRMED_SOFT}; color: {CONFIRMED}; }}
    QLabel#BadgeAccent    {{ background: {PRIMARY_SOFT}; color: {PRIMARY}; }}

    /* ── 확인되지 않은 구간 (UnknownBlock) ──
       없는 것을 지어내지 않고 없다고 말하는 자리. 오류가 아니라 사실이라
       옅은 판에 조용히 얹는다. */
    QFrame#UnknownBlock {{
        background: {ATTENTION_SOFT};
        border: none;
        border-radius: {RADIUS}px;
    }}
    QLabel#UnknownText {{ color: {ATTENTION}; font-size: {small}px; }}
    """
