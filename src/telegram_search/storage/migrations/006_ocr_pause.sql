ALTER TABLE chats ADD COLUMN ocr_paused INTEGER NOT NULL DEFAULT 0 CHECK(ocr_paused IN (0,1));
ALTER TABLE media_state ADD COLUMN ocr_paused INTEGER NOT NULL DEFAULT 0 CHECK(ocr_paused IN (0,1));
UPDATE chats SET ocr_paused=media_paused;
UPDATE media_state SET ocr_paused=paused;
INSERT INTO schema_migrations(version) VALUES(6);
