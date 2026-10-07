ALTER TABLE chats ADD COLUMN text_paused INTEGER NOT NULL DEFAULT 0 CHECK(text_paused IN (0,1));
ALTER TABLE chats ADD COLUMN media_paused INTEGER NOT NULL DEFAULT 0 CHECK(media_paused IN (0,1));
ALTER TABLE chats ADD COLUMN text_batch INTEGER CHECK(text_batch BETWEEN 1 AND 128);
ALTER TABLE chats ADD COLUMN image_batch INTEGER CHECK(image_batch BETWEEN 1 AND 32);
CREATE TABLE index_rates (
    kind TEXT NOT NULL,
    space_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    seconds_per_unit REAL NOT NULL,
    batches INTEGER NOT NULL,
    PRIMARY KEY(kind,space_id,provider)
);
INSERT INTO schema_migrations(version) VALUES(5);
