"""눈치코치(NunchiCoach) — 로컬 AI 기반 업무 인수인계 도구.

정체성: 전임자가 남긴 자료 더미에서 후임자가 What(무슨 업무) · When(언제) ·
How(어떻게 처리했나)를 근거 문서와 함께 확인할 수 있게 한다.

설계 기준: doc/00_제품_정체성_및_MVP_메뉴체계.md
"""

import warnings

__version__ = "0.1.0"

# 엑셀이 저장한 드롭다운(다른 시트를 참조하는 목록)은 openpyxl이 못 읽는 확장 꼴이라 시트를
# 읽을 때마다 경고를 낸다. read_only로 여는 시트는 행을 훑는 시점에 읽혀서 호출 지점마다
# catch_warnings로 감쌀 수 없다. 이 앱은 사용자 통합문서를 읽기만 하고 다시 저장하지 않으며,
# 저장하는 서식은 앱이 직접 만든 것이라 버려지는 유효성 검사가 없다.
warnings.filterwarnings(
    "ignore",
    message="Data Validation extension is not supported",
    module=r"openpyxl\.worksheet\._reader",
)
