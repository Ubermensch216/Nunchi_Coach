-- v10: 행동형 업무 비서 (docs/행동형_업무비서_구현계획서_2026-10-10.md 6절)
--
-- 기존 표의 행은 바꾸지 않는다. 새 표만 더한다.
--
-- 업무기억과 실행기억을 가른다. tasks·task_items·work_steps는 '이 업무는 어떻게
-- 하는 일인가'이고, work_cases 아래의 표들은 '올해 이 건에서 실제로 무엇을
-- 했는가'다. 2025년의 절차와 2026년의 수행이 같은 행에 섞이지 않는다.

-- ── 업무기억 보강 ───────────────────────────────────────────────────
-- 문서가 그 업무에서 맡은 역할. 못 정하면 other·low로 둔다.
CREATE TABLE IF NOT EXISTS doc_roles (
    doc_id     INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,          -- directive | criteria | plan | performance | evidence | submission | result | form | other
    basis      TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT 'low',
    decided_by TEXT NOT NULL DEFAULT 'ai',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 한 문서의 여러 '해'. documents.eff_year는 그대로 둔다.
CREATE TABLE IF NOT EXISTS doc_years (
    doc_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,          -- authored | performance | evaluation | submitted
    year       INTEGER NOT NULL,
    raw        TEXT,
    locator    TEXT,
    decided_by TEXT NOT NULL DEFAULT 'ai',
    PRIMARY KEY (doc_id, kind, year)
);

-- ── 수행 건 ─────────────────────────────────────────────────────────
-- 올해 실제로 하는 한 건. 기한은 그 해의 공식 문서나 사람의 확인에서만 온다.
-- 과거 사례의 날짜는 이 표에 들어오지 않는다.
CREATE TABLE IF NOT EXISTS work_cases (
    id           INTEGER PRIMARY KEY,
    task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    period_year  INTEGER NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    state        TEXT NOT NULL DEFAULT 'open',          -- open | closed | not_this_year
    basis_doc_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    basis_state  TEXT NOT NULL DEFAULT 'unconfirmed',   -- unconfirmed | official | user
    due_date     TEXT,
    due_state    TEXT NOT NULL DEFAULT 'unknown',       -- unknown | official | user
    opened_at    TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at    TEXT,
    UNIQUE (task_id, period_year, label)
);

-- ── 감지와 제안 ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,                          -- time | event | gap | conversation
    task_id     INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    signal_key  TEXT NOT NULL UNIQUE,
    detail      TEXT NOT NULL DEFAULT '{}',             -- 식별자와 수치만. 본문을 넣지 않는다
    detected_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS recommendations (
    id             INTEGER PRIMARY KEY,
    signal_id      INTEGER REFERENCES signals(id) ON DELETE SET NULL,
    task_id        INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    target_year    INTEGER,
    plan_key       TEXT NOT NULL,
    rec_key        TEXT NOT NULL UNIQUE,
    certainty      TEXT NOT NULL DEFAULT 'precedent',   -- official | precedent | suggested
    state          TEXT NOT NULL DEFAULT 'new',         -- new | seen | accepted | dismissed | snoozed | expired | superseded
    snooze_until   TEXT,
    dismiss_reason TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_recommendations_state ON recommendations(state, task_id);

-- ── 실행 ────────────────────────────────────────────────────────────
-- idem_key가 같으면 같은 실행이다. 다시 눌러도 둘이 되지 않는다.
CREATE TABLE IF NOT EXISTS action_runs (
    id                INTEGER PRIMARY KEY,
    case_id           INTEGER REFERENCES work_cases(id) ON DELETE SET NULL,
    task_id           INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    recommendation_id INTEGER REFERENCES recommendations(id) ON DELETE SET NULL,
    plan_key          TEXT NOT NULL,
    plan_version      TEXT NOT NULL,
    idem_key          TEXT NOT NULL UNIQUE,
    params            TEXT NOT NULL DEFAULT '{}',
    state             TEXT NOT NULL DEFAULT 'planned',  -- planned | running | waiting_for_approval | completed | failed | partial | cancelled
    started_at        TEXT,
    finished_at       TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_action_runs_case ON action_runs(case_id, id);

CREATE TABLE IF NOT EXISTS action_steps (
    id             INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL REFERENCES action_runs(id) ON DELETE CASCADE,
    ordinal        INTEGER NOT NULL,
    step_key       TEXT NOT NULL DEFAULT '',
    tool           TEXT NOT NULL,
    tool_version   TEXT NOT NULL,
    risk           TEXT NOT NULL,                       -- read | draft | change
    state          TEXT NOT NULL DEFAULT 'pending',     -- pending | running | done | failed | skipped | blocked | waiting_for_approval
    input_hash     TEXT,
    output         TEXT NOT NULL DEFAULT '{}',          -- 다음 단계로 넘기는 식별자와 수치
    model          TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    attempts       INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    started_at     TEXT,
    finished_at    TEXT,
    UNIQUE (run_id, ordinal)
);

-- 승인은 승인한 내용의 지문(scope_hash)에 묶인다. 내용이 달라지면 쓸 수 없다.
-- 표시명은 로컬 이력일 뿐 본인 확인이 아니다(reviews와 같다).
CREATE TABLE IF NOT EXISTS action_approvals (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES action_runs(id) ON DELETE CASCADE,
    step_id       INTEGER REFERENCES action_steps(id) ON DELETE CASCADE,
    action        TEXT NOT NULL,
    scope_hash    TEXT NOT NULL,
    decision      TEXT NOT NULL,                        -- approved | rejected
    approver_name TEXT NOT NULL DEFAULT '',
    approver_role TEXT NOT NULL DEFAULT 'successor',
    note          TEXT,
    decided_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_action_approvals_step ON action_approvals(step_id, scope_hash);

-- 산출물 원장. 파일은 프로젝트 폴더의 outputs/actions 아래에만 있다.
CREATE TABLE IF NOT EXISTS artifacts (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER REFERENCES action_runs(id) ON DELETE SET NULL,
    step_id         INTEGER REFERENCES action_steps(id) ON DELETE SET NULL,
    case_id         INTEGER REFERENCES work_cases(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL,
    title           TEXT NOT NULL,
    mode            TEXT NOT NULL DEFAULT 'preliminary', -- preliminary | official
    basis_year      INTEGER,
    format          TEXT NOT NULL,
    path            TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    source_snapshot TEXT NOT NULL DEFAULT '[]',
    state           TEXT NOT NULL DEFAULT 'draft',      -- draft | reviewed | final | stale | superseded
    version         INTEGER NOT NULL DEFAULT 1,
    supersedes_id   INTEGER REFERENCES artifacts(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_artifacts_case ON artifacts(case_id, kind, id);

CREATE TABLE IF NOT EXISTS checklist_items (
    id                INTEGER PRIMARY KEY,
    case_id           INTEGER NOT NULL REFERENCES work_cases(id) ON DELETE CASCADE,
    run_id            INTEGER REFERENCES action_runs(id) ON DELETE SET NULL,
    work_step_id      INTEGER REFERENCES work_steps(id) ON DELETE SET NULL,
    slot              TEXT NOT NULL,
    code              TEXT NOT NULL DEFAULT '',
    ordinal           INTEGER NOT NULL DEFAULT 0,
    phase             TEXT NOT NULL DEFAULT '',
    title             TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    source_type       TEXT NOT NULL,                    -- official | stated | precedent | suggested | user
    assignee          TEXT NOT NULL DEFAULT '',
    due_date          TEXT,
    due_state         TEXT NOT NULL DEFAULT 'unknown',  -- unknown | official | user
    depends_on        INTEGER REFERENCES checklist_items(id) ON DELETE SET NULL,
    approval_required INTEGER NOT NULL DEFAULT 0,
    state             TEXT NOT NULL DEFAULT 'todo',     -- todo | doing | review | done | skipped
    completion_kind   TEXT,
    completion_ref    TEXT,
    origin            TEXT NOT NULL DEFAULT 'auto',     -- auto | user
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (case_id, slot)
);

-- 근거는 기존 evidence 표를 쓴다. 소유자 갈래 셋을 더한다.
CREATE TRIGGER IF NOT EXISTS recommendations_evidence_ad AFTER DELETE ON recommendations BEGIN
    DELETE FROM evidence WHERE owner_kind = 'rec' AND owner_id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS checklist_items_evidence_ad AFTER DELETE ON checklist_items BEGIN
    DELETE FROM evidence WHERE owner_kind = 'check' AND owner_id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS artifacts_evidence_ad AFTER DELETE ON artifacts BEGIN
    DELETE FROM evidence WHERE owner_kind = 'artifact' AND owner_id = old.id;
END;
