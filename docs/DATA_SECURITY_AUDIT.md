# 추적 파일·민감자료 감사 (P0-02)

- 기준 커밋: `28bc7acba119302fba8ebd1bda688a4f43f728e4` · 감사일: 2026-10-08
- 방법: 이 작업 환경에는 `git`이 없어 `.git/index`(v2)를 직접 읽어 추적 경로를 뽑았다. 파일 내용은 패턴의 **건수만** 셌고 이 문서에는 경로·유형·건수만 적는다.
- **하지 않은 것(미실행)**: 과거 커밋 이력의 감사. `git log`가 필요하다. 아래 4절의 명령으로 확인해야 한다.

## 1. 현재 HEAD가 추적하는 것

| 구분 | 파일 수 | 비고 |
|---|---:|---|
| 전체 추적 | 2,350 | |
| 추적하면 안 되는 것 | 1,981 | 전부 `.analysis_audit/` 아래. 디스크 기준 215.3MB |
| 남는 것 | 369 | `app` 126 · `tests` 169 · `docs` 32 · `doc` 31 · 루트 설정 11 |

`.analysis_audit/` 밖에는 추적 중인 DB·실행 산출물이 없다(`tests/fixtures/전임자업무_샘플/project.db`는 디스크에 있으나 `*.db` 규칙으로 추적되지 않는다).

`.analysis_audit/`의 유형별 수: `.db` 795 · `.xlsx` 318 · `.docx` 194 · `.py` 153 · `.csv` 143 · `.pdf` 80 · `.pptx` 66 · `.txt` 65 · `.json` 55 · `.md` 25 · `.hwpx` 22 · `.bak-v1` 16 · 그 밖.

자료 출처: 사용자 확인(2026-10-08) — `.analysis_audit/live-project`를 포함해 **합성 자료**다. 이 감사에서 내용을 열어 확인하지는 않았다.

## 2. 패턴 검사 (건수만)

검사한 패턴: 개인키 머리말, 토큰 꼴(GitHub·AWS·Slack·`sk-`), `password/secret/token = "…"` 대입, 주민등록번호 꼴, 휴대전화 번호 꼴, 전자우편 주소.

| 위치 | 패턴 | 파일 | 판정 |
|---|---|---|---|
| 남는 것 | 비밀값 대입 | `tests/test_log_privacy.py`, `tests/test_release_diagnostics.py` | 낮음 — 로그에 본문이 남지 않는지 보는 시험의 가짜 값 |
| 남는 것 | 전자우편 | `docs/단계별_구축계획서_눈치코치_2026-10-06.md` 1건 | 낮음 — 공개된 기관 대표 주소 |
| 남는 것 | 전자우편 | `app/ui/assets/logo.png` | 해당 없음 — 이진 파일의 우연한 일치 |
| `.analysis_audit/` | 비밀값 대입 | `docs_mate/tests/` 아래 같은 이름의 시험 2개 | 낮음 — 위와 같은 가짜 값 |
| `.analysis_audit/` | 전자우편 | 1개 파일 5건 | 낮음 — 로고 이미지 사본 |
| 전체 | 개인키 · 토큰 · 주민등록번호 · 휴대전화 | 0건 | — |

한계: 패턴 검사는 꼴이 정해진 것만 찾는다. 문서 본문 속의 이름·주소 같은 개인정보는 찾지 못한다. `.docx`·`.xlsx`·`.db` 안의 압축·이진 내용은 풀어서 보지 않았다.

## 3. 조치

| 조치 | 상태 |
|---|---|
| `.gitignore`에 `*.bak-v*`, `*.sqlite`, `*.sqlite3` 추가 | 완료 |
| 추적 금지 검사 `python -m app.tools.repo_guard` 신설, 시험 `tests/test_repo_guard.py` | 완료 (git이 없으면 종료 코드 2 = 검사 못 함. 통과로 치지 않는다) |
| CI에 `guard` 작업 추가(GitHub Actions·GitLab) | 완료 — **추적 해제 전에는 이 작업이 실패한다. 의도한 동작이다** |
| `.analysis_audit/` 추적 해제 | **사용자 실행 필요** (4절) |
| 과거 이력 감사 | **미실행** (4절) |
| 이력 재작성·강제 푸시 | 하지 않는다. 별도 승인 사항 |

## 4. git이 있는 환경에서 실행할 것

로컬 파일은 지우지 않고 추적만 푼다.

```bash
git rm -r --cached --quiet .analysis_audit
```

```bash
python -m app.tools.repo_guard
```

```bash
git commit -m "Untrack local audit outputs and project databases (P0-02)"
```

이력 감사 — 과거에 올라간 적이 있는 DB·자료 경로를 본다(경로만 나온다).

```bash
git log --all --diff-filter=A --name-only --pretty=format: -- "*.db" "*.db-wal" "*.db-shm" "data/*" "outputs/*" | sort -u
```

기록할 것: 추적 해제 전후의 `git ls-files | wc -l`(2,350 → 369 예상)과 `git count-objects -vH`.

## 5. 남는 위험

- 추적을 풀어도 **이력에는 약 215MB가 남는다.** 저장소를 새로 받는 사람은 여전히 그 크기를 받는다. 줄이려면 이력 재작성이 필요하고, 그것은 원격의 모든 복제본에 영향을 주므로 따로 결정해야 한다.
- 합성 자료라는 것은 사용자 확인에 근거한다. 저장소가 공개 상태이므로, 이력 감사에서 합성이 아닌 자료가 보이면 추적 해제보다 노출 대응이 먼저다.
- `.analysis_audit/` 안의 시험 스크립트(`audit_checks.py`, `live_audit.py`, `fresh_compare.py`)는 추적 해제 뒤 저장소에서 사라진다. 계속 쓸 것이면 `app/tools/`나 `tests/`로 옮겨 따로 올려야 한다.
