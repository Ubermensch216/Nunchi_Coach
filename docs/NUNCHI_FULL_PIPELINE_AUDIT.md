# 전임자 업무 복원 파이프라인 감사·개선 기록

기준일: 2026-10-10 (Asia/Seoul). 기준 커밋: `73145c2921a2693d2dd6b08d9b93c42d475774a6`, `main`.
착수 시 `git status --short`는 비어 있었다. 직전 AI의 `58e24b5`, `73145c2` 변경과
`NOTEBOOK_RAG_STAGE01_2026-10-09.md`, `NOTEBOOK_RAG_STAGE02_2026-10-10.md`를 먼저 검토했다.
작업 도중 대상 파일의 SHA-256을 대조한 뒤에만 변경을 적용했다. 커밋·푸시하지 않았다.

재개 기준은 `09de9c63b35d1089ed1d89c8148275a5f4646f76`이다. 중단 사이 다른 작업이 앞선 변경을
`211b7eb`에 커밋했고, `09de9c6`에서 문서 역할·연도·판본 및 실행 엔진(v10)을 추가했다.
재개 당시 `core/discovery_index.py`의 soft-IDF 개선과 대응 시험이 미커밋 상태였다.
현재 파일에 준비한 변경만 병합했고 새 스키마·파이프라인 단계·표 조각 가중치를 보존했다.

## 기존 작업 중 유지한 것

- 부모·자식 검색, DOCX 긴 표·제목 문맥, HWPX/XLSX 구조 메타데이터.
- `core/reindex.py`의 준비·검증·원자적 전환·아카이브·롤백.
- `QueryScope`의 연도·업무·선택 문서 범위와 인용 검증.
- `output_policy.py`의 원본/자료원/DB 쓰기 차단과 원자적 내보내기.
- 본문 근거를 가진 항목·명시 절차와 과거 수행 사례의 구분, 사용자 교정·수행 기록 보존.

`.analysis_audit/audit-checks-results.json`은 2026-10-06의 기록이다. 원본 MD/CSV 덮어쓰기,
강제 검색 범위 이탈, 인용 숫자 오인 등을 현재 결함으로 재인용하지 않았다.
착수 시 관련 안전성·검색·재색인·발견·갱신 시험 **94개 통과**를 확인했다.
전체 과거 감사 스크립트를 실제 사용자 자료에 재실행한 것은 아니다.

## 실행 흐름

이전: 조사 → 해시 → 파싱 → 날짜 → 대표 벡터 → 업무 발견 → 주기 → 과거 순서 → 복원 → 조각 색인.

현재: 조사 → 해시 → 파싱 → 날짜 → 대표 벡터 → **조각 색인** → 업무 발견 → 문서 갈래 확인 → 주기 → 과거 순서 → 복원.

`app/jobs/pipeline.py:Pipeline.run`이 순서를 제어한다. `Pipeline._chunks`는 기존 재색인 경로를
그대로 호출하고 부모·자식 임베딩을 재사용한다. `core/discovery_index.py:load`는 모든 부모
조각 벡터가 준비된 문서에 한해 전체 조각을 후보 벡터에 반영한다. 미완성 문서는 기존 대표
벡터를 사용하고 `document_prefix` 근거로 명시한다. 전체 색인과 모델의 본문 검토를 구분한다.

## 확인·수정한 항목

| 구분 | 이전 구현/위험 | 변경 및 증거 |
|---|---|---|
| 구현 확인 | 조사 제외 항목이 `_walk`에서 조용히 사라짐 | `scan_runs`, `scan_entries`에 파일/디렉터리·제외/오류 사유 기록. `tests/test_pipeline_coverage.py` |
| 반례 검증 | 같은 크기·수정시각의 내용 변경을 기본 스캔이 놓침 | 선택적 `scan_verify_hash=1`. 기본의 빠른 조사 정책은 유지 |
| 반례 검증 | 파서 갱신 후 재추출 실패 시 이전 색인이 남을 수 있음 | 성공/실패 모두 기존 자동 추출 산출물을 무효화. 실패 이유 저장 |
| 반례 검증 | 부모 임베딩 실패·자식 완료 조합에서 `chunks_checked`가 설정될 수 있음 | 부모·자식 잔여/오류/취소/재색인 대기를 모두 확인 |
| 구현 확인 | 단계 처리 건수만으로 성공 표시 | `StageReport.state`, 단계 결과 영구 저장, 상태 화면의 실패·보류·중단 표시 |
| 반례 검증 | 동일한 앞 1,200자 때문에 서로 다른 후반 업무가 합쳐짐 | 전체 조각 벡터와 중복 본문 빈도 가중치 사용. 제어 벡터 및 로컬 bge-m3 반례 |
| 반례 검증 | 1~2건으로 존재하는 문서 업무가 목록에서 사라짐 | 본문 조각이 있는 미배정 문서를 낮은 확신의 개별 후보로 노출 |
| 반례 검증 | `coherent=false`가 파일명 대체 이름으로 배정됨 | `discovery_holds`에 보류. 기존 업무로 억지 배정하지 않음 |
| 반례 검증 | 동일 업무명이 자동 병합 키로 사용됨 | 서로 다른 군집은 고유 이름으로 유지, 기존 사용자 병합 기능 사용 |
| 반례 검증 | 취소 시 이전 제안이 먼저 삭제될 가능성 | 검토 후 짧은 트랜잭션으로 교체. 취소·검토 중 교정 변경 시 이전 결과 유지 |
| 신규 보완 | 후보 생성 근거와 모델 입력 범위가 기록되지 않음 | `discovery_evidence`: 문서 해시·조각·위치·가중치·모델·색인 버전·발췌 제공 여부 |

원본 근거의 대표 구현 위치는 `app/ui/views/evidence.py:for_task`,
`app/core/coverage.py:snapshot`, `app/ai/discover.py:_save_discovery`이다.

재개 후 확인한 코드 위치: `app/jobs/pipeline.py:147`(단계 기록), `:219`(재파싱),
`:665`(조각 준비), `app/ai/discover.py:66`(검토 준비), `:134`(원자적 저장),
`app/core/discovery_index.py:40`(전체 조각 후보), `:129`(발췌),
`app/core/coverage.py:73`(집계), `app/ui/views/evidence.py:104`(실제 분류 근거),
`app/db/migrations.py:439`(v9), `:448`(후속 v10), `app/core/clustering.py:177`(메모리 감소).
전체 본문 조각이 후보 생성에 참여했다는 기록은 AI가 모든 본문을 검토했다는 뜻이 아니다.
작명/일치성 검토는 최대 6문서의 앞·중간·끝 조각에서 예산 안의 발췌만 받는다.

## 단계별 판정

| 계획 | 이번 상태 | 다음 완료 조건 |
|---|---|---|
| 0 기준선·평가 정의 | 기준선 확보, 반례 평가 구현 | 실제 업무 단위·대표 평가셋·독립 정답 확정 |
| 1 안전성·검색 | 기존 개선 유지, 회귀 검증 | Windows 경로 경합 등 플랫폼별 추가 실증 |
| 2 처리 범위 | 조사 기록·진단 API/CLI/UI 구현 | 형식별 실제 생략 페이지·셀·표 구조 계측 확대 |
| 3 전체 본문 | 기존 조각 재사용 및 발견 순서 연결 | 대규모 자료에서 지연·메모리·복구 검증 |
| 4 업무 발견 | 전체 조각 후보·소수 문서·보류·근거 연결 구현 | 다중 행정 신호·다중 업무 문서·확신 보정 평가 |
| 5 사실·절차 | 기존 구현 보존 및 회귀 검증 | 실제 법령 시점·상충 날짜·도메인별 정답 평가 |
| 6 사람 검토 | 기존 UI 활용, 분류 근거 연결·동시 수정 보호 | 기관 사용자 검토 흐름 실증 |
| 7 운영 | 회귀·합성 실험·승격 검증 | 1만 파일 성능·배포 번들·기관 파일럿 |

전체 계획 완료나 95% 목표 달성을 선언하지 않는다. 상세 미완료 조건은
[잔여 위험](NUNCHI_REMAINING_RISKS.md)에 기록했다.

## 재현 명령과 실행 기록

저장소 루트에서 프로젝트 `.venv\Scripts\python.exe`를 사용한다. UI 시험은
`QT_QPA_PLATFORM=offscreen`, 출력은 `PYTHONUTF8=1`이다.

```powershell
.venv\Scripts\python.exe -m pytest -q -rs --junitxml=.test_runs/nunchi-full-regression-20261010.xml
.venv\Scripts\python.exe -m app.tools.discovery_eval --output-dir .test_runs/discovery-new-run
.venv\Scripts\python.exe -m app.tools.discovery_eval --live --output-dir .test_runs/discovery-live-new-run
.venv\Scripts\python.exe -m app.tools.coverage PATH_TO_PROJECT_DB --output PATH_TO_REPORT_JSON
```

평가 출력 디렉터리는 존재하지 않는 새 경로를 사용한다. 기관 원본·기존 프로젝트를 평가
입력으로 요구하지 않으며, 기본 평가는 제어 벡터를 쓴다. `--live`도 생성한 합성 본문만
로컬 Ollama에 보낸다. 결과 로그는 `.test_runs/nunchi-*`에 저장한다.

최종 시험 집계와 게이트 표는 [변경 기록](NUNCHI_ARCHITECTURE_CHANGELOG.md)에 함께 기록한다.
