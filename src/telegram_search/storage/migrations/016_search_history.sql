CREATE TABLE search_snapshots (
    id TEXT PRIMARY KEY,
    spec_json TEXT NOT NULL,
    spec_key TEXT NOT NULL,
    archive_revision TEXT NOT NULL,
    completed_at REAL NOT NULL,
    models_json TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    result_json BLOB,
    result_bytes INTEGER NOT NULL DEFAULT 0,
    results_count INTEGER NOT NULL,
    stale_reason TEXT
);
CREATE TABLE search_history (
    id TEXT PRIMARY KEY,
    spec_key TEXT NOT NULL UNIQUE,
    snapshot_id TEXT NOT NULL REFERENCES search_snapshots(id),
    last_run_at REAL NOT NULL,
    completion_order INTEGER NOT NULL,
    view_json TEXT NOT NULL DEFAULT '{}',
    favorite INTEGER NOT NULL DEFAULT 0 CHECK(favorite IN (0,1)),
    recent INTEGER NOT NULL DEFAULT 1 CHECK(recent IN (0,1))
);
CREATE INDEX search_history_recent ON search_history(recent,completion_order DESC);
CREATE TABLE search_snapshot_owners (
    owner TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES search_snapshots(id)
);
INSERT INTO schema_migrations(version) VALUES(16);
