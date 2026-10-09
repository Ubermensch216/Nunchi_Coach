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
