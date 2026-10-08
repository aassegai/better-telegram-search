-- Legacy rows/checkpoints retain their exact text, ranges, identity and ordinals.
CREATE TABLE chunking_policies (
    id TEXT PRIMARY KEY,
    policy_json TEXT NOT NULL
);
INSERT INTO chunking_policies VALUES('telegram-windows-v1','{"version":"telegram-windows-v1"}');
CREATE TABLE chat_chunking (
    chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
    policy_id TEXT NOT NULL REFERENCES chunking_policies(id)
);
INSERT INTO chat_chunking SELECT id,'telegram-windows-v1' FROM chats;
ALTER TABLE index_segments ADD COLUMN chunking_policy_id TEXT NOT NULL DEFAULT 'telegram-windows-v1';
ALTER TABLE index_work ADD COLUMN chunking_policy_id TEXT NOT NULL DEFAULT 'telegram-windows-v1';
ALTER TABLE index_work ADD COLUMN source_messages INTEGER NOT NULL DEFAULT 0;
ALTER TABLE index_work ADD COLUMN skipped_messages INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chunks ADD COLUMN chunking_policy_id TEXT NOT NULL DEFAULT 'telegram-windows-v1';
ALTER TABLE chunks ADD COLUMN lexical_text TEXT;
CREATE TABLE chunk_embedding_parts (
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    char_start INTEGER NOT NULL CHECK(char_start>=0),
    char_end INTEGER NOT NULL CHECK(char_end>=char_start),
    role TEXT NOT NULL CHECK(role IN ('core','support','borrowed_context')),
    PRIMARY KEY(chunk_id,ordinal)
);
INSERT INTO chunk_embedding_parts SELECT *, 'core' FROM chunk_parts;
CREATE INDEX embedding_parts_message ON chunk_embedding_parts(chat_id,message_id,chunk_id);
CREATE TABLE chunk_context_dependencies (
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    content_hash TEXT,
    PRIMARY KEY(chunk_id,chat_id,message_id)
);
CREATE INDEX context_dependencies_message ON chunk_context_dependencies(chat_id,message_id);
CREATE TABLE chunk_context_dirty (
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    utc_day TEXT NOT NULL,
    PRIMARY KEY(chat_id,utc_day)
);
CREATE TABLE segment_context_dependencies (
    work_id TEXT NOT NULL REFERENCES index_work(id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    content_hash TEXT,
    PRIMARY KEY(work_id,message_id)
);
CREATE INDEX segment_context_message ON segment_context_dependencies(chat_id,message_id);
CREATE TABLE segment_context_ranges (
    work_id TEXT PRIMARY KEY REFERENCES index_work(id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL,
    from_timestamp INTEGER NOT NULL,
    to_timestamp INTEGER NOT NULL
);
CREATE VIEW chunk_context_owners AS
    SELECT c.chat_id AS owner_chat,c.utc_day AS owner_day,d.chat_id,d.message_id
    FROM chunk_context_dependencies d JOIN chunks c ON c.id=d.chunk_id
    JOIN index_segments s ON s.chat_id=c.chat_id AND s.utc_day=c.utc_day
    WHERE c.generation=s.target_generation
    UNION
    SELECT w.chat_id,w.utc_day,d.chat_id,d.message_id
    FROM segment_context_dependencies d JOIN index_work w ON w.id=d.work_id
    JOIN index_segments s ON s.chat_id=w.chat_id AND s.utc_day=w.utc_day
    WHERE w.generation=s.target_generation;
CREATE VIEW chunk_context_intervals AS
    SELECT w.chat_id AS owner_chat,w.utc_day AS owner_day,r.chat_id,
        r.from_timestamp,r.to_timestamp
    FROM segment_context_ranges r JOIN index_work w ON w.id=r.work_id
    JOIN index_segments s ON s.chat_id=w.chat_id AND s.utc_day=w.utc_day
    WHERE w.generation=s.target_generation;
CREATE TRIGGER chunk_context_message_au AFTER UPDATE OF content_hash,remote_deleted,author_id,timestamp ON messages
WHEN old.content_hash<>new.content_hash OR old.remote_deleted<>new.remote_deleted
OR old.author_id IS NOT new.author_id OR old.timestamp<>new.timestamp BEGIN
    INSERT OR IGNORE INTO chunk_context_dirty
    SELECT owner_chat,owner_day FROM chunk_context_owners
    WHERE chat_id=old.chat_id AND message_id=old.message_id
    UNION SELECT owner_chat,owner_day FROM chunk_context_intervals
    WHERE chat_id=old.chat_id AND (
        old.timestamp>=from_timestamp AND old.timestamp<to_timestamp
        OR new.timestamp>=from_timestamp AND new.timestamp<to_timestamp
    );
END;
CREATE TRIGGER chunk_context_message_ad BEFORE DELETE ON messages BEGIN
    INSERT OR IGNORE INTO chunk_context_dirty
    SELECT owner_chat,owner_day FROM chunk_context_owners
    WHERE chat_id=old.chat_id AND message_id=old.message_id
    UNION SELECT owner_chat,owner_day FROM chunk_context_intervals
    WHERE chat_id=old.chat_id AND old.timestamp>=from_timestamp AND old.timestamp<to_timestamp;
END;
CREATE TRIGGER chunk_context_message_ai AFTER INSERT ON messages BEGIN
    INSERT OR IGNORE INTO chunk_context_dirty
    SELECT owner_chat,owner_day FROM chunk_context_owners
    WHERE chat_id=new.chat_id AND message_id=new.message_id
    UNION SELECT owner_chat,owner_day FROM chunk_context_intervals
    WHERE chat_id=new.chat_id AND new.timestamp>=from_timestamp AND new.timestamp<to_timestamp;
END;
CREATE TRIGGER chunk_context_author_ai AFTER INSERT ON index_excluded_authors BEGIN
    INSERT OR IGNORE INTO chunk_context_dirty
    SELECT d.owner_chat,d.owner_day FROM chunk_context_owners d
    JOIN messages m ON m.chat_id=d.chat_id AND m.message_id=d.message_id
    WHERE m.chat_id=new.chat_id AND m.author_id=new.author_id
    UNION SELECT d.owner_chat,d.owner_day FROM chunk_context_intervals d
    JOIN messages m ON m.chat_id=d.chat_id
    AND m.timestamp>=d.from_timestamp AND m.timestamp<d.to_timestamp
    WHERE m.chat_id=new.chat_id AND m.author_id=new.author_id;
END;
CREATE TRIGGER chunk_context_author_ad AFTER DELETE ON index_excluded_authors BEGIN
    INSERT OR IGNORE INTO chunk_context_dirty
    SELECT d.owner_chat,d.owner_day FROM chunk_context_owners d
    JOIN messages m ON m.chat_id=d.chat_id AND m.message_id=d.message_id
    WHERE m.chat_id=old.chat_id AND m.author_id=old.author_id
    UNION SELECT d.owner_chat,d.owner_day FROM chunk_context_intervals d
    JOIN messages m ON m.chat_id=d.chat_id
    AND m.timestamp>=d.from_timestamp AND m.timestamp<d.to_timestamp
    WHERE m.chat_id=old.chat_id AND m.author_id=old.author_id;
END;
INSERT INTO schema_migrations VALUES(11);
