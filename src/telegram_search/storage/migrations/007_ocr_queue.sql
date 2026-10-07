ALTER TABLE chats ADD COLUMN ocr_dense_paused INTEGER NOT NULL DEFAULT 0 CHECK(ocr_dense_paused IN (0,1));
ALTER TABLE chats ADD COLUMN ocr_batch INTEGER CHECK(ocr_batch BETWEEN 1 AND 4);
ALTER TABLE chats ADD COLUMN ocr_region_batch INTEGER CHECK(ocr_region_batch BETWEEN 1 AND 32);
ALTER TABLE media_state ADD COLUMN ocr_dense_paused INTEGER NOT NULL DEFAULT 0 CHECK(ocr_dense_paused IN (0,1));
UPDATE chats SET ocr_dense_paused=ocr_paused;
UPDATE media_state SET ocr_dense_paused=ocr_paused;
CREATE TABLE ocr_queue_version(id INTEGER PRIMARY KEY CHECK(id=1),version TEXT NOT NULL);
CREATE TABLE ocr_work (
    sha256 TEXT NOT NULL REFERENCES media_blobs(sha256) ON DELETE CASCADE,
    version TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','running','done','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    claim_token TEXT,
    PRIMARY KEY(version,sha256)
);
CREATE INDEX ocr_work_pending ON ocr_work(version,state,sha256);
CREATE TRIGGER ocr_work_ref_insert AFTER INSERT ON media_refs
WHEN new.kind='photo' AND new.status='ready' AND new.sha256 IS NOT NULL BEGIN
    INSERT OR IGNORE INTO ocr_work(sha256,version,state)
    SELECT new.sha256,v.version,COALESCE((SELECT CASE o.state WHEN 'ready' THEN 'done' ELSE 'failed' END
        FROM ocr_cache o WHERE o.sha256=new.sha256 AND o.version=v.version),'pending')
    FROM ocr_queue_version v WHERE v.id=1 ON CONFLICT(version,sha256) DO NOTHING;
END;
CREATE TRIGGER ocr_work_ref_update AFTER UPDATE OF sha256,status,kind ON media_refs
WHEN new.kind='photo' AND new.status='ready' AND new.sha256 IS NOT NULL BEGIN
    INSERT OR IGNORE INTO ocr_work(sha256,version,state)
    SELECT new.sha256,v.version,COALESCE((SELECT CASE o.state WHEN 'ready' THEN 'done' ELSE 'failed' END
        FROM ocr_cache o WHERE o.sha256=new.sha256 AND o.version=v.version),'pending')
    FROM ocr_queue_version v WHERE v.id=1 ON CONFLICT(version,sha256) DO NOTHING;
END;
CREATE TRIGGER ocr_work_cache_insert AFTER INSERT ON ocr_cache BEGIN
    UPDATE ocr_work SET state=CASE new.state WHEN 'ready' THEN 'done' ELSE 'failed' END
    WHERE sha256=new.sha256 AND version=new.version;
END;
CREATE TRIGGER ocr_work_cache_update AFTER UPDATE OF state ON ocr_cache BEGIN
    UPDATE ocr_work SET state=CASE new.state WHEN 'ready' THEN 'done' ELSE 'failed' END
    WHERE sha256=new.sha256 AND version=new.version;
END;
CREATE TRIGGER ocr_work_cache_delete AFTER DELETE ON ocr_cache BEGIN
    UPDATE ocr_work SET state='pending',claim_token=NULL WHERE sha256=old.sha256 AND version=old.version;
END;
INSERT INTO schema_migrations(version) VALUES(7);
