ALTER TABLE index_work ADD COLUMN available_at INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN remote_deleted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE source_roots ADD COLUMN managed INTEGER NOT NULL DEFAULT 0;
CREATE TABLE telegram_connections (
    id TEXT PRIMARY KEY,
    slot INTEGER NOT NULL DEFAULT 1 UNIQUE CHECK(slot=1),
    account_user_id INTEGER,
    state TEXT NOT NULL DEFAULT 'disconnected',
    reconnect INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    retry_after INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE dialog_sync_bindings (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL UNIQUE REFERENCES chats(id) ON DELETE CASCADE,
    connection_id TEXT NOT NULL REFERENCES telegram_connections(id),
    account_user_id INTEGER NOT NULL,
    peer_type TEXT NOT NULL CHECK(peer_type IN ('user','chat','channel')),
    peer_id INTEGER NOT NULL CHECK(peer_id>0),
    access_hash TEXT,
    source_root_id INTEGER NOT NULL REFERENCES source_roots(id),
    enabled INTEGER NOT NULL DEFAULT 1,
    download_media INTEGER NOT NULL DEFAULT 1,
    deletion_policy TEXT NOT NULL DEFAULT 'archive' CHECK(deletion_policy IN ('archive','mirror')),
    reconcile_days INTEGER NOT NULL DEFAULT 7 CHECK(reconcile_days BETWEEN 1 AND 30),
    revision INTEGER NOT NULL DEFAULT 1,
    generation INTEGER NOT NULL DEFAULT 1,
    last_success_at INTEGER,
    next_sync_at INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    UNIQUE(account_user_id,peer_type,peer_id)
);
CREATE TABLE dialog_sync_cursors (
    binding_id TEXT PRIMARY KEY REFERENCES dialog_sync_bindings(id) ON DELETE CASCADE,
    baseline_id INTEGER NOT NULL,
    scanned_through_id INTEGER NOT NULL,
    upper_bound INTEGER,
    recent_offset_id INTEGER,
    recent_upper_bound INTEGER,
    recent_since INTEGER,
    reconciled_at INTEGER,
    coverage TEXT NOT NULL DEFAULT 'partial_export'
);
CREATE TABLE sync_runs (
    id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL REFERENCES dialog_sync_bindings(id) ON DELETE CASCADE,
    trigger TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    fetched INTEGER NOT NULL DEFAULT 0,
    added INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    unchanged INTEGER NOT NULL DEFAULT 0,
    conflicts INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    retry_after INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX one_active_sync ON sync_runs(binding_id)
    WHERE state IN ('queued','running','partial','waiting_rate_limit');
CREATE TABLE telegram_jobs (
    id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL REFERENCES dialog_sync_bindings(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES sync_runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('scan','media')),
    asset_id TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'pending',
    attempt INTEGER NOT NULL DEFAULT 0,
    available_at INTEGER NOT NULL DEFAULT 0,
    lease_until INTEGER NOT NULL DEFAULT 0,
    error_code TEXT
);
CREATE INDEX telegram_jobs_due ON telegram_jobs(kind,state,available_at,lease_until);
CREATE TABLE telegram_message_provenance (
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    binding_id TEXT REFERENCES dialog_sync_bindings(id) ON DELETE SET NULL,
    remote_edit_at INTEGER,
    observed_at INTEGER NOT NULL,
    verified_at INTEGER NOT NULL,
    semantic_hash TEXT NOT NULL,
    remote_media_id TEXT,
    PRIMARY KEY(chat_id,message_id),
    FOREIGN KEY(chat_id,message_id) REFERENCES messages(chat_id,message_id) ON DELETE CASCADE
);
CREATE TABLE telegram_tombstones (
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL,
    account_user_id INTEGER NOT NULL,
    peer_type TEXT NOT NULL,
    peer_id INTEGER NOT NULL,
    observed_at INTEGER NOT NULL,
    policy TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT 'delete_event',
    PRIMARY KEY(chat_id,message_id)
);
CREATE TABLE telegram_sync_conflicts (
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    PRIMARY KEY(chat_id,message_id)
);
CREATE TABLE telegram_assets (
    id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL REFERENCES dialog_sync_bindings(id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    remote_identity TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    relative_path TEXT,
    sha256 TEXT,
    size INTEGER,
    reserved_bytes INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    created_at INTEGER NOT NULL,
    FOREIGN KEY(chat_id,message_id) REFERENCES messages(chat_id,message_id) ON DELETE CASCADE,
    UNIQUE(binding_id,message_id,remote_identity)
);
CREATE INDEX media_refs_path ON media_refs(relative_path,source_root_id);
CREATE INDEX telegram_assets_blob ON telegram_assets(sha256);
INSERT INTO schema_migrations(version) VALUES(8);
