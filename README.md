# 눈치코치 (Nunchi_Coach)

전임자가 참여할 수 없어도 온나라의 작성 문서와 남은 업무자료에서 업무를 복원하고, 후임자의 파악·수행 부담을 줄이는 Windows 업무 인수인계 도구입니다.

구축 기준은 [단계별 구현 계획서](docs/단계별_구축계획서_눈치코치_2026-10-06.md) 3.0입니다. 기존 문서 분석·업무·일정·질문 UI를 이식했고, 초기 품질 수정과 온나라 수집 엔진의 합성 실행 경로를 구현했습니다. 실제 기관 온나라 연결부는 아직 구현·검증하지 않았습니다.

## 현재 구현

- PySide6 데스크톱, SQLite/FTS5, 로컬 문서 파싱과 기존 Ollama 검색 기능.
- 보고서/감사 CSV의 원본 자료 경로·DB·하드링크 보호, 실패 시 이전 출력 유지.
- 답변의 표시용 인용번호 검증, 업무·연도 범위 밖 자료의 대체 방지.
- HWPX 중첩 문단의 중복 추출 제거와 section 숫자 순서 처리, 표의 행·첫 행/머리글·단위·병합 보존.
- 긴 조각에서 질문과 관련된 문장을 골라 담는 증거창(생성·검증·근거 발췌 공통).
- 생성 호출의 종료 사유 구분과 계측, 사고 기능 모델의 빈 응답 방지.
- 문서 단위 기록의 원자화와 중단된 분석의 자동 복구.
- 업무 복원·수행의 데이터 계약(업무 항목·수행 단계·근거·정보 공백·수행 기록·검토 기록).
- 문서에 명시된 기한·금액·제출처·절차 등을 근거와 함께 업무 항목으로 올리는 규칙 기반 추출.
- 이름표 없이 문장으로만 적힌 내용(“준공검사 뒤 30일 이내에 정산한다”)을 로컬 모델이 골라 ‘추정’ 항목으로 덧붙이는 보조 추출. 모델은 문장 번호와 갈래만 답하고 값은 원문 문장 그대로이며, 필수 항목의 공백을 닫거나 단계를 막지 않습니다. 모델이 없으면 건너뜁니다.
- 내용만으로는 묶이지 않은 문서를 파일명이 같은 낱말로 시작하는 것끼리 ‘낮은 확신’의 업무 제안으로 묶어, 문서가 몇 건뿐인 업무에도 복원 카드가 생기게 하는 경로.
- 문서에 적힌 절차와 과거 수행 사례를 구분한 수행 단계, 마지막 기록과 ‘이후 상태 미상’ 정리.
- 빠졌거나 엇갈리는 정보마다 재검색 결과와 구체적인 다음 행동을 붙이고 해당 단계만 막는 공백 처리.
- 업무 상세의 ‘지금 할 일’ 탭: 다음 행동, 근거가 붙은 항목과 절차, 단계별 수행 기록, 확인할 정보, 결과 초안(Markdown) 저장. 전임자의 확인 없이 사용할 수 있습니다.
- 업무 복원본: 복원 결과와 수행 기록을 그 시점 그대로 저장한 판(분석 종료 시 자동 저장)과 그 판의 Markdown 내보내기.
- 전임자의 선택적 참여: 문서에 없는 내용·단계 보충, 누가 확인했는지의 이력, 확인하지 않은 항목을 그대로 밝히는 검토 인계본 발행. 전임자가 없어도 위 기능은 모두 동작합니다.
- 다른 PC로 이어받기: 복원본·인계본을 묶음 파일로 내보내고 가져오며, 해시와 자료 폴더 안의 위치로 원문을 다시 잇습니다. 묶음에 원문 파일은 들어가지 않습니다.
- ODT 문단·표·첫 행 문맥·반복/병합 정보 추출과 추출 상한 표시.
- 온나라 연결 계약, 합성 작성자 검색·페이지 순회, 본문/붙임 수집, 해시 검증·누락 표시·재개.
- 수집 자료를 기존 파서·문서 검색·청크 색인에 연결하는 개발용 실행 경로.
- 수집한 공문의 문서 ID·문서번호·상태·본문/붙임 관계를 프로젝트 문서에 연결하고 질문 답변의 근거에 표시.

현재 온나라 수집 실행은 명시적으로 합성 자료를 사용합니다. 기관 API·브라우저 화면·인증을 임의로 가정한 실제 연결 기능은 포함하지 않습니다. 

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
.\.venv\Scripts\python.exe -m app.tools.scenario_eval      # 전임자 없이 자료만으로 하는 업무 수행 평가
.\.venv\Scripts\python.exe -m app.tools.selfcheck          # 복원 → 복원본 → 묶음 → 이어받기 자체 점검
.\.venv\Scripts\python.exe -m app.tools.scenario_eval --model   # 로컬 모델로 문장에서 찾은 내용까지 넣은 평가
.\.venv\Scripts\python.exe -m app.tools.rag_report --project 이름 --dev --repeat 3   # 개발용 30문항 질문 평가
.\.venv\Scripts\python.exe -m app.tools.rag_report --project 이름 --holdout --repeat 3   # 따로 떼어 둔 60문항(이 문항으로 조정하지 않는다)
.\.venv\Scripts\python.exe -m app.tools.statement_eval --repeat 3   # 문장 추출의 정밀도·재현율(로컬 모델 필요)
.\.venv\Scripts\python.exe -m app.tools.notices --strict   # 오픈소스 고지문 만들기(본문이 빠진 라이선스가 있으면 실패)
```

시나리오 평가는 기본으로 모델을 쓰지 않으며 합성 자료 세 건에서 자동 복원 24/28(86%)로 계획 기준 90%에 못 미칩니다(종료 코드 1). `--model`을 주면 이 장비의 gemma4:e2b로 28/28이 나오지만, 이 자료에는 헷갈릴 문장이 거의 없어 정밀도를 말해 주지 않습니다. 놓친 항목과 한계는 단계 4 검증 결과에 있습니다.

## 실행물과 오픈소스 고지

```powershell
.\.venv\Scripts\python.exe -m PyInstaller NunchiCoach.spec
dist\NunchiCoach\NunchiCoach.exe selftest tests\fixtures\sample_tree
dist\NunchiCoach\NunchiCoach.exe licenses
```

빌드할 때마다 고지문(`THIRD_PARTY_NOTICES.txt`)을 지금 환경에서 다시 만들어 실행물 안과 실행 파일 옆에 넣습니다. 앱에서는 설정 → 시스템 → ‘오픈소스 고지’에서 봅니다. 실행물에 든 것과 빌드·시험 도구를 모두 적습니다.

- **PyMuPDF(및 MuPDF)** 는 교체하지 않고 **AGPL-3.0** 조건으로 씁니다(상용 라이선스 미사용). 그래서 실행물을 받은 사람이 이 프로그램의 대응 소스를 받을 수 있어야 하며, 고지문은 그 위치로 이 저장소를 적습니다. 저장소 주소가 바뀌면 `app/tools/notices.py`의 `SOURCE_URL`을 고칩니다.
- **PySide6/Qt** 는 LGPL-3.0 조건으로 쓰며 실행물 폴더에 동적 라이브러리로 따로 들어 있습니다.
- AGPL-3.0·LGPL-3.0·GPL-3.0 본문은 해당 배포판에 들어 있지 않아 `app/licenses/texts/`에 따로 두었고 고지문에 실립니다(SPDX 라이선스 목록 저장소에서 받은 본문). 이 파일이 빠지면 고지문에는 받는 주소만 적히고 빌드가 경고합니다.
- 모델과 Ollama는 실행물에 넣지 않습니다.

이 고지는 각 구성요소가 밝힌 표기를 옮긴 것이며 법률 검토가 아닙니다.

온나라 핵심 검사에는 페이지·동명이인·접근 거부·인증 만료 후 재개·파일 손상·해시 불일치·로그인 HTML·파싱/검색 연결이 포함됩니다. 모의 검사 통과를 실제 기관 연동 완료로 해석하지 않습니다.

## 개발 기록

- [구현 진행 기록](docs/구현진행_2026-10-06.md)
- [단계 4 검증 결과](docs/단계4_검증결과.md) — 시나리오 평가, 실제 모델 확인, 실행물, 라이선스 표기, 하지 못한 것
- [온나라 연결 프로필](docs/온나라_연결프로필.md)
- [참조 기준선](docs/참조_기준선.md)
- 기존 참조 프로젝트 설명은 `doc/REFERENCE_README.md`와 `doc/`에 보존했습니다.

원본 자료·수집 데이터·프로젝트 DB·출력·검사 임시자료는 Git에 포함하지 않습니다. 외부 구성요소는 위 ‘실행물과 오픈소스 고지’의 방침대로 고지합니다. 이 저장소 자체의 라이선스 파일(`LICENSE`)은 아직 두지 않았습니다 — AGPL-3.0 구성요소를 포함해 배포하려면 저장소의 라이선스 표기와 소스 공개 방식을 확정해야 합니다.
