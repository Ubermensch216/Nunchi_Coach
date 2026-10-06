-- 눈치코치 스키마 v5
--
-- 설계 원칙
--  1. 원본 파일 본체는 DB에 넣지 않는다. 경로와 분석 결과만 저장한다.
--  2. 시점(document_dates)은 후보를 지우지 않는다. 채택 결과와 근거를 분리
--     보관해야 "무엇을 근거로 2024년이라 했는지"를 보여줄 수 있고, 추정 규칙을
--     바꿔도 재파싱 없이 재계산할 수 있다.
--  3. AI 결과는 status를 갖는다. 사람이 고친 값은 재분석이 덮어쓰지 않는다.
--  4. 주기(task_cycles)와 순서(task_steps)는 실체화한다. 매번 계산하지 않고
--     저장해야 화면이 빠르고, 무엇보다 사용자 교정이 남는다.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ── 자료원 ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sources (
    id        INTEGER PRIMARY KEY,
    path      TEXT NOT NULL UNIQUE,
    kind      TEXT NOT NULL DEFAULT 'local',   -- local | removable | unc | onnara
    excludes  TEXT NOT NULL DEFAULT '',        -- 줄바꿈 구분 패턴
    added_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── 문서 ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY,
    source_id      INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    path           TEXT NOT NULL UNIQUE,
    filename       TEXT NOT NULL,
    ext            TEXT NOT NULL,
    size           INTEGER NOT NULL DEFAULT 0,
    fs_mtime       TEXT,
    fs_ctime       TEXT,
    hash           TEXT,                       -- SHA-256, 완전 중복 판정용
    author         TEXT,
    doc_title      TEXT,                       -- 문서 내부 제목 속성
    meta_created   TEXT,                       -- 문서 속성의 작성일 (시점 추정 근거 ③)
    meta_modified  TEXT,

    -- 채택된 시점 (document_dates에서 고른 결과)
    eff_date       TEXT,                       -- YYYY-MM-DD
    eff_date_kind  TEXT,                       -- body|filename|meta|folder|fs
    eff_precision  TEXT,                       -- day|month|year
    eff_year       INTEGER,
    eff_month      INTEGER,
    -- v2: 사람이 고른 시점은 재분석이 덮지 않는다. 후보(document_dates)는
    -- 그대로 두고 '누가 골랐는가'만 기록한다.
    date_decided_by TEXT NOT NULL DEFAULT 'ai',   -- ai | user

    parse_status   TEXT NOT NULL DEFAULT 'pending',
    parse_error    TEXT,
    parse_note     TEXT,
    parser         TEXT,
    parser_version TEXT,
    char_count     INTEGER NOT NULL DEFAULT 0,

    analysis_status TEXT NOT NULL DEFAULT 'pending',
    missing_since   TEXT,                      -- 원본이 사라진 시각 (기록은 보존)
    first_seen      TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_documents_hash   ON documents(hash);
CREATE INDEX IF NOT EXISTS idx_documents_ext    ON documents(ext);
CREATE INDEX IF NOT EXISTS idx_documents_year   ON documents(eff_year, eff_month);
CREATE INDEX IF NOT EXISTS idx_documents_parse  ON documents(parse_status);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id);

-- 시점 후보. 하나의 문서가 여러 근거를 가질 수 있다 (ING-003).
CREATE TABLE IF NOT EXISTS document_dates (
    doc_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,     -- body | filename | meta | folder | fs
    value     TEXT NOT NULL,     -- YYYY-MM-DD (미상 부분은 01로 채우고 precision으로 구분)
    precision TEXT NOT NULL,     -- day | month | year
    raw       TEXT,              -- 판정 근거가 된 원문 ("2024. 9. 12.")
    locator   TEXT,              -- 어느 조각에서 나왔는지 ("3쪽")
    PRIMARY KEY (doc_id, kind, value)
);

-- 본문 조각. locator는 근거 표시(RAG 출처)에 그대로 쓰인다 (PAR-002).
CREATE TABLE IF NOT EXISTS document_sections (
    id      INTEGER PRIMARY KEY,
    doc_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    kind    TEXT NOT NULL,       -- paragraph | table | page | sheet | slide | note | preview
    ordinal INTEGER NOT NULL,
    locator TEXT NOT NULL,
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sections_doc ON document_sections(doc_id, ordinal);

-- ── 통합 검색 ───────────────────────────────────────────────────────
-- 토크나이저는 trigram을 쓴다. 실측 결과 unicode61은 "행정사무감사"에서
-- "사무감사"를 찾지 못했고 trigram은 찾았다. 다만 trigram은 3글자 미만
-- 질의를 처리하지 못하므로, 2글자 이하는 LIKE로 폴백한다(search/fts.py).
CREATE TABLE IF NOT EXISTS document_index (
    doc_id   INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    filename TEXT NOT NULL DEFAULT '',
    path     TEXT NOT NULL DEFAULT '',
    author   TEXT NOT NULL DEFAULT '',
    body     TEXT NOT NULL DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
    filename, path, author, body,
    content='document_index',
    content_rowid='doc_id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS document_index_ai AFTER INSERT ON document_index BEGIN
    INSERT INTO document_fts(rowid, filename, path, author, body)
    VALUES (new.doc_id, new.filename, new.path, new.author, new.body);
END;
CREATE TRIGGER IF NOT EXISTS document_index_ad AFTER DELETE ON document_index BEGIN
    INSERT INTO document_fts(document_fts, rowid, filename, path, author, body)
    VALUES ('delete', old.doc_id, old.filename, old.path, old.author, old.body);
END;
CREATE TRIGGER IF NOT EXISTS document_index_au AFTER UPDATE ON document_index BEGIN
    INSERT INTO document_fts(document_fts, rowid, filename, path, author, body)
    VALUES ('delete', old.doc_id, old.filename, old.path, old.author, old.body);
    INSERT INTO document_fts(rowid, filename, path, author, body)
    VALUES (new.doc_id, new.filename, new.path, new.author, new.body);
END;

-- ── 중복과 버전 ─────────────────────────────────────────────────────
-- 완전 중복은 해시로 결정된다. AI를 쓰지 않으므로 오탐이 없다 (DUP-001).
CREATE VIEW IF NOT EXISTS duplicate_groups AS
    SELECT hash, COUNT(*) AS n, MIN(id) AS representative_id
    FROM documents
    WHERE hash IS NOT NULL AND missing_since IS NULL
    GROUP BY hash HAVING COUNT(*) > 1;

-- 버전 후보는 이름·날짜·본문 유사도로 묶는 '제안'이다. 확정하지 않는다.
CREATE TABLE IF NOT EXISTS version_groups (
    id              INTEGER PRIMARY KEY,
    label           TEXT NOT NULL,
    latest_doc_id   INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    latest_reason   TEXT,           -- "'송부' 표기 + 최신 수정일"
    confidence      TEXT NOT NULL DEFAULT 'medium',
    decided_by      TEXT NOT NULL DEFAULT 'ai',   -- ai | user
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS version_members (
    group_id INTEGER NOT NULL REFERENCES version_groups(id) ON DELETE CASCADE,
    doc_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal  INTEGER NOT NULL DEFAULT 0,
    reason   TEXT,
    PRIMARY KEY (group_id, doc_id)
);

-- ── AI 문서 분석 (제안 상태로 저장) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS ai_document (
    doc_id         INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    title          TEXT,
    doc_type       TEXT,
    summary        TEXT,
    keywords       TEXT,            -- JSON 배열
    importance     INTEGER,
    confidence     TEXT,            -- high | medium | low | unknown
    evidence       TEXT,            -- 판단 근거가 된 구절
    model          TEXT,
    prompt_version TEXT,
    status         TEXT NOT NULL DEFAULT 'proposed',  -- proposed | approved | edited
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── 업무 (What) ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    origin      TEXT NOT NULL DEFAULT 'ai',   -- ai | user | seeded
    status      TEXT NOT NULL DEFAULT 'proposed',
    confidence  TEXT NOT NULL DEFAULT 'medium',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),

    -- v2: 확인·교정 (core/status.py의 4단계와 같은 값을 쓴다)
    review_state TEXT NOT NULL DEFAULT 'inferred',
    reviewed_at  TEXT,
    -- 사용자가 "이건 업무가 아니다"라고 판정한 묶음. 지우지 않는 이유는
    -- 재분석이 같은 묶음을 또 만들어 낼 때 다시 묻지 않기 위해서다.
    not_a_task   INTEGER NOT NULL DEFAULT 0,
    -- 합치기의 흔적. 없앤 쪽을 지우지 않고 어디로 갔는지 남긴다.
    merged_into  INTEGER REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS task_docs (
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    doc_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    origin     TEXT NOT NULL DEFAULT 'ai',    -- ai | user
    confidence TEXT NOT NULL DEFAULT 'medium',
    evidence   TEXT,
    is_primary INTEGER NOT NULL DEFAULT 0,    -- v2: 이 업무의 대표 문서
    PRIMARY KEY (task_id, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_task_docs_doc ON task_docs(doc_id);

-- 먼저 읽을 문서. 추천 이유를 반드시 함께 저장한다 — 이유 없는 별점은
-- 신뢰를 만들지 못한다.
CREATE TABLE IF NOT EXISTS task_reading (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    doc_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    score   REAL NOT NULL DEFAULT 0,
    reason  TEXT NOT NULL,
    PRIMARY KEY (task_id, doc_id)
);

-- ── 주기 (When) ─────────────────────────────────────────────────────
-- 타임라인 격자를 세로로 읽은 결과.
CREATE TABLE IF NOT EXISTS task_cycles (
    id             INTEGER PRIMARY KEY,
    task_id        INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,        -- monthly | quarterly | yearly
    months         TEXT NOT NULL,        -- "9,10,11" (yearly) / "" (monthly)
    day_hint       TEXT,                 -- "5~10일"
    years_observed INTEGER NOT NULL DEFAULT 0,
    confidence     TEXT NOT NULL DEFAULT 'low',  -- high(3년+) | medium(2년) | low
    decided_by     TEXT NOT NULL DEFAULT 'ai',
    evidence       TEXT                  -- 근거 문서 id JSON 배열
);
CREATE INDEX IF NOT EXISTS idx_cycles_task ON task_cycles(task_id);

-- ── 처리 순서 (How) ─────────────────────────────────────────────────
-- 타임라인 격자를 가로로 읽은 결과. 모든 단계는 문서에 앵커링된다.
CREATE TABLE IF NOT EXISTS task_steps (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    year        INTEGER NOT NULL,
    ordinal     INTEGER NOT NULL,
    label       TEXT NOT NULL,
    month       INTEGER,
    day_hint    TEXT,                    -- "9월 초"
    doc_id      INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    is_inferred INTEGER NOT NULL DEFAULT 0,   -- 1이면 근거 없는 추정 — UI에서 점선
    gap_note    TEXT,                    -- "③과 ④ 사이 2주는 확인되지 않음"
    decided_by  TEXT NOT NULL DEFAULT 'ai'
);
CREATE INDEX IF NOT EXISTS idx_steps_task ON task_steps(task_id, year, ordinal);

-- ── 질문·근거 (Step 9) ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chunks (
    id      INTEGER PRIMARY KEY,
    doc_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    locator TEXT NOT NULL,
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

-- 조각 단위 정확 일치 검색 (v3, 질문화면 RAG 개선 R3). document_fts는 문서
-- 단위라 어느 조각이 일치했는지 알 수 없다 — RAG는 조각을 근거로 인용하므로
-- 조각 단위 색인이 있어야 한다. document_fts와 같은 이유로 trigram을 쓴다.
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunk_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunk_fts(chunk_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunk_fts(chunk_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunk_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    model    TEXT NOT NULL,
    dim      INTEGER NOT NULL,
    vector   BLOB NOT NULL             -- float32 배열
);

-- 문서 단위 벡터. 업무 발견(군집화)의 재료다.
-- 생성 모델은 문서 1만 건에 27시간이 걸리지만 임베딩은 11분이면 끝난다.
-- 그래서 전수 처리는 여기서 하고 생성 모델은 묶음 대표에만 쓴다.
CREATE TABLE IF NOT EXISTS doc_embeddings (
    doc_id     INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vector     BLOB NOT NULL,
    source_len INTEGER NOT NULL DEFAULT 0,   -- 임베딩에 쓴 글자 수
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS questions (
    id         INTEGER PRIMARY KEY,
    question   TEXT NOT NULL,
    filters    TEXT,
    answer     TEXT,
    citations  TEXT,                   -- JSON: [{doc_id, locator}]
    withheld   INTEGER NOT NULL DEFAULT 0,  -- 근거 부족으로 유보했는가
    model      TEXT,
    rating     TEXT,
    asked_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── 교정·작업·감사 ──────────────────────────────────────────────────
-- 사람이 고친 값은 자동 재분석으로 덮어쓰지 않는다 (NFR-SAF-004).
CREATE TABLE IF NOT EXISTS corrections (
    id         INTEGER PRIMARY KEY,
    target     TEXT NOT NULL,          -- ai_document.task | task_cycles | task_steps ...
    target_id  INTEGER,
    doc_id     INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    field      TEXT NOT NULL,
    before_val TEXT,
    after_val  TEXT,
    scope      TEXT NOT NULL DEFAULT 'single',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── 인수인계 진행 (v2, 계획서 §18) ──────────────────────────────────
-- 분석이 끝난 뒤 "이제 무엇을 확인해야 하는가"에 답하는 재료. 진행도는
-- 여기서 세지, 별도로 계산하지 않는다.
CREATE TABLE IF NOT EXISTS handover_checks (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,     -- task | reading | cycle | steps
    target_id  INTEGER,
    state      TEXT NOT NULL DEFAULT 'pending',  -- pending | done | skipped
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (kind, target_id)
);

-- 업무기억 건강도 (v2, 계획서 §19). 규칙으로만 계산한다 — LLM을 쓰지 않는다.
CREATE TABLE IF NOT EXISTS task_health (
    task_id    INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    score      INTEGER NOT NULL DEFAULT 0,
    findings   TEXT,              -- JSON: 부족한 항목 목록
    checked_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── 온나라 공식 출처 (v4, 구축계획서 §5.1) ─────────────────────────
-- 수집 manifest의 문서 ID·상태·본문/붙임 관계를 프로젝트 문서에 잇는다.
-- 경로나 파일명이 아니라 원 시스템의 안정 ID가 키다. 제공되지 않은 값은
-- 빈 값(미상)으로 두고 채워 넣지 않는다. 자격 증명은 여기 들어오지 않는다.
CREATE TABLE IF NOT EXISTS collection_runs (
    id                 INTEGER PRIMARY KEY,
    collection_key     TEXT NOT NULL UNIQUE,   -- 연결부+검색 범위의 지문
    adapter            TEXT NOT NULL,
    institution        TEXT NOT NULL,
    system_id          TEXT NOT NULL,
    scope              TEXT NOT NULL,          -- JSON: 작성자·기간·소속·상태
    simulation         INTEGER NOT NULL DEFAULT 0,
    live_verified      INTEGER NOT NULL DEFAULT 0,
    status             TEXT,
    completeness       TEXT,                   -- complete | partial | unknown
    expected_documents INTEGER,
    documents          INTEGER NOT NULL DEFAULT 0,
    assets             INTEGER NOT NULL DEFAULT 0,
    downloaded         INTEGER NOT NULL DEFAULT 0,
    failed             INTEGER NOT NULL DEFAULT 0,
    started_at         TEXT,
    collected_at       TEXT,
    registered_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS source_records (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES collection_runs(id) ON DELETE CASCADE,
    institution     TEXT NOT NULL,
    system_id       TEXT NOT NULL,
    document_id     TEXT NOT NULL,             -- 원 시스템의 문서 ID
    title           TEXT NOT NULL DEFAULT '',
    document_number TEXT NOT NULL DEFAULT '',
    author_id       TEXT NOT NULL DEFAULT '',
    author_name     TEXT NOT NULL DEFAULT '',
    department      TEXT NOT NULL DEFAULT '',
    author_role     TEXT NOT NULL DEFAULT '',  -- author | drafter | coauthor
    status          TEXT NOT NULL DEFAULT 'unknown',
    authored_at     TEXT,
    registered_at   TEXT,
    approved_at     TEXT,
    first_collected TEXT NOT NULL DEFAULT (datetime('now')),
    last_collected  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (institution, system_id, document_id)
);

-- 본문과 붙임. 내려받지 못한 파일도 사유와 함께 남긴다 — 지우면 '붙임이
-- 없는 문서'와 '붙임을 못 받은 문서'를 구분할 수 없다.
CREATE TABLE IF NOT EXISTS source_assets (
    id            INTEGER PRIMARY KEY,
    record_id     INTEGER NOT NULL REFERENCES source_records(id) ON DELETE CASCADE,
    asset_id      TEXT NOT NULL,
    role          TEXT NOT NULL,               -- body | attachment
    filename      TEXT NOT NULL DEFAULT '',
    media_type    TEXT NOT NULL DEFAULT '',
    sha256        TEXT,
    size          INTEGER,
    status        TEXT NOT NULL DEFAULT 'pending',  -- downloaded | failed | denied | pending
    error         TEXT,
    downloaded_at TEXT,
    doc_id        INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    UNIQUE (record_id, asset_id)
);
CREATE INDEX IF NOT EXISTS idx_source_assets_doc  ON source_assets(doc_id);
CREATE INDEX IF NOT EXISTS idx_source_assets_hash ON source_assets(sha256);

-- ── 업무 복원·수행 (v5, 구축계획서 §5.1·§5.2) ──────────────────────
-- 전임자 없이 자료만으로 시작하는 것이 기본이다. 전임자 이름·검토자·기존
-- 인계본은 어느 표에서도 필수값이 아니다.
--
-- 네 가지 상태를 한 열에 섞지 않는다. '문서에 적혀 있다'와 '사람이
-- 확인했다'는 서로 다른 사실이고, 과거 문서의 기한이 지금도 적용되는지는
-- 또 다른 질문이다.
--   basis_state    근거 상태    stated | inferred | conflict | missing
--   applicability  적용 시점    current | past | unknown
--   human_state    사람 확인    none | predecessor | successor | recheck
--   (step_runs.state 수행 상태  not_started | in_progress | done | waiting)
--
-- 기존 표와의 관계: task_steps는 연도별 문서를 시간순으로 늘어놓은 '과거
-- 수행 사례'이고 work_steps는 후임자가 따라갈 수행 안내다. corrections는
-- AI 분석값의 교정 이력이고 reviews는 누가 어떤 자격으로 확인했는지다.

-- 업무 시작 맥락. 어떤 방식으로, 언제 기준으로 복원을 시작했는가.
CREATE TABLE IF NOT EXISTS work_contexts (
    id           INTEGER PRIMARY KEY,
    mode         TEXT NOT NULL DEFAULT 'materials',  -- materials | with_predecessor
    period_start TEXT,
    period_end   TEXT,
    basis_at     TEXT NOT NULL DEFAULT (datetime('now')),  -- 분석 기준 시각
    predecessor  TEXT,                                -- 표시명. 선택값이다
    note         TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 업무 항목. 목적·기한·금액·입력 자료·서식·산출물·연락 대상·현안 등.
CREATE TABLE IF NOT EXISTS task_items (
    id            INTEGER PRIMARY KEY,
    task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    context_id    INTEGER REFERENCES work_contexts(id) ON DELETE SET NULL,
    kind          TEXT NOT NULL,
    slot          TEXT NOT NULL DEFAULT '',     -- 재분석 때 같은 항목을 찾는 이름
    content       TEXT NOT NULL DEFAULT '',     -- 화면에 보이는 값 (사람이 고치면 그 값)
    auto_content  TEXT,                         -- 자료에서 마지막으로 읽어 낸 값
    origin        TEXT NOT NULL DEFAULT 'auto', -- auto | user
    basis_state   TEXT NOT NULL DEFAULT 'stated',
    applicability TEXT NOT NULL DEFAULT 'unknown',
    human_state   TEXT NOT NULL DEFAULT 'none',
    excluded      INTEGER NOT NULL DEFAULT 0,
    version       INTEGER NOT NULL DEFAULT 1,
    extractor     TEXT,                         -- 값을 만든 규칙·모델과 그 판
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (task_id, kind, slot)
);

-- 수행 단계. 문서에 적힌 절차·과거 사례·시스템 제안을 섞지 않는다.
CREATE TABLE IF NOT EXISTS work_steps (
    id             INTEGER PRIMARY KEY,
    task_id        INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    context_id     INTEGER REFERENCES work_contexts(id) ON DELETE SET NULL,
    slot           TEXT NOT NULL DEFAULT '',
    ordinal        INTEGER NOT NULL DEFAULT 0,
    label          TEXT NOT NULL,
    auto_label     TEXT,
    inputs         TEXT,                        -- 필요한 입력 자료
    forms          TEXT,                        -- 사용할 서식
    output         TEXT,                        -- 기대 산출물
    done_criteria  TEXT,                        -- 완료 확인 방법
    procedure_kind TEXT NOT NULL DEFAULT 'precedent',  -- stated | precedent | suggested | told
    after_step_id  INTEGER REFERENCES work_steps(id) ON DELETE SET NULL,  -- 선행 단계
    task_step_id   INTEGER REFERENCES task_steps(id) ON DELETE SET NULL,  -- 근거가 된 과거 사례
    origin         TEXT NOT NULL DEFAULT 'auto',
    basis_state    TEXT NOT NULL DEFAULT 'stated',
    applicability  TEXT NOT NULL DEFAULT 'unknown',
    human_state    TEXT NOT NULL DEFAULT 'none',
    excluded       INTEGER NOT NULL DEFAULT 0,
    version        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (task_id, slot)
);
CREATE INDEX IF NOT EXISTS idx_work_steps_task ON work_steps(task_id, ordinal);

-- 근거. 문서 '판'(그때의 해시)과 위치·인용문을 함께 남긴다. 원문이 바뀌면
-- 해시가 달라지므로 옛 인용을 새 본문의 근거처럼 보여 주지 않을 수 있다.
CREATE TABLE IF NOT EXISTS evidence (
    id           INTEGER PRIMARY KEY,
    owner_kind   TEXT NOT NULL,                 -- item | step | gap
    owner_id     INTEGER NOT NULL,
    doc_id       INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    doc_hash     TEXT,
    locator      TEXT NOT NULL DEFAULT '',
    quote        TEXT NOT NULL DEFAULT '',
    extracted_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_evidence_owner ON evidence(owner_kind, owner_id);
CREATE INDEX IF NOT EXISTS idx_evidence_doc   ON evidence(doc_id);

-- 정보 공백. 빠졌거나 충돌하거나 오래된 정보와, 그것을 메울 다음 행동.
-- step_id가 가리키는 단계만 영향을 받는다 — 나머지 단계는 계속 쓸 수 있다.
CREATE TABLE IF NOT EXISTS info_gaps (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_id     INTEGER REFERENCES work_steps(id) ON DELETE SET NULL,
    item_id     INTEGER REFERENCES task_items(id) ON DELETE SET NULL,
    kind        TEXT NOT NULL,                  -- missing | conflict | stale
    origin      TEXT NOT NULL DEFAULT 'user',   -- auto(분석이 찾음) | user(사람이 적음)
    gap_key     TEXT NOT NULL DEFAULT '',       -- 재분석 때 같은 공백을 찾는 이름
    field       TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',
    research    TEXT,                           -- 재검색에서 찾은 것 / 못 찾은 것
    next_action TEXT,                           -- 구체적인 다음 확인 방법
    state       TEXT NOT NULL DEFAULT 'open',   -- open | resolved
    resolution  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_info_gaps_task ON info_gaps(task_id, state);

-- 수행 기록. 과거 문서에 '완료'라고 적힌 것과 후임자가 실제로 끝낸 것은 다르다.
CREATE TABLE IF NOT EXISTS step_runs (
    step_id       INTEGER PRIMARY KEY REFERENCES work_steps(id) ON DELETE CASCADE,
    state         TEXT NOT NULL DEFAULT 'not_started',
    started_at    TEXT,
    finished_at   TEXT,
    result_doc_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    result_path   TEXT,
    note          TEXT,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 검토 기록. 선택 기록이며, 표시명은 로컬 이력일 뿐 본인 확인이 아니다.
CREATE TABLE IF NOT EXISTS reviews (
    id             INTEGER PRIMARY KEY,
    target_kind    TEXT NOT NULL,               -- item | step
    target_id      INTEGER NOT NULL,
    target_version INTEGER NOT NULL DEFAULT 1,
    action         TEXT NOT NULL,               -- confirm | edit | supplement | exclude
    before_val     TEXT,
    after_val      TEXT,
    reviewer_name  TEXT NOT NULL DEFAULT '',
    reviewer_role  TEXT NOT NULL DEFAULT 'successor',  -- predecessor | successor
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_reviews_target ON reviews(target_kind, target_id);

-- 업무 복원본·인계본의 스냅샷. 저장한 순간의 항목·근거·절차·공백·수행 기록을
-- 통째로 담는다. 한 번 저장한 행은 고치지 않는다 — 나중에 자료나 사람의
-- 판단이 바뀌어도 '그때 무엇을 알고 있었는가'가 남아야 한다.
--   restoration  자료에서 자동으로 복원한 것. 사람 확인이 없어도 저장된다.
--   handover     전임자가 검토해 발행한 것 (선택 경로).
CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL DEFAULT 'restoration',  -- restoration | handover
    label        TEXT NOT NULL DEFAULT '',
    context_id   INTEGER REFERENCES work_contexts(id) ON DELETE SET NULL,
    basis_at     TEXT NOT NULL,
    content_hash TEXT NOT NULL,          -- 같은 내용의 중복 저장을 막는다
    summary      TEXT NOT NULL,          -- JSON: 건수와 미확인 요약
    payload      TEXT NOT NULL,          -- JSON: 스냅샷 본체
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_snapshots_kind ON snapshots(kind, id);

-- 근거의 소유자는 표가 셋이라 외래키로 묶을 수 없다. 소유자가 지워지면
-- (업무 삭제의 연쇄 포함) 근거도 함께 지운다.
CREATE TRIGGER IF NOT EXISTS task_items_evidence_ad AFTER DELETE ON task_items BEGIN
    DELETE FROM evidence WHERE owner_kind = 'item' AND owner_id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS work_steps_evidence_ad AFTER DELETE ON work_steps BEGIN
    DELETE FROM evidence WHERE owner_kind = 'step' AND owner_id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS info_gaps_evidence_ad AFTER DELETE ON info_gaps BEGIN
    DELETE FROM evidence WHERE owner_kind = 'gap' AND owner_id = old.id;
END;

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,        -- scan | hash | parse | date | analyze | embed
    doc_id       INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    state        TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    input_hash   TEXT,                 -- 입력이 바뀌면 재실행 대상
    tool_version TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(kind, state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_unique ON jobs(kind, doc_id);

-- 로그에는 본문이나 민감정보를 남기지 않는다 (SEC-005).
CREATE TABLE IF NOT EXISTS audit_logs (
    id        INTEGER PRIMARY KEY,
    action    TEXT NOT NULL,
    target    TEXT,
    detail    TEXT,
    result    TEXT,
    at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_logs(at);
