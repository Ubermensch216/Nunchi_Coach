CREATE TABLE IF NOT EXISTS chunk_children (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    text TEXT NOT NULL,
    version TEXT NOT NULL,
    UNIQUE(parent_id, ordinal)
);
CREATE TABLE IF NOT EXISTS child_embeddings (
    child_id INTEGER PRIMARY KEY REFERENCES chunk_children(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS child_fts USING fts5(
    text, content='chunk_children', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS child_ai AFTER INSERT ON chunk_children BEGIN
    INSERT INTO child_fts(rowid,text) VALUES(new.id,new.text);
END;
CREATE TRIGGER IF NOT EXISTS child_ad AFTER DELETE ON chunk_children BEGIN
    INSERT INTO child_fts(child_fts,rowid,text) VALUES('delete',old.id,old.text);
END;
CREATE TRIGGER IF NOT EXISTS child_au AFTER UPDATE ON chunk_children BEGIN
    INSERT INTO child_fts(child_fts,rowid,text) VALUES('delete',old.id,old.text);
    INSERT INTO child_fts(rowid,text) VALUES(new.id,new.text);
END;
-- 색인 전환(core/reindex.py). 조각 규칙이 바뀌면 새 조각을 chunk_staging에 준비하고,
-- 검증이 끝난 뒤 한 번에 바꾼다. 밀려난 옛 조각은 chunk_archive에 남는다 — 저장된
-- 답변·메모의 인용(위치와 원문)을 계속 대조할 수 있고, 전환을 되돌릴 수 있다.
CREATE TABLE IF NOT EXISTS chunk_staging (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    locator TEXT NOT NULL,
    text TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    doc_hash TEXT NOT NULL,
    version TEXT NOT NULL,
    model TEXT,
    dim INTEGER,
    vector BLOB,
    UNIQUE(doc_id, ordinal)
);
CREATE TABLE IF NOT EXISTS chunk_archive (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    locator TEXT NOT NULL,
    text TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    doc_hash TEXT NOT NULL,
    revision INTEGER NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    model TEXT,
    dim INTEGER,
    vector BLOB,
    archived_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chunk_archive_doc ON chunk_archive(doc_id, locator);
