CREATE TABLE embedding_spaces (
    id TEXT PRIMARY KEY,
    profile TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE semantic_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    active_space_id TEXT REFERENCES embedding_spaces(id),
    enabled INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0,
    preparation_state TEXT NOT NULL DEFAULT 'idle',
    requested_profile TEXT,
    download_completed_bytes INTEGER NOT NULL DEFAULT 0,
    download_total_bytes INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
INSERT INTO semantic_state(id) VALUES(1);
ALTER TABLE index_segments ADD COLUMN chunk_generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE index_segments ADD COLUMN embedding_space_id TEXT REFERENCES embedding_spaces(id);
ALTER TABLE index_work ADD COLUMN embedding_space_id TEXT REFERENCES embedding_spaces(id);
ALTER TABLE index_work ADD COLUMN stage TEXT NOT NULL DEFAULT 'building';
ALTER TABLE index_work ADD COLUMN chunks_total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE index_work ADD COLUMN chunks_done INTEGER NOT NULL DEFAULT 0;
ALTER TABLE index_work ADD COLUMN tokens_total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE index_work ADD COLUMN embedding_seconds REAL NOT NULL DEFAULT 0;
CREATE TABLE chunks (
    rowid INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    utc_day TEXT NOT NULL,
    generation INTEGER NOT NULL,
    embedding_space_id TEXT NOT NULL REFERENCES embedding_spaces(id),
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    text_normalized TEXT NOT NULL,
    tokens INTEGER NOT NULL CHECK(tokens BETWEEN 1 AND 480),
    UNIQUE(chat_id,utc_day,generation,embedding_space_id,ordinal)
);
CREATE INDEX chunks_segment ON chunks(chat_id,utc_day,generation,embedding_space_id);
CREATE TABLE chunk_parts (
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    char_start INTEGER NOT NULL CHECK(char_start>=0),
    char_end INTEGER NOT NULL CHECK(char_end>=char_start),
    PRIMARY KEY(chunk_id,ordinal),
    FOREIGN KEY(chat_id,message_id) REFERENCES messages(chat_id,message_id) ON DELETE CASCADE
);
CREATE INDEX parts_message ON chunk_parts(chat_id,message_id,chunk_id);
CREATE VIRTUAL TABLE chunk_fts USING fts5(
    text_normalized,content='chunks',content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 0'
);
CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunk_fts(rowid,text_normalized) VALUES(new.rowid,new.text_normalized);
END;
CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunk_fts(chunk_fts,rowid,text_normalized)
    VALUES('delete',old.rowid,old.text_normalized);
END;
CREATE TRIGGER chunks_au AFTER UPDATE OF text_normalized ON chunks BEGIN
    INSERT INTO chunk_fts(chunk_fts,rowid,text_normalized)
    VALUES('delete',old.rowid,old.text_normalized);
    INSERT INTO chunk_fts(rowid,text_normalized) VALUES(new.rowid,new.text_normalized);
END;
CREATE TABLE vector_deletions (
    chat_id TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    error TEXT
);
CREATE TABLE vector_segment_cleanup (
    chat_id TEXT NOT NULL,
    utc_day TEXT NOT NULL,
    keep_space_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    error TEXT,
    PRIMARY KEY(chat_id,utc_day)
);
INSERT INTO schema_migrations(version) VALUES(3);
