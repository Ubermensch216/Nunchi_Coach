# 요구사항 추적표

> **갱신일:** 2026-10-31

이 문서는 눈치코치(NunchiCoach)의 핵심 요구사항별 구현 소스와 회귀 시험 매핑을 관리합니다. `TESTED_LOCAL`은 개발 PC의 합성 자료·모의 시험 통과를 뜻하며, 실제 기관 온나라 서버 검증 전인 항목은 `실환경 미확인`(`BLOCKED_EXTERNAL`)과 구분해 표기합니다.

## 1. 기반·신뢰성·보안 (P0)

| ID | 요구사항 | 구현 소스 | 회귀 시험 | 상태 | 비고 |
|---|---|---|---|---|---|
| P0-01-1 | 조각 생성과 임베딩 분리 (모델 없이 FTS 동작) | `app/jobs/pipeline.py` `_chunks`, `app/db/repo.py` `documents_without_chunks` | `tests/test_chunks_pipeline.py` | `TESTED_LOCAL` | 모델이 없으면 조각·글자 검색 생성 후 임베딩만 보류 |
| P0-01-2 | 기존 조각·임베딩 보존 및 모델 교체 정합성 | `app/jobs/pipeline.py`, `app/db/repo.py` | `tests/test_chunks_pipeline.py`, `tests/test_index_integrity.py` | `TESTED_LOCAL` | 조각 ID·위치·본문 불변 확인 |
| P0-01-4 | 공식 출처가 조각·인용으로 연결 | `app/core/provenance.py`, `app/connectors/onnara/` | `tests/test_onnara_collection.py`, `tests/test_provenance.py`, `tests/test_saide_package.py` | `TESTED_LOCAL` | 모델 없는 조건에서도 공식 출처 인용 확인 |
| P0-02-4 | 금지 파일 추적 차단 (`repo_guard`) | `app/tools/repo_guard.py`, `.gitignore` | `tests/test_repo_guard.py` | `TESTED_LOCAL` | [DATA_SECURITY_AUDIT.md](DATA_SECURITY_AUDIT.md) 참고 |
| P0-03-1~3 | 질의 대상·속성·기간 분해 및 표 행/열/수량 규칙 검증 | `app/search/intent.py`, `app/search/tablemath.py`, `app/search/verify.py`, `app/search/rag.py` | `tests/test_intent.py`, `tests/test_generation.py`, `tests/test_verify.py` | `TESTED_LOCAL` | 상세 수치는 [현재구현_및_검증현황.md](현재구현_및_검증현황.md) 참고 |
| P0-03-8 | 문서 속 지시문(프롬프트 인젝션) 차단 | `app/search/intent.py` `strip_instructions` | `tests/test_intent.py`, `tests/test_generation.py` | `TESTED_LOCAL` | |
| P0-05-3~4 | 건너뜀 허용 목록(`ALLOWED_SKIPS`) 및 `selfcheck` 핵심 경로 검증 | `tests/conftest.py`, `app/tools/selfcheck.py` | `tests/test_selfcheck.py` | `TESTED_LOCAL` | |

## 2. 규제개혁 비교연구 및 업무 복원 품질 보강 (2026-10-14 ~ 2026-10-15)

| ID | 요구사항 | 구현 소스 | 회귀 시험 | 상태 | 비고 |
|---|---|---|---|---|---|
| R-P0 | 타 기관·지난 회차·AI 생성물·서식 빈칸의 업무 값 오염 차단 | `app/core/reconstruction.py`, `app/core/work_overview.py` | `tests/test_regulation_regressions.py` | `TESTED_LOCAL` | 실자료 스냅샷 사본 검증 포함 |
| R-P1-1 | 표 계산 문서의 건 단위(`matters`) 구조화 및 공문 자취(`matter_trail`), 건 확인 화면 | `app/core/matters.py`, `app/core/matter_trail.py`, `app/ui/views/matter_dialog.py` | `tests/test_matter_review.py`, `tests/test_matter_trail.py` | `TESTED_LOCAL` | |
| R-P1-2 | 구형 XLS(`olefile`), 보호 문서 표시, 압축 안 문서 개별 색인(`archive:3`) | `app/ingest/parsers/`, `app/ingest/members.py` | `tests/test_archive_members.py` | `TESTED_LOCAL` | |
| R-P2 | 긴 매뉴얼 구획 접기, 재분석 변경 추적, 품질 관문(`quality_gate`) | `app/ui/views/work_workspace.py`, `app/tools/quality_gate.py` | `tests/test_work_workspace.py`, `tests/test_quality_gate.py` | `TESTED_LOCAL` | |

## 3. 공문 기준 업무 분석 및 온나라·sAIde 연동 (2026-10-12 ~ 2026-10-30)

세부 설계와 상태표는 [공문 기준 업무 분석 계획서](공문기준_업무분석_계획서_2026-10-29.md)와 [온나라 연동 구현계획서](온나라_연동_구현계획서_2026-10-12.md)를 따른다.

| ID | 요구사항 | 구현 소스 | 회귀 시험 | 상태 | 비고 |
|---|---|---|---|---|---|
| P1 (S4·S5) | `onNara.sAIde` 문서등록대장 머리글·문서 ID 추출, 검색 폼 조건 채우기, 전체 페이지 순회, 50건 분할 내보내기 | `src/lib/onnara/document-list.ts`, `search-form.ts`, `inbox-pages.ts`, `download.ts` | `document-list.test.ts`, `search-form.test.ts`, `inbox-pages.test.ts`, `package.test.ts` | `TESTED_LOCAL` | 합성 DOM·계약 시험 50건 통과 (실환경 미확인 — G1~G5 대기) |
| P2 (B1~B5) | 로컬 브리지(`127.0.0.1`) 페어링·보유 문서 조회·꾸러미 알림·온보딩 수집 요청 큐 | `app/bridge/`, `app/ui/views/onboarding.py` · sAIde `src/lib/nunchi/` | `tests/test_bridge_server.py`, `test_bridge_pairing.py`, `test_bridge_packages.py`, `test_bridge_requests.py` | `TESTED_LOCAL` | |
| P3 | 공문(단위과제·제목 알맹이·인용 문서번호)으로 업무 골격 세우기 | `app/core/official.py`, `app/ai/discover.py` | `tests/test_official_discovery.py` | `TESTED_LOCAL` | 단건 공문+PC 파일 매칭 승격 및 `body_texts` 일괄 조회 포함 |
| P4a | 로컬 PC 파일 5단계 대응 (해시 > 문서번호 > 제목 알맹이 > 같은 폴더 > 본문 임베딩 유사도) | `app/ai/discover.py` (`_match_local`) | `tests/test_official_discovery.py`, `tests/test_official_comparison.py` | `TESTED_LOCAL` | |
| P4b | 사본 인계 폴더 내보내기 (`<업무>/<연도>/공문·PC자료/` + 안내문 + `manifest.json`) | `app/core/handover_folder.py`, `app/ui/views/handover.py` | `tests/test_handover_folder.py` | `TESTED_LOCAL` | 원본 불변성(`output_policy`) 준수 |
| P5 | 공문↔로컬 비교 고도화 (초안↔등록본 판 묶음, 공문 기둥 시간표·주기, 공백 3종, 값 충돌 출처 대비) | `app/core/versions.py`, `timeline.py`, `continuity.py`, `workstate.py`, `app/db/repo.py` | `tests/test_official_comparison.py` | `TESTED_LOCAL` | |
| P6 (V1) | 실제 기관 온나라 5~10건 종단 실증 | — | — | `BLOCKED_EXTERNAL` | G1~G5 기관 환경 확보 후 수행 |
