CREATE TABLE noise_analyses (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    state TEXT NOT NULL,
    fence_json TEXT NOT NULL,
    options_json TEXT NOT NULL,
    progress_json TEXT NOT NULL DEFAULT '{}',
    summary_json TEXT,
    error TEXT
);
CREATE INDEX noise_analyses_chat ON noise_analyses(chat_id,created_at DESC);
INSERT INTO schema_migrations(version) VALUES(18);
