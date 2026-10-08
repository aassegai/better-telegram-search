import hashlib

from telegram_search.shared.text import serialize


def message_version(conn, chat_id: str, message_id: int) -> str | None:
    row = conn.execute(
        "SELECT content_hash,edited_timestamp FROM messages WHERE chat_id=? AND message_id=?",
        (chat_id, message_id),
    ).fetchone()
    if row is None:
        tombstone = conn.execute(
            "SELECT observed_at,policy FROM telegram_tombstones WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        ).fetchone()
        return (
            hashlib.sha256(serialize([chat_id, message_id, list(tombstone)]).encode()).hexdigest()
            if tombstone
            else None
        )
    known_media = sorted(
        {
            (ref["kind"], ref["sha256"])
            for ref in conn.execute(
                "SELECT kind,sha256 FROM media_refs WHERE chat_id=? AND message_id=? "
                "AND sha256 IS NOT NULL",
                (chat_id, message_id),
            )
        }
    )
    return hashlib.sha256(serialize([row[0], row[1], known_media]).encode()).hexdigest()
