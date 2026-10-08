CREATE TABLE index_excluded_authors (
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    author_id TEXT NOT NULL,
    PRIMARY KEY(chat_id,author_id)
);
CREATE VIEW indexable_messages AS
SELECT m.rowid AS rowid,m.* FROM messages m WHERE NOT EXISTS (
    SELECT 1 FROM index_excluded_authors x WHERE x.chat_id=m.chat_id AND x.author_id=m.author_id
);
CREATE VIEW indexable_media_refs AS
SELECT r.* FROM media_refs r JOIN indexable_messages m
ON m.chat_id=r.chat_id AND m.message_id=r.message_id;

-- Keep the archive intact; keyword statistics and rebuilds use only eligible messages.
DROP TRIGGER messages_ai;
DROP TRIGGER messages_ad;
DROP TRIGGER messages_au;
DROP TABLE message_fts;
CREATE VIRTUAL TABLE message_fts USING fts5(
    text_normalized, content='indexable_messages', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 0'
);
CREATE TRIGGER messages_ai AFTER INSERT ON messages
WHEN NOT EXISTS (SELECT 1 FROM index_excluded_authors x WHERE x.chat_id=new.chat_id AND x.author_id=new.author_id) BEGIN
    INSERT INTO message_fts(rowid,text_normalized) VALUES(new.rowid,new.text_normalized);
END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages
WHEN NOT EXISTS (SELECT 1 FROM index_excluded_authors x WHERE x.chat_id=old.chat_id AND x.author_id=old.author_id) BEGIN
    INSERT INTO message_fts(message_fts,rowid,text_normalized) VALUES('delete',old.rowid,old.text_normalized);
END;
CREATE TRIGGER messages_au AFTER UPDATE OF text_normalized,author_id,chat_id ON messages BEGIN
    INSERT INTO message_fts(message_fts,rowid,text_normalized)
    SELECT 'delete',old.rowid,old.text_normalized WHERE NOT EXISTS (
        SELECT 1 FROM index_excluded_authors x WHERE x.chat_id=old.chat_id AND x.author_id=old.author_id
    );
    INSERT INTO message_fts(rowid,text_normalized)
    SELECT new.rowid,new.text_normalized WHERE NOT EXISTS (
        SELECT 1 FROM index_excluded_authors x WHERE x.chat_id=new.chat_id AND x.author_id=new.author_id
    );
END;
CREATE TRIGGER index_excluded_authors_ai AFTER INSERT ON index_excluded_authors BEGIN
    INSERT INTO message_fts(message_fts,rowid,text_normalized)
    SELECT 'delete',m.rowid,m.text_normalized FROM messages m
    WHERE m.chat_id=new.chat_id AND m.author_id=new.author_id;
END;
CREATE TRIGGER index_excluded_authors_ad AFTER DELETE ON index_excluded_authors BEGIN
    INSERT INTO message_fts(rowid,text_normalized)
    SELECT m.rowid,m.text_normalized FROM messages m
    WHERE m.chat_id=old.chat_id AND m.author_id=old.author_id;
END;
INSERT INTO message_fts(message_fts) VALUES('rebuild');

-- A persistent epoch prevents stale pages after delete + reimport of the same chat ID.
CREATE TABLE search_archive_epoch (
    id INTEGER PRIMARY KEY CHECK(id=1),
    generation INTEGER NOT NULL DEFAULT 0
);
INSERT INTO search_archive_epoch(id) VALUES(1);
CREATE TRIGGER chats_search_ai AFTER INSERT ON chats BEGIN
    UPDATE search_archive_epoch SET generation=generation+1 WHERE id=1;
END;
CREATE TRIGGER chats_search_ad AFTER DELETE ON chats BEGIN
    UPDATE search_archive_epoch SET generation=generation+1 WHERE id=1;
END;
CREATE TRIGGER chats_search_au AFTER UPDATE OF revision ON chats
WHEN old.revision<>new.revision BEGIN
    UPDATE search_archive_epoch SET generation=generation+1 WHERE id=1;
END;

INSERT INTO schema_migrations(version) VALUES(10);
