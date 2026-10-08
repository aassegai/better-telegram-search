import time
from datetime import UTC, datetime


def utc_day(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).date().isoformat()


def invalidate_segments(conn, chat_id: str, days: set[str], reason: str) -> None:
    """Publish source changes and idempotent derivative work in the same transaction."""
    from telegram_search.search.chunking.policy import LEGACY_POLICY_ID

    selected = conn.execute(
        "SELECT policy_id FROM chat_chunking WHERE chat_id=?", (chat_id,)
    ).fetchone()
    policy_id = selected[0] if selected else LEGACY_POLICY_ID
    # Dependency triggers also capture cross-day edits/deletions and exclusions.
    days = days | {
        row[0]
        for row in conn.execute(
            "SELECT utc_day FROM chunk_context_dirty WHERE chat_id=?", (chat_id,)
        )
    }
    conn.execute("DELETE FROM chunk_context_dirty WHERE chat_id=?", (chat_id,))
    for day in sorted(days):
        row = conn.execute(
            "SELECT target_generation FROM index_segments WHERE chat_id=? AND utc_day=?",
            (chat_id, day),
        ).fetchone()
        now = int(time.time())
        if reason == "telegram" and row:
            pending = conn.execute(
                "SELECT id,created_at FROM index_work WHERE chat_id=? AND utc_day=? "
                "AND generation=? AND state='pending' AND stage='building' AND chunks_done=0 "
                "AND reason='telegram' AND created_at>?",
                (chat_id, day, row[0], now - 30),
            ).fetchone()
            if pending:
                conn.execute(
                    "UPDATE index_work SET available_at=? WHERE id=?",
                    (min(pending["created_at"] + 30, now + 3), pending["id"]),
                )
                continue
        generation = row[0] + 1 if row else 1
        conn.execute(
            "INSERT INTO index_segments(chat_id,utc_day,target_generation,"
            "lexical_generation,chunking_policy_id) "
            "VALUES(?,?,?,?,?) ON CONFLICT(chat_id,utc_day) DO UPDATE SET "
            "target_generation=excluded.target_generation,lexical_generation=excluded.lexical_generation,"
            "chunking_policy_id=excluded.chunking_policy_id",
            (chat_id, day, generation, generation, policy_id),
        )
        conn.execute(
            "UPDATE index_work SET state='superseded',finished_at=? WHERE chat_id=? AND utc_day=? "
            "AND state IN ('pending','running','failed')",
            (int(time.time()), chat_id, day),
        )
        for table in ("segment_context_dependencies", "segment_context_ranges"):
            conn.execute(
                f"DELETE FROM {table} WHERE work_id IN "
                "(SELECT id FROM index_work WHERE chat_id=? AND utc_day=? AND generation<>?)",
                (chat_id, day, generation),
            )
        conn.execute(
            "INSERT INTO index_work(id,chat_id,utc_day,generation,reason,created_at,"
            "chunking_policy_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (f"{chat_id}:{day}:{generation}", chat_id, day, generation, reason, now, policy_id),
        )
        if reason == "telegram":
            conn.execute(
                "UPDATE index_work SET available_at=? WHERE id=?",
                (now + 3, f"{chat_id}:{day}:{generation}"),
            )


def bump_revision(conn, chat_id: str) -> None:
    conn.execute("UPDATE chats SET revision=revision+1 WHERE id=?", (chat_id,))
