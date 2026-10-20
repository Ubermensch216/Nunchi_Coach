CREATE TABLE IF NOT EXISTS notebooks (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, purpose TEXT NOT NULL DEFAULT '',
 task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
 classification TEXT NOT NULL DEFAULT 'private' CHECK(classification IN ('private','restricted')),
 archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS notebook_sources (
 notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
 doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 version_hash TEXT NOT NULL, selected INTEGER NOT NULL DEFAULT 1,
 allowed INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(notebook_id,doc_id)
);
CREATE TABLE IF NOT EXISTS notebook_messages (
 id INTEGER PRIMARY KEY, notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
 question TEXT NOT NULL, answer TEXT NOT NULL, source_snapshot TEXT NOT NULL,
 citations TEXT NOT NULL, generation TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
 withheld INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS notebook_notes (
 id INTEGER PRIMARY KEY, notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
 message_id INTEGER REFERENCES notebook_messages(id) ON DELETE SET NULL,
 title TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'ai_draft',
 revision INTEGER NOT NULL DEFAULT 1, evidence TEXT NOT NULL DEFAULT '[]',
 created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS notebook_note_revisions (
 id INTEGER PRIMARY KEY, note_id INTEGER NOT NULL REFERENCES notebook_notes(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL, body TEXT NOT NULL, reason TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE(note_id,revision)
);
CREATE TABLE IF NOT EXISTS knowledge_cards (
 id INTEGER PRIMARY KEY, note_id INTEGER NOT NULL REFERENCES notebook_notes(id) ON DELETE CASCADE,
 note_revision INTEGER NOT NULL, task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
 body TEXT NOT NULL, evidence TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'verified',
 reviewer TEXT NOT NULL, applicability TEXT NOT NULL, valid_until TEXT,
 reviewed_at TEXT NOT NULL DEFAULT (datetime('now')), published_at TEXT
);
-- ── v7: 자료 분석 업무노트 (법령 조회·서식 산출물) ─────────────────────
-- 기존 표에 붙는 열은 db/migrations.py의 NOTEBOOK_COLUMNS가 더한다. 이 파일에는
-- 방아쇠를 두지 않는다. 승격 단계가 이 파일을 쌍반점으로 끊어 실행하므로
-- 주석에도 그 글자를 쓰지 않는다.
CREATE TABLE IF NOT EXISTS law_cache (
 key TEXT PRIMARY KEY, tool TEXT NOT NULL, payload TEXT NOT NULL,
 fetched_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doc_templates (
 id INTEGER PRIMARY KEY, key TEXT UNIQUE, title TEXT NOT NULL,
 doc_type TEXT NOT NULL DEFAULT '기타', description TEXT NOT NULL DEFAULT '',
 sections TEXT NOT NULL DEFAULT '[]', guidance TEXT NOT NULL DEFAULT '',
 format TEXT NOT NULL DEFAULT '', builtin INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL DEFAULT (datetime('now')),
 updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS notebook_outputs (
 id INTEGER PRIMARY KEY, notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
 template_id INTEGER REFERENCES doc_templates(id) ON DELETE SET NULL,
 title TEXT NOT NULL, doc_type TEXT NOT NULL DEFAULT '', model TEXT NOT NULL,
 source_snapshot TEXT NOT NULL DEFAULT '{}', citations TEXT NOT NULL DEFAULT '[]',
 state TEXT NOT NULL DEFAULT 'ai_draft', revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL DEFAULT (datetime('now')),
 updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS output_files (
 id INTEGER PRIMARY KEY, output_id INTEGER NOT NULL REFERENCES notebook_outputs(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL, format TEXT NOT NULL, path TEXT NOT NULL,
 sha256 TEXT NOT NULL, renderer TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS manual_versions (
 id INTEGER PRIMARY KEY,
 task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 version INTEGER NOT NULL,
 digest TEXT NOT NULL,
 notebook_id INTEGER REFERENCES notebooks(id) ON DELETE SET NULL,
 doc_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
 status TEXT NOT NULL DEFAULT 'ok' CHECK(status IN ('ok','failed')),
 note TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_manual_versions_task ON manual_versions(task_id, version);
