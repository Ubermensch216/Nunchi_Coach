# 요구사항 추적표 — 개발지시서(2026-10-08)

기준 커밋 `28bc7ac` 위의 작업 트리(미커밋) · 2026-10-08. 상태 표기는 지시서 7.1을 따른다. `TESTED_LOCAL`은 이 PC의 합성 자료·모의 시험 통과를 뜻하며 `TESTED_CI`·`VALIDATED_REAL`이 아니다.

## P0

| ID | 요구 | 소스 | 시험 | 상태 | 증거·비고 |
|---|---|---|---|---|---|
| P0-01-1 | 조각 생성과 임베딩 분리 | `app/jobs/pipeline.py` `_chunks`, `app/db/repo.py` `documents_without_chunks` | `tests/test_chunks_pipeline.py::test_chunks_are_built_without_ollama_and_only_embedding_waits` | `TESTED_LOCAL` | 기존 시험 `test_chunks_stage_is_skipped_without_ollama`의 계약(모델 없으면 조각 0)을 뒤집었다 — 실패하던 두 시험과 동시에 참일 수 없었다 |
| P0-01-2 | 기존 조각·임베딩 보존 | 같은 곳 | `…::test_late_ollama_embeds_existing_chunks_without_changing_their_ids` | `TESTED_LOCAL` | 조각 id·위치·본문 불변 확인 |
| P0-01-3 | 모델 없음·뒤늦은 준비·일부 실패·중단·모델 교체에서의 정합성 | 같은 곳 | `…::test_embedding_failure_keeps_chunks_and_resumes_only_what_is_left`, `…::test_changing_the_embedding_model_refills_vectors_on_the_same_chunks`, `…::test_stopping_between_documents_leaves_no_half_built_document`, 기존 `tests/test_index_integrity.py` | `TESTED_LOCAL` | 강제 종료(프로세스 kill) 뒤의 재개는 기존 `test_crash_resume.py`가 스캔 단계까지만 본다 |
| P0-01-4 | 공식 출처가 조각·인용으로 연결 | `app/tools/onnara_demo.py`(변경 없음) | `tests/test_onnara_collection.py::test_collected_odt_and_attachments_reach_project_fts`, `tests/test_provenance.py::test_answer_evidence_shows_the_official_source` | `TESTED_LOCAL` | CI에서 실패하던 두 시험. 두 파일을 Ollama 없는 조건으로 고정했다 |
| P0-01-5 | CI에 Ollama를 깔아 숨기지 않음 | `.github/workflows/verify.yml` | — | `TESTED_LOCAL` | 러너에 모델을 설치하지 않는다. 원격 실행은 미확인 |
| P0-02-1 | 추적 파일·민감자료 탐지(경로·유형만) | — | — | `TESTED_LOCAL` (HEAD) / 이력은 미실행 | [DATA_SECURITY_AUDIT.md](DATA_SECURITY_AUDIT.md) |
| P0-02-2 | `.gitignore` 재점검, 추적 해제 | `.gitignore` | `tests/test_repo_guard.py::test_gitignore_covers_every_guarded_folder` | `IMPLEMENTING` | `git rm --cached`는 사용자 실행 대기 |
| P0-02-4 | 금지 파일이 추적되면 CI 실패 | `app/tools/repo_guard.py`, CI `guard` 작업 | `tests/test_repo_guard.py` (5건) | `TESTED_LOCAL` | git이 없으면 종료 코드 2(검사 못 함) |
| P0-02-5 | 이력 재작성·강제 푸시 금지 | — | — | 준수 | 하지 않았다 |
| P0-03-1 | 질의를 대상·속성·기간·의도로 분해 | `app/search/intent.py` `frame` | `tests/test_intent.py::test_the_asked_attribute_is_read_from_the_noun_or_the_predicate` 외 | `TESTED_LOCAL` | 범위(scope)는 기존 `query.py` |
| P0-03-2 | 근거가 묻는 대상의 그 속성을 뒷받침하는지 | `intent.mismatch`·`confirmed`, `app/search/rag.py` `_wrong_field`·`_subject_unknown` | `tests/test_intent.py`(계약금액↔정산액, 기한↔실제 일자, 계획↔실적), `tests/test_generation.py::test_the_contract_amount_is_not_given_for_a_settlement_asked_with_a_verb`, `…::test_a_subject_the_material_never_names_is_withheld_without_asking_the_model` | `TESTED_LOCAL` | 값이 이름표·표 칸에 묶여 있을 때만 판정한다 |
| P0-03-3 | 표의 행·열·문서·연도·단위 일치, 규칙 계산 | `app/search/tablemath.py` `count`, `rag._named_table_rows` | `tests/test_generation.py::test_a_table_cell_asked_in_other_words_is_still_read_by_rule`, `…::test_the_table_row_the_question_names_is_brought_in_with_its_document`, `tests/test_intent.py::test_a_table_value_must_sit_in_the_asked_column` | `TESTED_LOCAL` | 단위는 기존 검사(`verify._unsupported_units`) 그대로 |
| P0-03-4 | 현행/과거·초안/승인본 충돌 시 양쪽 제시·보류 | — | — | `NOT_STARTED` | KNOWN_LIMITATIONS 2절 |
| P0-03-5 | 동의어 지원 + 역방향 오답 시험 | `intent.SAME_THING`·`_NATIVE_VERBS`, `tablemath._MODIFIER` | `tests/test_intent.py::test_synonym_groups_do_not_swallow_other_attributes`, `test_generation.py`의 '삭감된 예산' | `TESTED_LOCAL` | 묶음을 최종 검증 문항에 맞춰 늘리지 않았다 |
| P0-03-6 | 질문 적합성 검사 분리, 실패 사유의 구조적 진단 | `rag.ask`의 `generation["withheld"]`·`dropped`·`frame` | `test_generation.py`의 `generation["withheld"] == "field_mismatch"`, 진단에 본문 없음 단언 | `TESTED_LOCAL` | 화면 문구는 그대로 |
| P0-03-7 | 보류 시 필요한 자료·확인 행동 제안 | — | — | `NOT_STARTED` | |
| P0-03-8 | 문서 속 지시문을 명령으로 취급하지 않음 | `intent.strip_instructions`, `rag.ask` | `tests/test_intent.py::test_instructions_written_inside_a_document_are_not_evidence`, `…::test_ordinary_official_sentences_are_kept`, `tests/test_generation.py::test_an_instruction_written_inside_a_document_is_neither_followed_nor_cited` | `TESTED_LOCAL` | 꼴이 정해진 지시문만. 프롬프트의 신뢰 경계 문장은 넣지 않았다 |
| P0-03 | 같은 입력에 답이 갈림 | `app/ai/client.py` `seed`, `rag._ask_again` | `tests/test_generation.py::test_generation_is_seeded_and_a_second_ask_uses_another_seed` | `TESTED_LOCAL` | 씨앗 고정의 효과(반복 실행 일치율)는 재지 않았다 |
| P0-03 | 옳은 답의 과잉 보류(근거 번호 오기) | `app/search/verify.py` `_only_supporting_sources` | `tests/test_generation.py::test_a_right_sentence_with_the_wrong_source_number_is_moved_to_the_document_that_says_it`, `…::test_a_number_found_in_two_documents_is_not_moved_to_either` | `TESTED_LOCAL` | |
| P0-03 | 독립 평가세트, 지표 분리 | `tests/fixtures/rag_eval/final_2026-10-08*.json`, `app/tools/rag_report.py` `metrics`·`--final`·`--build-final` | `tests/test_rag_report.py`(4건 추가) | `TESTED_LOCAL` | **완료 기준(정답률 90%·근거 없는 확정 1% 미만) 달성 여부는 VALIDATION_REPORT 4절** |
| P0-04 | 온나라 실연계 | — | — | `BLOCKED_EXTERNAL` | 기관 사양·승인 환경 없음 |
| P0-05-1 | Python 3.14 + lock 고정, `lockfile --check` | CI 두 파일 | — | `TESTED_LOCAL` | 이 PC에서 `환경이 잠금 파일과 같습니다`. 러너에서는 미확인 |
| P0-05-2 | CI 단계 구분 | CI 두 파일(`guard`·`test`·`package`) | — | `IMPLEMENTING` | 다섯 갈래 중 로직·UI·AI 모의는 한 작업. 실모델은 수동 |
| P0-05-3 | 이유 없는 skip·xfail 금지 | `tests/conftest.py` `ALLOWED_SKIPS` | 실제 실행으로 확인: 표본이 없던 첫 실행에서 16건을 잡아 종료 코드 1 | `TESTED_LOCAL` | 게이트 자체의 단위 시험은 없다 |
| P0-05-4 | selftest가 핵심 경로 검증 | `app/tools/selfcheck.py`(조각·글자 검색 추가) | `tests/test_selfcheck.py` | `TESTED_LOCAL` | 번들 실행물로는 미실행 |
| P0-05-5 | 회귀 시험 보강(원본 불변·인젝션 등) | — | 인젝션만 추가 | `IMPLEMENTING` | 원본 해시 불변·오프라인 실행은 기존 시험과의 대조부터 남음 |
| P0-05-6 | 러너 로그와 개발 PC 기록 분리 | VALIDATION_REPORT | — | `TESTED_LOCAL` | 러너 기록은 아직 없다 |

## P1 · P2

| ID | 상태 | 비고 |
|---|---|---|
| P1-01 ~ P1-08 | `NOT_STARTED` | |
| P1-09 | `NOT_STARTED` | 모델 없을 때의 FTS 전용 검색 폴백은 P0-01로 재료(조각 글자 검색)만 준비됐다 |
| P2-01 ~ P2-06 | `DEFERRED` | P0·P1 게이트 뒤 |
