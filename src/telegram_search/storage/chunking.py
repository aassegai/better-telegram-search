"""Default document rules with durable compatibility for existing checkpoints."""

from telegram_search.search.chunking.policy import DEFAULT_POLICY


def chat_policy(conn, chat_id):
    selected = conn.execute(
        "SELECT policy_id FROM chat_chunking WHERE chat_id=?", (chat_id,)
    ).fetchone()
    if selected:
        return selected[0]
    # Before the default changed, new chats could have legacy work without a
    # chat_chunking row. Keep its frozen policy when resuming or adding days.
    existing = conn.execute(
        "SELECT chunking_policy_id FROM index_segments WHERE chat_id=? ORDER BY utc_day LIMIT 1",
        (chat_id,),
    ).fetchone()
    return existing[0] if existing else DEFAULT_POLICY.identity


def ensure_chat_policy(conn, chat_id):
    policy_id = chat_policy(conn, chat_id)
    if policy_id == DEFAULT_POLICY.identity:
        conn.execute(
            "INSERT OR IGNORE INTO chunking_policies VALUES(?,?)",
            (DEFAULT_POLICY.identity, DEFAULT_POLICY.json),
        )
    conn.execute("INSERT OR IGNORE INTO chat_chunking VALUES(?,?)", (chat_id, policy_id))
    return policy_id


def reset_for_rebuild(conn, chat_id=None):
    """Upgrade legacy rules, preserving an explicitly frozen exact noise policy."""
    conn.execute(
        "INSERT OR IGNORE INTO chunking_policies VALUES(?,?)",
        (DEFAULT_POLICY.identity, DEFAULT_POLICY.json),
    )
    conn.execute(
        "INSERT INTO chat_chunking(chat_id,policy_id) SELECT id,? FROM chats WHERE "
        + ("id=? " if chat_id is not None else "1 ")
        + "AND NOT EXISTS (SELECT 1 FROM chat_chunking selected "
        "JOIN chunking_policies p ON p.id=selected.policy_id "
        "WHERE selected.chat_id=chats.id "
        "AND json_extract(p.policy_json,'$.version')='telegram-episodes-noise-v3') "
        + "ON CONFLICT(chat_id) DO UPDATE SET policy_id=excluded.policy_id",
        (DEFAULT_POLICY.identity, chat_id) if chat_id is not None else (DEFAULT_POLICY.identity,),
    )
    conn.execute(
        "UPDATE chats SET revision=revision+1" + (" WHERE id=?" if chat_id is not None else ""),
        (chat_id,) if chat_id is not None else (),
    )
