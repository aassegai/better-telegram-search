CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY);
CREATE TABLE chats (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    external_id TEXT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(scope, external_id)
);
CREATE TABLE source_roots (
    id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    UNIQUE(chat_id, relative_path)
);
CREATE TABLE messages (
    rowid INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL,
    timestamp INTEGER NOT NULL,
    edited_timestamp INTEGER,
    author_id TEXT,
    author TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    text_normalized TEXT NOT NULL,
    reply_to INTEGER,
    has_photo INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE(chat_id, message_id)
);
CREATE INDEX messages_chronology ON messages(chat_id, timestamp, message_id);
CREATE INDEX messages_author ON messages(chat_id, author_id, timestamp);
CREATE VIRTUAL TABLE message_fts USING fts5(
    text_normalized, content='messages', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 0'
);
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO message_fts(rowid, text_normalized) VALUES (new.rowid, new.text_normalized);
END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO message_fts(message_fts, rowid, text_normalized)
    VALUES ('delete', old.rowid, old.text_normalized);
END;
CREATE TRIGGER messages_au AFTER UPDATE OF text_normalized ON messages BEGIN
    INSERT INTO message_fts(message_fts, rowid, text_normalized)
    VALUES ('delete', old.rowid, old.text_normalized);
    INSERT INTO message_fts(rowid, text_normalized) VALUES (new.rowid, new.text_normalized);
END;
CREATE TABLE media_blobs (
    sha256 TEXT PRIMARY KEY,
    size INTEGER NOT NULL
);
CREATE TABLE media_refs (
    id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    source_root_id INTEGER NOT NULL REFERENCES source_roots(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    kind TEXT NOT NULL,
    sha256 TEXT REFERENCES media_blobs(sha256),
    status TEXT NOT NULL,
    FOREIGN KEY(chat_id, message_id) REFERENCES messages(chat_id, message_id) ON DELETE CASCADE,
    UNIQUE(chat_id, message_id, source_root_id, relative_path)
);
CREATE INDEX media_by_message ON media_refs(chat_id, message_id);
CREATE TABLE imports (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    source_root_id INTEGER NOT NULL REFERENCES source_roots(id) ON DELETE CASCADE,
    json_relative_path TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    state TEXT NOT NULL,
    policy TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0,
    added INTEGER NOT NULL DEFAULT 0,
    unchanged INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    conflicts INTEGER NOT NULL DEFAULT 0,
    missing_media INTEGER NOT NULL DEFAULT 0,
    invalid_media INTEGER NOT NULL DEFAULT 0,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    error TEXT
);
CREATE TABLE import_conflicts (
    import_id TEXT NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    incoming_json TEXT NOT NULL,
    PRIMARY KEY(import_id, message_id)
);
INSERT INTO schema_migrations(version) VALUES (1);
