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
