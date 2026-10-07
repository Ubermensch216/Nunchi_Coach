# 화면 캡처 — 촬영 조건

촬영일: **2026-10-07** · 소스 기준: 커밋 `cca4444` · 루트 [README](../../README.md)(사용 설명서)가 이 폴더의 이미지를 씁니다.

## 촬영 조건

- 현재 소스의 `MainWindow`·대화상자를 Windows에서 실제 PySide6 위젯으로 그려 `QWidget.grab()`으로 저장했습니다. 다시 그린 시안이나 생성 이미지가 아닙니다. 창은 화면에 띄우지 않고 그렸습니다(`WA_DontShowOnScreen`).
- 자료는 **합성 자료 90건**입니다 — `app.tools.make_fixtures`의 합성 표본 77건과 `tests/fixtures/scenarios/corpus.json`의 시나리오 자료 13건. 실제 기관 자료·개인정보는 없습니다.
- 빈 프로젝트에 이 폴더만 연결하고 분석 파이프라인을 처음부터 끝까지 한 번 돌렸습니다. 사람이 확인·수정한 것은 없습니다. 그래서 화면의 `인수인계 0%`, `확인 필요`, `자료에서 추정`은 연출이 아니라 그 상태 그대로입니다.
- 분석 결과: 문서 90건(완전 중복 8건), 업무 9개(그중 2개는 파일명으로 묶은 낮은 확신의 제안), 배정 87건·미분류 3건, 업무 항목 14개, 문서에 적힌 절차 8단계, 문장에서 찾은 내용 4건, 확인할 공백 35건, 질문용 조각 154개. 분석 시간 약 169초.
- 모델은 이 PC의 Ollama에 설치된 것을 썼습니다 — 생성 모델은 gemma4 계열 5.1B Q4_K_M(이 PC에는 `gemma4:e2b`가 아닌 다른 이름으로 등록되어 있어 설정 화면에 그 이름이 보입니다), 검색 모델은 `bge-m3`. 업무 이름·설명과 질문의 답은 모델이 만든 것이라 실행마다 달라질 수 있습니다.
- 질문 화면의 대기·답변·보류는 실제 실행 결과입니다. 답을 주입하지 않았습니다. 촬영 때 물은 다섯 질문 가운데 네 개는 답했고 하나(자료에 없는 내용)는 보류했습니다.
- 주 화면 1440×960, 자료 폴더 연결 화면 1120×720, 대화상자는 각자의 크기. 글자 크기 ‘보통’, 밝은 테마(23번만 어두운 테마). 기준일은 촬영일(2026-10-07)입니다.
- 일부 화면에 합성 자료의 로컬 경로(`D:\Dev\Nunchi_Coach\data\manual-demo\…`)가 보입니다.

## 이미지 목록

| 파일 | 화면 |
|---|---|
| [01-launcher.png](01-launcher.png) | 시작 화면 — 인수인계 선택 |
| [02-onboarding.png](02-onboarding.png) | 자료 폴더 연결(빈 인수인계) |
| [03-tasks.png](03-tasks.png) | 업무 목록 |
| [04-work-next-actions.png](04-work-next-actions.png) | 업무 상세 › 지금 할 일 — 다음 행동, 해야 할 일 |
| [05-work-evidence.png](05-work-evidence.png) | 지금 할 일 — 근거 서랍 |
| [06-work-steps.png](06-work-steps.png) | 지금 할 일 — 처리 순서, 과거 수행 사례 |
| [07-work-gaps.png](07-work-gaps.png) | 지금 할 일 — 확인이 필요한 정보 |
| [08-work-statements.png](08-work-statements.png) | 지금 할 일 — 문장에서 찾은 내용(파일명으로 묶은 업무 ‘보조금’) |
| [09-task-reading.png](09-task-reading.png) | 업무 상세 › 먼저 읽을 문서 |
| [10-task-when.png](10-task-when.png) | 업무 상세 › 언제 하는 일인가 |
| [11-task-how.png](11-task-how.png) | 업무 상세 › 어떻게 처리했나 |
| [12-calendar-year.png](12-calendar-year.png) | 일정 › 연간 패턴 |
| [13-calendar-now.png](13-calendar-now.png) | 일정 › 지금 챙길 일 |
| [14-calendar-review.png](14-calendar-review.png) | 일정 › 확인 필요 |
| [15-documents.png](15-documents.png) | 문서 — 목록과 선택한 문서의 상세 |
| [16-ask-waiting.png](16-ask-waiting.png) | 질문 — 답을 만드는 중 |
| [17-ask-answer.png](17-ask-answer.png) | 질문 — 답변과 근거 원문 |
| [18-ask-withheld.png](18-ask-withheld.png) | 질문 — 답변 보류 |
| [19-snapshots.png](19-snapshots.png) | 업무 복원본 대화상자 |
| [20-settings-appearance.png](20-settings-appearance.png) | 설정 › 화면 |
| [21-settings-sources.png](21-settings-sources.png) | 설정 › 자료 폴더 |
| [22-settings-system.png](22-settings-system.png) | 설정 › 시스템 |
| [23-tasks-dark.png](23-tasks-dark.png) | 어두운 테마의 업무 목록 |

## 촬영하지 않은 것

- **분석 상태 대화상자** — 단계별 결과는 그 창을 띄운 실행에서 돈 분석만 보여 줍니다. 촬영은 분석을 마친 프로젝트를 다시 열어서 했으므로 모든 단계가 ‘아직 시작하지 않았습니다’로 나와 싣지 않았습니다.
- 온나라 수집 — 앱에 화면이 없습니다.
- 전임자 검토 인계본 발행, 묶음 가져오기 결과 — 대화상자 확인 창이라 싣지 않았습니다.

## 촬영 중 본 것

- 업무 복원본 대화상자의 목록은 저장 시각을 이 PC의 시각(23:16)으로, 아래 요약은 같은 판을 세계 표준시(14:16:27)로 적습니다. 화면 19번에 그대로 보입니다. 고치지 않았습니다.

## 다시 찍을 때

1. 합성 자료로 새 프로젝트를 만든다. 실제 업무 프로젝트를 쓰지 않는다.
2. 분석이 끝난 것과 모델 상태를 확인하고, 사람의 입력 없이 찍거나 입력한 것을 이 문서에 적는다.
3. 화면을 바꾸면 이 목록, 촬영일·소스 기준, 루트 README의 설명을 함께 고친다.

캡처는 화면이 이렇게 생겼다는 기록입니다. 실제 사용자의 사용성, 기관 자료에서의 품질, 저사양 PC의 성능을 확인했다는 뜻이 아닙니다. 2026-09-15의 이전 캡처는 [`doc/screenshots/`](../../doc/screenshots/README.md)에 그대로 있습니다.
