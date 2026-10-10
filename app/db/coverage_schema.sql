-- v9: append-only scan observations and current processing diagnostics.
-- Existing documents, reviewed tasks, notebook citations and indexes are unchanged.
CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_scan_runs_source ON scan_runs(source_id, id);
CREATE TABLE IF NOT EXISTS scan_entries (
    run_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    doc_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    PRIMARY KEY(run_id, path)
);
CREATE TABLE IF NOT EXISTS processing_issues (
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    reason TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(doc_id, stage)
);
CREATE TABLE IF NOT EXISTS pipeline_stage_results (
    stage TEXT PRIMARY KEY,
    done INTEGER NOT NULL,
    total INTEGER NOT NULL,
    state TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    errors TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS discovery_evidence (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    piece_key TEXT NOT NULL,
    chunk_id INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
    doc_hash TEXT,
    locator TEXT NOT NULL DEFAULT '',
    basis TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    index_version TEXT NOT NULL DEFAULT '',
    reviewed_by_model INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(task_id, doc_id, piece_key)
);
CREATE TABLE IF NOT EXISTS discovery_holds (
    signature TEXT PRIMARY KEY,
    doc_ids TEXT NOT NULL,
    reason TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
