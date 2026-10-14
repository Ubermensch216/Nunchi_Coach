-- 건 — 업무 안에서 하나씩 따로 처리되는 일 (규제개혁 개선계획서 P1-1 WorkCase)
--
-- 업무(tasks)는 반복하는 책임이고, 회차(work_cases)는 그 업무의 한 해 수행이다. 건은 그 안의
-- 낱낱의 대상이다 — 규제 개선 건의 한 건, 개선 사례 한 건, 정비 대상 규제사무 한 건.
-- 한 건은 여러 문서에 나온다(부서 제출 목록 → 우리 기관 제출 목록 → 상급기관 취합표 → 부처
-- 검토결과). 그 등장 하나하나가 matter_sources의 한 행이고, 근거 위치는 시트와 행 번호다.
--
-- 행 번호는 근거일 뿐 식별자가 아니다. 건의 식별자는 관할과 제목의 정규형(key)이다.

CREATE TABLE IF NOT EXISTS matters (
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,               -- 관할|정규화한 제목
    title         TEXT NOT NULL,
    locality      TEXT NOT NULL DEFAULT '',           -- 시군구
    kind          TEXT NOT NULL DEFAULT 'proposal',   -- proposal | case | regulation | agenda
    dept          TEXT NOT NULL DEFAULT '',           -- 주관·소관 부서(우리 쪽)
    ministry      TEXT NOT NULL DEFAULT '',           -- 소관 부처·기관(상대 쪽)
    law           TEXT NOT NULL DEFAULT '',
    stage         TEXT NOT NULL DEFAULT 'listed',     -- listed | compiled | answered (자료로 확인한 데까지)
    outcome       TEXT NOT NULL DEFAULT '',           -- accepted | partial | rejected | pending | ''
    outcome_text  TEXT NOT NULL DEFAULT '',
    human_stage   TEXT,                               -- 사람이 정한 진행: submitted | accepted | partial | rejected | implemented | closed
    human_note    TEXT NOT NULL DEFAULT '',
    excluded      INTEGER NOT NULL DEFAULT 0,         -- 사람이 '건이 아님'으로 뺀 것
    missing_since TEXT,                               -- 근거 문서에서 더는 보이지 않음
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS matter_sources (
    id          INTEGER PRIMARY KEY,
    matter_id   INTEGER NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
    doc_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    sheet       TEXT NOT NULL,
    row         INTEGER NOT NULL,
    list_kind   TEXT NOT NULL,                        -- own(우리 기관 목록) | compiled(여러 기관 취합)
    round       TEXT NOT NULL DEFAULT '',             -- '2025 상반기', '2024 2분기'
    period_year INTEGER,
    fields      TEXT NOT NULL DEFAULT '{}',           -- 그 행의 주요 칸 (머리글: 값)
    quote       TEXT NOT NULL DEFAULT '',
    UNIQUE (matter_id, doc_id, sheet, row)
);
CREATE INDEX IF NOT EXISTS matter_sources_doc ON matter_sources(doc_id);

-- 목록 문서를 언제, 어떤 판(해시)으로 읽었는가. 판이 같으면 다시 열지 않는다.
CREATE TABLE IF NOT EXISTS matter_scans (
    doc_id      INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    hash        TEXT,
    state       TEXT NOT NULL,                        -- read | not_a_list | unreadable | changed
    rows_own    INTEGER NOT NULL DEFAULT 0,
    rows_other  INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 건의 자취 — 건 제목이 목록 밖의 문서(공문과 그 붙임)에 나온 곳 (core/matter_trail.py).
-- signal: answered(회신 검토의견) | decided(위원회 심의·의결) | enacted(의회 의결 — 시행 근거 후보)
--         | reported(추진 결과 보고 — 시행 근거 후보) | submitted(보낸 제출 공문) | mentioned(언급만)
-- 덮개 공문(letter_*)은 그 문서 자신이거나 같은 폴더(같은 압축)의 공문이다.
CREATE TABLE IF NOT EXISTS matter_mentions (
    id             INTEGER PRIMARY KEY,
    matter_id      INTEGER NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
    doc_id         INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    locator        TEXT NOT NULL DEFAULT '',
    signal         TEXT NOT NULL,
    result         TEXT NOT NULL DEFAULT '',          -- 제목 뒤에서 읽은 낱말: '수용곤란', '개정', '원안가결'
    outcome        TEXT NOT NULL DEFAULT '',          -- 회신 갈래(accepted | partial | rejected | pending)
    letter_id      INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    letter         TEXT NOT NULL DEFAULT '',          -- 시행 번호 '지방규제혁신과-1234'
    direction      TEXT NOT NULL DEFAULT '',          -- received | outgoing | internal
    letter_date    TEXT NOT NULL DEFAULT '',
    letter_subject TEXT NOT NULL DEFAULT '',
    quote          TEXT NOT NULL DEFAULT '',
    hash           TEXT,                              -- 읽은 문서의 판
    UNIQUE (matter_id, doc_id, locator)
);
CREATE INDEX IF NOT EXISTS matter_mentions_matter ON matter_mentions(matter_id);
