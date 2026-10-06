CREATE TABLE media_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    ocr_enabled INTEGER NOT NULL DEFAULT 0,
    images_enabled INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0,
    preparation_state TEXT NOT NULL DEFAULT 'idle',
    error TEXT
);
INSERT INTO media_state(id) VALUES(1);
CREATE INDEX media_by_blob ON media_refs(sha256,kind,status);
CREATE INDEX media_by_source ON media_refs(source_root_id,id);
CREATE TABLE ocr_cache (
    rowid INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL REFERENCES media_blobs(sha256) ON DELETE CASCADE,
    version TEXT NOT NULL,
    state TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    text_normalized TEXT NOT NULL DEFAULT '',
    confidence REAL,
    error TEXT,
    UNIQUE(sha256,version)
);
CREATE VIRTUAL TABLE ocr_fts USING fts5(
    text_normalized,content='ocr_cache',content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 0'
);
CREATE TRIGGER ocr_ai AFTER INSERT ON ocr_cache BEGIN
    INSERT INTO ocr_fts(rowid,text_normalized) VALUES(new.rowid,new.text_normalized);
END;
CREATE TRIGGER ocr_ad AFTER DELETE ON ocr_cache BEGIN
    INSERT INTO ocr_fts(ocr_fts,rowid,text_normalized)
    VALUES('delete',old.rowid,old.text_normalized);
END;
CREATE TRIGGER ocr_au AFTER UPDATE OF text_normalized ON ocr_cache BEGIN
    INSERT INTO ocr_fts(ocr_fts,rowid,text_normalized)
    VALUES('delete',old.rowid,old.text_normalized);
    INSERT INTO ocr_fts(rowid,text_normalized) VALUES(new.rowid,new.text_normalized);
END;
CREATE TABLE media_embeddings (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL REFERENCES media_blobs(sha256) ON DELETE CASCADE,
    space_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('image','ocr')),
    ocr_version TEXT,
    ordinal INTEGER NOT NULL DEFAULT 0,
    char_start INTEGER NOT NULL DEFAULT 0,
    char_end INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX media_embedding_space ON media_embeddings(kind,space_id,sha256);
CREATE TABLE media_spaces (
    id TEXT PRIMARY KEY,
    dimension INTEGER NOT NULL,
    kind TEXT NOT NULL
);
CREATE TABLE media_vector_cleanup (
    sha256 TEXT PRIMARY KEY
);
CREATE TABLE media_failures (
    sha256 TEXT NOT NULL REFERENCES media_blobs(sha256) ON DELETE CASCADE,
    space_id TEXT NOT NULL,
    error TEXT NOT NULL,
    PRIMARY KEY(sha256,space_id)
);
CREATE TRIGGER media_blob_deleted AFTER DELETE ON media_blobs BEGIN
    INSERT OR IGNORE INTO media_vector_cleanup(sha256) VALUES(old.sha256);
END;
INSERT INTO schema_migrations(version) VALUES(4);
