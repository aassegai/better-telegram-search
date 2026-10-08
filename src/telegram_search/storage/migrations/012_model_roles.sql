ALTER TABLE media_state ADD COLUMN visual_profile TEXT NOT NULL DEFAULT 'clip';
ALTER TABLE media_state ADD COLUMN visual_space_id TEXT;
CREATE TABLE rerank_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    preparation_state TEXT NOT NULL DEFAULT 'idle',
    download_completed_bytes INTEGER NOT NULL DEFAULT 0,
    download_total_bytes INTEGER NOT NULL DEFAULT 0,
    manifest_id TEXT,
    error TEXT
);
INSERT INTO rerank_state(id) VALUES(1);
INSERT INTO schema_migrations(version) VALUES(12);
