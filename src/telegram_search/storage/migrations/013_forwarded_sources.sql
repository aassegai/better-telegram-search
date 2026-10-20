-- Derive the source from canonical export data for both existing and new messages.
-- A virtual column avoids rewriting messages or invalidating their vector indexes.
ALTER TABLE messages ADD COLUMN forwarded_from TEXT GENERATED ALWAYS AS (
    CASE WHEN json_type(raw_json,'$.forwarded_from')='text'
    THEN nullif(json_extract(raw_json,'$.forwarded_from'),'') END
) VIRTUAL;
CREATE INDEX messages_forwarded_source ON messages(forwarded_from,chat_id)
    WHERE forwarded_from IS NOT NULL;
CREATE INDEX messages_chat_forwarded_source ON messages(chat_id,forwarded_from)
    WHERE forwarded_from IS NOT NULL;
INSERT INTO schema_migrations(version) VALUES(13);
