CREATE TABLE noise_dry_runs (
    id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL UNIQUE REFERENCES noise_analyses(id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    draft_revision INTEGER NOT NULL,
    fence_json TEXT NOT NULL,
    space_id TEXT NOT NULL,
    batch_size INTEGER NOT NULL,
    created_at REAL NOT NULL,
    state TEXT NOT NULL,
    options_json TEXT NOT NULL,
    progress_json TEXT NOT NULL DEFAULT '{}',
    summary_json TEXT,
    error TEXT
);
INSERT INTO schema_migrations(version) VALUES(20);
