ALTER TABLE imports ADD COLUMN total_messages INTEGER;
ALTER TABLE imports ADD COLUMN progress_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE import_previews ADD COLUMN total_messages INTEGER;
ALTER TABLE import_previews ADD COLUMN progress_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE import_previews ADD COLUMN preparation_json TEXT;
ALTER TABLE import_previews ADD COLUMN operation TEXT NOT NULL DEFAULT 'preview';
INSERT INTO schema_migrations(version) VALUES(15);
