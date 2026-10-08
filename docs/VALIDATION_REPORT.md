# 검증 보고 — 개발지시서(2026-10-08) 대응 1차

- 기준 커밋 `28bc7acba119302fba8ebd1bda688a4f43f728e4` · 최종 커밋 **없음(미커밋 작업 트리)** — 이 환경에 git이 없다
- 실행일 2026-10-08 · 실행 주체: 개발 AI, 개발 PC 한 대
- 이 문서의 수치는 모두 **그 실행에서만** 유효하다. 합성 자료·모의 시험·이 PC의 결과이며 실제 기관 문서·원격 CI·다른 PC의 결과가 아니다.

## 1. 실행 환경

| 항목 | 값 |
|---|---|
| OS | Windows 11 Pro 10.0.22631 |
| Python | 3.14.7 (`.venv`), `python -m app.tools.lockfile --check` → "환경이 잠금 파일과 같습니다" |
| CPU · 메모리 · 그래픽 | Intel Core Ultra 5 225 · 15GB · Intel Graphics(내장) |
| Ollama | 0.40.1, `127.0.0.1:11434` |
| 생성 모델 | `gemma4:e2b` digest `b37049369adf` (4.27GB) |
| 임베딩 모델 | `bge-m3:latest` digest `790764642607` (1.08GB) |

## 2. 실행한 것과 하지 않은 것

| 검사 | 명령 | 결과 |
|---|---|---|
| 잠금 파일 대조 | `python -m app.tools.lockfile --check` | 통과 |
| 합성 표본 생성 | `python -m app.tools.make_fixtures` | 80건 |
| 전체 회귀 | `$env:QT_QPA_PLATFORM='offscreen'; $env:NUNCHICOACH_STRICT_SKIPS='1'; python -m pytest tests -q -p no:cacheprovider -rs` | **1246 통과 · 0 실패 · 4 건너뜀**, 530.9초, 종료 코드 0 (3절) |
| 업무 복원 자체 점검(소스 실행) | `python -m app.tools.selfcheck` | 통과 |
| 추적 금지 검사 | `python -m app.tools.repo_guard` | **검사 못 함(종료 코드 2)** — git 없음 |
| 질문 평가 | `python -m app.tools.rag_report --final\|--reworded\|--holdout\|--dev` | 4절 |
| PyInstaller 빌드 · 번들 `selftest` | — | **미실행** |
| 실제 모델 회귀(`NUNCHICOACH_LIVE_AI=1`) | — | **미실행** |
| GitHub Actions · GitLab CI | — | **미실행** (푸시하지 않음) |
| 시나리오 평가(`scenario_eval`) · 문장 추출 평가 | — | **미실행** (이번에 그 코드를 건드리지 않았다. 회귀 시험 `test_scenario_eval.py`는 통과) |

## 3. 회귀 시험

| 시점 | 통과 | 실패 | 건너뜀 | 비고 |
|---|---:|---:|---:|---|
| 지시서가 인용한 CI 실행(원격) | 1182 | 2 | 5 | 참고. 이번에 재현한 것이 아니다 |
| P0-01·P0-05 수정 직후, 표본 없이 | 1178 | 0 | 20 | 종료 코드 **1** — 새 건너뜀 게이트가 표본이 없어 빠진 16건을 실패로 잡았다. 표본을 만든 뒤 다시 실행 |
| P0-03 연결 직후 | 1236 | 0 | 4 | |
| 보완 도중 | 1243 | **1** | 4 | `test_verify.py::test_number_check_looks_only_at_the_sentences_own_cited_sources` — 아래 설명 |
| **최종(코드 고정 뒤)** | **1246** | **0** | **4** | |

건너뛴 4건(모두 허용 목록): 실제 모델 시험 3건(`test_ai.py` 2, `test_chunks_pipeline.py` 1), 실제 기관 문서 표본 1건(`test_quality_reports.py`).

**기존 시험의 계약을 바꾼 것 — 셋.** 실패를 지운 것이 아니라 계약이 바뀐 것이며, 바뀐 뒤의 계약을 같은 자리에서 단언한다.

1. `test_chunks_pipeline.py::test_chunks_stage_is_skipped_without_ollama` → `test_chunks_are_built_without_ollama_and_only_embedding_waits`. "모델이 없으면 조각 0개"에서 "조각·글자 검색은 생기고 임베딩만 보류"로. CI에서 실패하던 두 시험과 예전 계약은 동시에 참일 수 없었다(지시서 P0-01-1).
2. `test_verify.py::test_number_check_looks_only_at_the_sentences_own_cited_sources` → 둘로 나눔. "인용한 조각에 숫자가 없으면 무조건 버린다"에서 "그 숫자와 말을 가진 문서가 하나뿐이면 근거를 고쳐 잡고, 아니면 버린다"로. **검증을 한 군데 느슨하게 한 변경**이다. 이유와 위험은 KNOWN_LIMITATIONS 2절.
3. `test_generation.py`의 규칙 답변 2건: `generation == {"rule_answer": …}`를 `generation["rule_answer"] == …`로. 진단 기록에 `frame`이 더해졌기 때문이다.

CI에서 실패하던 두 시험(`test_collected_odt_and_attachments_reach_project_fts`, `test_answer_evidence_shows_the_official_source`)은 두 파일 전체를 죽은 주소의 Ollama로 고정한 조건에서 통과한다. **원격 러너에서 통과하는지는 푸시 뒤에 확인해야 한다.**

## 4. 질문 평가 (P0-03)

### 4.1 조건

- 평가 프로젝트: `python -m app.tools.rag_report --build-final <폴더>` — 합성 표본 + 시나리오 자료(세 업무) + 최종 검증 자료(네 업무)를 한 폴더에 넣고 분석했다. 파이프라인 집계로 문서 104건(완전 중복 8건), 조각 182개, 임베딩 182개. **세 문항 세트를 모두 이 한 프로젝트에서 쟀다.** 그래서 개발용 세트의 "수정 전" 수치가 `현재구현_및_검증현황.md`의 수치(다른 프로젝트)와 다르다.
- 문항마다 **1회**. 반복 실행은 하지 않았다.
- 최종 검증 문항: `tests/fixtures/rag_eval/final_2026-10-08.json` sha256 `ce6f2d71359d36b1ec7324433067f4f19f11ef2bc199c80919521ec865800161`, 자료 `final_2026-10-08_corpus.json` sha256 `67e03e66bb2c88f0dd917c49a966153c17818ac154ce89c9ae80c4531983da70`. 수정 전과 후의 실행이 같은 해시다.
- 절차: 문항·자료 작성 → 수정 전 측정 → **집계 수치만** 확인 → 개발용 세트(30·60·24)의 실패를 보며 수정 → 코드 고정 → 최종 문항 1회 → 그 뒤에 문항별 결과를 열었다. 이 결과를 보고 고친 코드는 없다.

### 4.2 최종 검증 45문항 (답해야 하는 33 · 유보해야 하는 12)

| 지표 | 수정 전 | 수정 후 | 권고 기준 |
|---|---:|---:|---|
| 정답률 (답해야 하는 문항 중 옳게 답함) | 18/33 **54.5%** | 23/33 **69.7%** | 90% 이상 — **미달** |
| 근거 없는 확정 (전체 중 없는 사실·다른 속성의 값을 답함) | 5/45 **11.1%** | 4/45 **8.9%** | 1% 미만 — **미달** |
| 보류 정밀도 (보류한 것 중 보류가 옳았음) | 8/20 40.0% | 10/17 58.8% | |
| 보류 재현율 (보류해야 하는 것 중 보류함) | 8/12 66.7% | 10/12 83.3% | |
| 근거 정확성 (답한 것 중 기대한 문서를 인용) | 20/21 95.2% | 25/26 96.2% | |
| 질문 적합성 (답한 것 중 묻는 것에 대한 답) | 20/25 80.0% | 24/28 85.7% | |
| 전체 통과 | 26/45 | 33/45 | |
| 응답 시간 | 중앙값 13.4초 · 최대 19.7초 | 중앙값 10.4초 · 최대 16.8초 | |

유형별 통과(수정 전 → 후): 속성 혼동 3/6 → 6/6 · 표의 칸 5/9 → 6/9 · 해·시점 5/7 → 6/7 · 다른 말 5/10 → 5/10 · 예정↔완료 1/4 → 2/4 · 바뀐 값 0/1 → 1/1 · 문서 속 지시문 1/1 → 1/1 · 자료 없음 3/4 → 4/4 · 그대로 읽기 3/3 → 2/3.

**판정: 완료 기준 미달. 질문 화면은 배포 보류.**

수정 후에 남은 12건:

| 갈래 | 건수 | 문항 |
|---|---:|---|
| 없는 사실·다른 속성의 값을 답함 | 4 | `f-train-planned`(계획 인원을 물었는데 수료 인원), `f-civil-road-carry`(이월을 물었는데 처리 건수를 함께 답함), `fw-civil-water-resp`(표에 없는 행을 "표 1에 나타나 있습니다"로 답함), `fw-event-attended`(참석 인원을 물었는데 초청 인원) |
| 옳은 답이 있는데 보류 — 초점 낱말이 문서에 없음(기존 검사) | 3 | `f-buy-toner-got`, `f-buy-vendor`, `f-event-host` |
| 옳은 답이 있는데 보류 — 문장 검증(기존 검사) | 3 | `f-civil-road-resp`, `f-civil-env-happy`, `f-event-2024-came` |
| 옳은 답이 있는데 보류 — **이번에 넣은 속성 검사** | 1 | `f-civil-rate`. 원인은 일부러 조사하지 않았다(이 세트로 고치지 않기 위해) |
| 답했지만 내용이 빠짐 | 1 | `f-event-place`("장소는 변동이 없습니다" — 변경 알림만 읽음) |

읽을 때: 수정 전에 통과했다가 수정 후에 실패한 문항이 둘 있다(`f-civil-rate`, `f-event-place`). 1회 실행이라 뒤의 것은 모델 출력의 변동일 수 있고, 앞의 것은 새 검사가 만든 과잉 보류다.

### 4.3 개발용 세트 (재고 고치는 데 쓴 문항 — 새 질문에서의 성능이 아니다)

| 세트 | 수정 전 | 수정 후 | 남은 실패 |
|---|---:|---:|---|
| 다른 말로 묻는 24문항 | 17/24 | 24/24 | — |
| 60문항 | 53/60 | 56/60 | 표의 숫자 아닌 칸 1, 근거 문서 불일치 1, **없는 사실을 답함 2**("누가 해?", "늦어진 부서는 어디야?" — 수정 전과 같은 두 문항) |
| 30문항 | 30/30 | **29/30** | `table-status` — 기존 검사(`_foreign_subject`)의 과잉 보류. 수정 전에는 통과했다 |

24문항의 수치는 이 문항들의 실패를 직접 보고 고친 결과다. 일반화 성능으로 읽으면 안 된다. 30문항은 한 문항 나빠졌다.

### 4.4 이번에 넣은 것이 실제로 걸린 횟수 (최종 문항, 수정 후)

답변 경로: 모델 답변 22 · 표 규칙 답변 6 · 초점 낱말 없음 6 · 모델이 답하지 않음 4 · 문장 검증 탈락 4 · 속성 불일치(신규) 3.

## 5. P0-01 확인 내용

- 모델 없이: 조각 생성, 조각 글자 검색(`chunk_fts`) 적재, 임베딩 0, `chunks_checked` 미설정, 단계 보고 "N조각 나눔 · 임베딩 보류 — (사유)".
- 뒤늦게 모델 준비: 조각 id·위치·본문 불변, 임베딩만 추가, `chunks_checked` 설정.
- 임베딩 배치 실패: 조각 유지, 다음 실행에서 빠진 것만(임베딩 호출 1회).
- 임베딩 모델 교체: 같은 조각에 새 모델 벡터로 교체, 두 모델의 벡터가 섞이지 않음.
- 문서 사이에서 중단: 반쪽 문서 없음, `PRAGMA integrity_check` ok.
- 온나라 합성 수집본: 모델 없이 문서 6건 → 조각 → 공식 출처가 붙은 인용.

모의 임베더·죽은 주소로 한 시험이다. 실제 Ollama가 도중에 꺼지는 상황, 프로세스 강제 종료는 해 보지 않았다.

## 6. 변경 파일

수정: `app/jobs/pipeline.py` · `app/db/repo.py` · `app/search/rag.py` · `app/search/verify.py` · `app/search/tablemath.py` · `app/ai/client.py` · `app/tools/rag_report.py` · `app/tools/selfcheck.py` · `tests/conftest.py` · `tests/test_chunks_pipeline.py` · `tests/test_onnara_collection.py` · `tests/test_provenance.py` · `tests/test_generation.py` · `tests/test_verify.py` · `tests/test_rag_report.py` · `.github/workflows/verify.yml` · `.gitlab-ci.yml` · `.gitignore` · `docs/현재구현_및_검증현황.md`(한 줄)

신규: `app/search/intent.py` · `app/tools/repo_guard.py` · `tests/test_intent.py` · `tests/test_repo_guard.py` · `tests/fixtures/rag_eval/final_2026-10-08.json` · `tests/fixtures/rag_eval/final_2026-10-08_corpus.json` · `docs/IMPLEMENTATION_PLAN.md` · `docs/VALIDATION_REPORT.md` · `docs/DATA_SECURITY_AUDIT.md` · `docs/KNOWN_LIMITATIONS.md` · `docs/REQUIREMENTS_TRACEABILITY.md`

데이터 마이그레이션: **없음.** 스키마를 바꾸지 않았다. 기존 프로젝트 DB는 그대로 열리고, 모델 없이 분석했던 프로젝트는 다음 분석에서 조각이 새로 생긴다(기존 조각·임베딩은 건드리지 않는다). `questions.filters`의 `generation`에 `frame`·`withheld`·`dropped` 같은 키가 더해진다 — 읽는 쪽은 없는 키를 견딘다.

## 7. 작업 결과 요약

- 기준 커밋 / 최종 커밋: `28bc7ac` / 없음(미커밋)
- 완료 요구사항: 없음 — `TESTED_CI` 이상에 이른 항목이 없다
- 부분 완료: P0-01(`TESTED_LOCAL`), P0-02(감사·검사 도구까지), P0-03(일부, 기준 미달), P0-05(1차)
- 외부 의존: P0-04(`BLOCKED_EXTERNAL`)
- 회귀 테스트: 통과 1246 / 실패 0 / 건너뜀 4
- CI URL 및 상태: 없음 — 푸시 전
- 실모델/실자료/별도 PC 시험 범위: 질문 평가만 이 PC의 실제 모델로 실행. 실자료·별도 PC 없음
- 해결된 주요 문제: 모델 없는 환경에서 조각이 만들어지지 않던 것, 서술어로 물은 속성의 오답("얼마로 정산됐어?"에 계약금액), 표의 칸을 다른 말로 물었을 때의 보류, 근거 번호 오기로 인한 보류, 건너뛴 시험이 녹색으로 끝나던 것
- 남은 리스크 및 다음 단계: KNOWN_LIMITATIONS. 다음은 ① 사용자: `.analysis_audit` 추적 해제·커밋·푸시 → CI 결과 확인 ② P0-03 잔여(초점 낱말 검사의 과잉 보류, 충돌 처리, 보류 안내)와 새 검증 세트 ③ P1-02
- **배포 가능 여부: 불가.** 근거 — 질문 화면이 최종 검증 문항에서 정답률 69.7%·근거 없는 확정 8.9%로 권고 기준에 못 미치고, 원격 CI·패키징·실자료 검증이 모두 미실행이다
