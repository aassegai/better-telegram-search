ALTER TABLE chats ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE imports ADD COLUMN preview_id TEXT;
ALTER TABLE import_conflicts ADD COLUMN state TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE import_conflicts ADD COLUMN base_content_hash TEXT;
ALTER TABLE import_conflicts ADD COLUMN incoming_media_json TEXT;
ALTER TABLE import_conflicts ADD COLUMN resolved_at INTEGER;
UPDATE import_conflicts SET base_content_hash=(
    SELECT m.content_hash FROM messages m JOIN imports i ON i.chat_id=m.chat_id
    WHERE i.id=import_conflicts.import_id AND m.message_id=import_conflicts.message_id
);

CREATE TABLE import_previews (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    external_id TEXT,
    chat_name TEXT NOT NULL,
    chat_kind TEXT NOT NULL,
    target_existed INTEGER NOT NULL,
    base_revision INTEGER NOT NULL,
    root_relative_path TEXT NOT NULL,
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
    created_at INTEGER NOT NULL,
    finished_at INTEGER,
    applied_import_id TEXT REFERENCES imports(id) ON DELETE SET NULL,
    error TEXT
);
CREATE TABLE preview_entries (
    preview_id TEXT NOT NULL REFERENCES import_previews(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    classification TEXT NOT NULL,
    reason TEXT,
    base_version TEXT,
    incoming_json TEXT NOT NULL,
    media_json TEXT NOT NULL,
    PRIMARY KEY(preview_id, ordinal),
    UNIQUE(preview_id, message_id)
);
CREATE TABLE index_segments (
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    utc_day TEXT NOT NULL,
    target_generation INTEGER NOT NULL,
    lexical_generation INTEGER NOT NULL,
    dense_generation INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(chat_id, utc_day)
);
CREATE TABLE index_work (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    utc_day TEXT NOT NULL,
    generation INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    error TEXT,
    created_at INTEGER NOT NULL,
    finished_at INTEGER,
    FOREIGN KEY(chat_id, utc_day) REFERENCES index_segments(chat_id, utc_day) ON DELETE CASCADE,
    UNIQUE(chat_id, utc_day, generation)
);
CREATE INDEX index_work_state ON index_work(state, created_at);
INSERT INTO index_segments(chat_id,utc_day,target_generation,lexical_generation)
SELECT chat_id,date(timestamp,'unixepoch'),1,1 FROM messages
GROUP BY chat_id,date(timestamp,'unixepoch');
INSERT INTO index_work(id,chat_id,utc_day,generation,reason,created_at)
SELECT chat_id || ':' || utc_day || ':1',chat_id,utc_day,1,'migration',CAST(strftime('%s','now') AS INTEGER)
FROM index_segments;
INSERT INTO schema_migrations(version) VALUES (2);
