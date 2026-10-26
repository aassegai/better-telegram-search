CREATE TABLE search_tabs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'search' CHECK(kind IN ('search')),
    position INTEGER NOT NULL UNIQUE,
    revision INTEGER NOT NULL DEFAULT 0,
    draft_json TEXT NOT NULL,
    view_json TEXT NOT NULL DEFAULT '{}',
    snapshot_id TEXT REFERENCES search_snapshots(id),
    run_job_id TEXT,
    interrupted INTEGER NOT NULL DEFAULT 0 CHECK(interrupted IN (0,1)),
    last_state TEXT,
    error TEXT
);
CREATE TABLE search_tab_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    active_tab_id TEXT REFERENCES search_tabs(id) ON DELETE SET NULL
);
INSERT INTO search_tab_state(id) VALUES(1);
INSERT INTO schema_migrations(version) VALUES(17);
