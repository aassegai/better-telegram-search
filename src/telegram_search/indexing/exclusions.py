"""Per-chat author exclusions; canonical messages and shared media caches survive."""

from telegram_search.shared.errors import UserError
from telegram_search.storage.generations import bump_revision, invalidate_segments, utc_day


def validate_authors(values):
    if (
        not isinstance(values, list)
        or len(values) > 500
        or any(
            type(value) is not str or not 1 <= len(value) <= 128 or "\0" in value
            for value in values
        )
    ):
        raise UserError("Выберите не более 500 авторов для исключения из индексации.")
    return set(values)


def update_authors(conn, chat_id, selected):
    previous = {
        row[0]
        for row in conn.execute(
            "SELECT author_id FROM index_excluded_authors WHERE chat_id=?", (chat_id,)
        )
    }
    added, removed = selected - previous, previous - selected
    if not added and not removed:
        return False
    # Retain existing choices even if all of that author's messages were removed.
    for author in added:
        if not conn.execute(
            "SELECT 1 FROM messages WHERE chat_id=? AND author_id=? LIMIT 1", (chat_id, author)
        ).fetchone():
            raise UserError("Выбранный автор не найден в этом диалоге.")
    days = set()
    for author in added | removed:
        days.update(
            utc_day(row[0])
            for row in conn.execute(
                "SELECT DISTINCT timestamp FROM messages WHERE chat_id=? AND author_id=?",
                (chat_id, author),
            )
        )
    conn.executemany(
        "DELETE FROM index_excluded_authors WHERE chat_id=? AND author_id=?",
        [(chat_id, author) for author in removed],
    )
    conn.executemany(
        "INSERT INTO index_excluded_authors(chat_id,author_id) VALUES(?,?)",
        [(chat_id, author) for author in added],
    )
    # Supersede in-flight text work in the same transaction as the filter change.
    invalidate_segments(conn, chat_id, days, "author_exclusions")
    bump_revision(conn, chat_id)
    return True
