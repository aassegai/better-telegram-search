ALTER TABLE noise_analyses ADD COLUMN overrides_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE noise_analyses ADD COLUMN draft_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE noise_analyses ADD COLUMN applied_policy_id TEXT REFERENCES chunking_policies(id);
ALTER TABLE noise_analyses ADD COLUMN applied_draft_revision INTEGER;
INSERT INTO schema_migrations(version) VALUES(19);
