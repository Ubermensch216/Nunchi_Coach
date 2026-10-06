# 눈치코치 (Nunchi_Coach)

전임자가 참여할 수 없어도 온나라의 작성 문서와 남은 업무자료에서 업무를 복원하고, 후임자의 파악·수행 부담을 줄이는 Windows 업무 인수인계 도구입니다.

구축 기준은 [단계별 구현 계획서](docs/단계별_구축계획서_눈치코치_2026-10-06.md) 3.0입니다. 기존 문서 분석·업무·일정·질문 UI를 이식했고, 초기 품질 수정과 온나라 수집 엔진의 합성 실행 경로를 구현했습니다. 실제 기관 온나라 연결부는 아직 구현·검증하지 않았습니다.

## 현재 구현

- PySide6 데스크톱, SQLite/FTS5, 로컬 문서 파싱과 기존 Ollama 검색 기능.
- 보고서/감사 CSV의 원본 자료 경로·DB·하드링크 보호, 실패 시 이전 출력 유지.
- 답변의 표시용 인용번호 검증, 업무·연도 범위 밖 자료의 대체 방지.
- HWPX 중첩 문단의 중복 추출 제거와 section 숫자 순서 처리.
- ODT 문단·표·첫 행 문맥·반복/병합 정보 추출과 추출 상한 표시.
- 온나라 연결 계약, 합성 작성자 검색·페이지 순회, 본문/붙임 수집, 해시 검증·누락 표시·재개.
- 수집 자료를 기존 파서·문서 검색·청크 색인에 연결하는 개발용 실행 경로.

현재 온나라 수집 실행은 명시적으로 합성 자료를 사용합니다. 기관 API·브라우저 화면·인증을 임의로 가정한 실제 연결 기능은 포함하지 않습니다. 업무 복원 카드·수행 기록·인계 스냅샷 등 계획의 후속 기능은 개발 중입니다.

## 실행 환경

Python 3.14와 `requirements.lock`의 잠금 의존성이 기준입니다. `start.bat`은 로컬 `.venv`와 의존성을 준비하고 앱을 실행합니다. 오프라인 설치는 준비한 `wheelhouse`와 `NUNCHICOACH_OFFLINE=1` 설정을 사용합니다. 모델 없는 상태에서는 로컬 파싱·기본 검색을 사용할 수 있으며 AI 기능에는 별도 Ollama와 모델이 필요합니다.

새 개발 환경:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m app.main
```

이 장비에서 최초 검증에는 참조 프로젝트의 잠금 환경 `D:\Dev\docs_mate\.venv\Scripts\python.exe`를 사용했습니다. 참조 환경을 수정하거나 복사한 것은 아니며 배포에는 눈치코치 자체 환경을 마련해야 합니다.

## 온나라 합성 수집 실행

합성 문서 30건, 본문·붙임 90개를 생성합니다. 기본 작성자 ID에 속한 문서 27건·파일 81개를 수집하고 다른 ID의 동명이인을 제외합니다. 폴더는 서로 분리합니다.

```powershell
.\.venv\Scripts\python.exe -m app.tools.onnara_demo --make-fixture data/onnara-fixture --output data/onnara-collected --project data/onnara-project
```

같은 조건으로 중단 작업을 다시 실행하려면 기존 카탈로그를 지정합니다. 이미 검증된 파일은 재사용하고 손상된 로컬 파일은 다시 받습니다.

```powershell
.\.venv\Scripts\python.exe -m app.tools.onnara_demo --catalog data/onnara-fixture/catalog.json --output data/onnara-collected --project data/onnara-project
```

`manifest.json`에는 작성자·검색 범위·원 문서 ID·상태·본문/붙임 관계·파일 해시·성공/실패·완전성·합성 여부가 남습니다. `--project`는 수집 자료를 로컬 파서와 FTS에 등록합니다. 이 단계는 생성 AI 호출이나 전임자 확인 없이 실행됩니다.

## 검사

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m app.tools.lockfile --check
```

온나라 핵심 검사에는 페이지·동명이인·접근 거부·인증 만료 후 재개·파일 손상·해시 불일치·로그인 HTML·파싱/검색 연결이 포함됩니다. 모의 검사 통과를 실제 기관 연동 완료로 해석하지 않습니다.

## 개발 기록

- [구현 진행 기록](docs/구현진행_2026-10-06.md)
- [온나라 연결 프로필](docs/온나라_연결프로필.md)
- [참조 기준선](docs/참조_기준선.md)
- 기존 참조 프로젝트 설명은 `doc/REFERENCE_README.md`와 `doc/`에 보존했습니다.

원본 자료·수집 데이터·프로젝트 DB·출력·검사 임시자료는 Git에 포함하지 않습니다. 프로젝트 및 외부 구성요소의 배포 권리 결정은 제출 단계에서 별도로 확정해야 하며 이 이식 작업이 새로운 라이선스를 부여하지 않습니다.
