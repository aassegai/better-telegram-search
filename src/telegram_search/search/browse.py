"""Bounded keyset pages over canonical messages, independent of every model."""

import base64
import json

from telegram_search.search.execution import advance, checkpoint, phase
from telegram_search.search.lexical import ContextService
from telegram_search.shared.errors import UserError


class BrowseService:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def cursor_for(spec, epoch, message):
        return base64.urlsafe_b64encode(
            json.dumps(
                {
                    "spec": spec.identity,
                    "epoch": epoch,
                    "position": [message["timestamp"], message["chat_id"], message["message_id"]],
                },
                separators=(",", ":"),
            ).encode()
        ).decode()

    def search(self, spec, *, cursor=None, limit=None):
        if not spec.browsing:
            raise UserError("Просмотр по фильтрам требует пустого поискового запроса.")
        limit = spec.page_size if limit is None else limit
        if type(limit) is not int or not 1 <= limit <= 100:
            raise UserError("Размер страницы должен быть от 1 до 100 сообщений.")
        filters = spec.filters()
        where, parameters = filters.sql()
        operator, direction = ("<", "DESC") if spec.sort == "newest" else (">", "ASC")
        phase("browse", unit="messages")
        with self.db.connect() as conn:
            conn.execute("BEGIN")
            epoch = conn.execute(
                "SELECT generation FROM search_archive_epoch WHERE id=1"
            ).fetchone()[0]
            if cursor:
                try:
                    if len(cursor) > 4096:
                        raise ValueError("cursor size")
                    value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                    stamp, chat, mid = value["position"]
                    if (
                        value["spec"] != spec.identity
                        or value["epoch"] != epoch
                        or type(stamp) is not int
                        or type(mid) is not int
                        or type(chat) is not str
                        or not 1 <= len(chat) <= 256
                        or not -(2**63) <= stamp < 2**63
                        or not -(2**63) <= mid < 2**63
                    ):
                        raise ValueError("cursor scope")
                except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                    raise UserError(
                        "Страница просмотра устарела. Покажите сообщения заново."
                    ) from exc
                where += f" AND (m.timestamp,m.chat_id,m.message_id){operator}(?,?,?)"
                parameters += [stamp, chat, mid]
            rows = conn.execute(
                "SELECT m.*,c.name AS chat_name FROM indexable_messages m "
                "JOIN chats c ON c.id=m.chat_id WHERE "
                + where
                + f" ORDER BY m.timestamp {direction},m.chat_id {direction},"
                + f"m.message_id {direction} LIMIT ?",
                [*parameters, limit + 1],
            ).fetchall()
            results = []
            for row in rows[:limit]:
                checkpoint()
                results.append(
                    {
                        "chat_id": row["chat_id"],
                        "chat_name": row["chat_name"],
                        "message_id": row["message_id"],
                        "timestamp": row["timestamp"],
                        "matched_by": [],
                        "result_type": "message",
                        "messages": [ContextService.serialize_message(conn, row, filters)],
                    }
                )
                advance()
            following = None
            if len(rows) > limit:
                last = rows[limit - 1]
                following = self.cursor_for(spec, epoch, last)
        return {
            "results": results,
            "has_more": following is not None,
            "browse_cursor": following,
            "backend": "messages",
            "effective_mode": "browse",
            "effective_strategy": "none",
            "warnings": [],
        }
