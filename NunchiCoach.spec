# -*- mode: python ; coding: utf-8 -*-
"""눈치코치 오프라인 패키징 spec.

빌드:
    python -m PyInstaller NunchiCoach.spec

산출물은 dist/NunchiCoach/ (onedir)에 생긴다. 이 폴더 전체를 복사하면
그대로 다른 PC에서 실행된다 — Python이나 pip이 설치되어 있지 않아도 된다.

실행 파일 이름을 영문으로 두는 이유
    한글 파일명은 콘솔 코드페이지나 배포 경로에 따라 깨질 수 있다. 화면에
    보이는 제품명은 한글("눈치코치")이지만 파일 수준 식별자는 영문으로
    통일한다 — start.bat에서 겪은 인코딩 문제와 같은 종류를 피한다.

entry.py를 쓰는 이유
    app/main.py는 `python -m app.main`으로 실행되는 것을 전제로 상대
    임포트를 쓴다. PyInstaller가 그 파일을 최상위 스크립트로 직접 분석하면
    `app` 패키지 컨텍스트가 없어 "attempted relative import with no known
    parent package" 오류가 난다. entry.py는 저장소 루트에서 `app`을 정식
    패키지로 임포트해 이 문제를 우회하는 얇은 진입점이다.

console=True로 두는 이유
    콘솔을 숨기면(windowed) 크래시가 아무 흔적 없이 사라진다. 배치
    런처(start.bat)와 같은 원칙이다 — 오류가 나면 사용자와 개발자 모두
    볼 수 있어야 한다. 제품이 충분히 안정화된 뒤 console=False로 바꾸는
    것을 고려한다.

data 파일
    app/ai/prompts/*.md 와 app/db/schema.sql 은 코드가 아니라서 PyInstaller가
    자동으로 못 찾는다. 명시적으로 포함해야 하며, 원본과 같은 상대 경로
    구조를 유지해야 코드의 Path(__file__).with_name(...) 조회가 그대로
    맞는다.

오픈소스 고지문
    빌드할 때마다 지금 환경에서 다시 만든다(app.tools.notices). 실행물 안
    (app/licenses, 설정 화면이 읽는다)과 실행 파일 옆(받은 사람이 프로그램을
    띄우지 않고도 읽는다) 두 곳에 둔다.
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, SPECPATH)
from app.tools import notices

NOTICES = notices.write(Path(SPECPATH) / 'build' / 'licenses')
for name in notices.missing_texts():
    print(f'WARNING: 고지문에 {name[:-4]} 본문이 없다 — {notices.CANONICAL[name]}')

a = Analysis(
    ['entry.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('app/ai/prompts', 'app/ai/prompts'),
        ('app/db/schema.sql', 'app/db'),
        ('app/db/notebook_schema.sql', 'app/db'),
        ('app/db/child_schema.sql', 'app/db'),
        ('app/db/coverage_schema.sql', 'app/db'),
        ('app/db/action_schema.sql', 'app/db'),
        ('app/db/matter_schema.sql', 'app/db'),
        ('app/db/change_schema.sql', 'app/db'),
        ('app/db/knowledge_schema.sql', 'app/db'),
        ('app/core/knowledge/ontology.json', 'app/core/knowledge'),
        ('app/ui/assets', 'app/ui/assets'),
        (str(NOTICES), 'app/licenses'),
    ],
    # 선 아이콘(app/ui/icons.py)은 QtSvg로 그린다. try/except로 감싸 놓아
    # 빠져도 죽지는 않지만, 빠지면 아이콘이 통째로 기호로 물러선다.
    hiddenimports=['PySide6.QtSvg'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['rdflib', 'pyshacl', 'owlrl', 'html5rdf'],  # 그래프 표준 검증은 개발 전용
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='NunchiCoach',
    icon=str(Path(SPECPATH) / 'app' / 'ui' / 'assets' / 'logo.ico'),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='NunchiCoach',
)
shutil.copy2(NOTICES, Path(DISTPATH) / 'NunchiCoach' / notices.FILENAME)
