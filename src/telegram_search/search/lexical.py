import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from telegram_search.search.presentation import search_options
from telegram_search.search.stopwords import keyword_terms
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import normalize_text
from telegram_search.storage.database import Database


@dataclass
class Filters:
    chat_ids: list[str] = field(default_factory=list)
    author_ids: list[str] = field(default_factory=list)
    date_from: int | None = None
    date_to: int | None = None
    content_type: str = "all"
    exclude_deleted: bool = False

    def sql(self, alias: str = "m") -> tuple[str, list]:
        clauses, params = [], []
        if self.exclude_deleted:
            clauses.append(f"{alias}.remote_deleted=0")
        for column, values in (("chat_id", self.chat_ids), ("author_id", self.author_ids)):
            if values:
                clauses.append(f"{alias}.{column} IN ({','.join('?' for _ in values)})")
                params.extend(values)
        if self.date_from is not None:
            clauses.append(f"{alias}.timestamp>=?")
            params.append(self.date_from)
        if self.date_to is not None:
            clauses.append(f"{alias}.timestamp<?")
            params.append(self.date_to)
        if self.content_type == "photo":
            clauses.append(f"{alias}.has_photo=1")
        elif self.content_type == "text":
            clauses.append(f"{alias}.text<>'' AND {alias}.kind='message'")
        elif self.content_type == "service":
            clauses.append(f"{alias}.kind='service'")
        elif self.content_type != "all":
            raise UserError("Неизвестный фильтр содержимого.")
        return " AND ".join(clauses) or "1", params

    def matches(self, message: dict) -> bool:
        return (
            (not self.exclude_deleted or not message.get("remote_deleted", False))
            and (not self.chat_ids or message["chat_id"] in self.chat_ids)
            and (not self.author_ids or message["author_id"] in self.author_ids)
            and (self.date_from is None or message["timestamp"] >= self.date_from)
            and (self.date_to is None or message["timestamp"] < self.date_to)
            and (self.content_type != "photo" or bool(message["has_photo"]))
            and (
                self.content_type != "text"
                or (bool(message["text"]) and message["kind"] == "message")
            )
            and (self.content_type != "service" or message["kind"] == "service")
        )


def date_bound(value: str | None, end: bool = False) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        result = int(dt.timestamp())
        return result + (86400 if end and len(value) == 10 else 0)
    except ValueError as exc:
        raise UserError("Неверная дата фильтра. Используйте ISO 8601.") from exc


def fts_query(query: str, exact: bool) -> str | None:
    normalized = normalize_text(query.strip())
    words = re.findall(r"[^\W_]+", normalized, re.UNICODE)
    if not words:
        return None
    if len(words) > 100:
        raise UserError("Слишком много слов в запросе.")
    if exact:
        return '"' + normalized.replace('"', '""') + '"'
    words = keyword_terms(normalized)
    if not words:
        return None
    # Treat every term as literal text; never accept FTS operators from the input.
    return " AND ".join('"' + word + '"' for word in words)


class ContextService:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def serialize_message(
        conn: sqlite3.Connection, row: sqlite3.Row, filters: Filters | None = None
    ) -> dict:
        message = dict(row)
        original = json.loads(message.pop("raw_json"))
        message.pop("content_hash", None)
        message.pop("text_normalized", None)
        message.pop("rowid", None)
        message["entities"] = original.get("text_entities", [])
        message["forwarded_from"] = original.get("forwarded_from")
        message["action"] = original.get("action")
        refs = conn.execute(
            "SELECT id,kind,status FROM media_refs WHERE chat_id=? AND message_id=? "
            "ORDER BY status='ready' DESC,id DESC",
            (row["chat_id"], row["message_id"]),
        ).fetchall()
        # Multiple export roots are alternative locations, not duplicate attachments.
        chosen = {}
        for ref in refs:
            chosen.setdefault(ref["kind"], dict(ref))
        message["media"] = list(chosen.values())
        message["matches_filters"] = filters.matches(message) if filters else True
        return message

    def get_context(
        self,
        chat_id: str,
        message_id: int,
        before: int = 3,
        after: int = 4,
        filters: Filters | None = None,
    ) -> list[dict]:
        if not 0 <= before <= 100 or not 0 <= after <= 100:
            raise UserError("Размер окна должен быть от 0 до 100 сообщений с каждой стороны.")
        with self.db.connect() as conn:
            anchor = conn.execute(
                "SELECT * FROM messages WHERE chat_id=? AND message_id=?", (chat_id, message_id)
            ).fetchone()
            if not anchor or (filters and filters.exclude_deleted and anchor["remote_deleted"]):
                raise UserError("Сообщение не найдено.")
            visible = " AND remote_deleted=0" if filters and filters.exclude_deleted else ""
            previous = conn.execute(
                "SELECT * FROM messages WHERE chat_id=? AND (timestamp,message_id)<(?,?) "
                f"{visible} ORDER BY timestamp DESC,message_id DESC LIMIT ?",
                (chat_id, anchor["timestamp"], message_id, before),
            ).fetchall()
            following = conn.execute(
                "SELECT * FROM messages WHERE chat_id=? AND (timestamp,message_id)>(?,?) "
                f"{visible} ORDER BY timestamp,message_id LIMIT ?",
                (chat_id, anchor["timestamp"], message_id, after),
            ).fetchall()
            return [
                self.serialize_message(conn, row, filters)
                for row in [*reversed(previous), anchor, *following]
            ]

    def get_result_context(self, conn, anchor, size, filters):
        """Keep the anchor and fill a chronological window, including at chat boundaries."""
        visible = " AND remote_deleted=0" if filters and filters.exclude_deleted else ""
        previous = conn.execute(
            "SELECT * FROM indexable_messages WHERE chat_id=? AND (timestamp,message_id)<(?,?) "
            f"{visible} ORDER BY timestamp DESC,message_id DESC LIMIT ?",
            (anchor["chat_id"], anchor["timestamp"], anchor["message_id"], size - 1),
        ).fetchall()
        following = conn.execute(
            "SELECT * FROM indexable_messages WHERE chat_id=? AND (timestamp,message_id)>(?,?) "
            f"{visible} ORDER BY timestamp,message_id LIMIT ?",
            (anchor["chat_id"], anchor["timestamp"], anchor["message_id"], size - 1),
        ).fetchall()
        before = min((size - 1) // 2, len(previous))
        after = min(size - 1 - before, len(following))
        before = min(size - 1 - after, len(previous))
        rows = [*reversed(previous[:before]), anchor, *following[:after]]
        return [self.serialize_message(conn, row, filters) for row in rows]


class SearchService:
    def __init__(self, db: Database):
        self.db = db
        self.context = ContextService(db)

    def search(
        self,
        query: str,
        filters: Filters | None = None,
        exact: bool = False,
        limit: int | None = None,
        chunk_size: int | None = None,
    ) -> dict:
        limit, chunk_size = search_options(self.db.settings, limit, chunk_size)
        filters = filters or Filters()
        where, params = filters.sql()
        match = fts_query(query, exact)
        if match is None:
            return {"results": [], "backend": "bm25_messages", "has_more": False}
        if exact:
            where += " AND instr(m.text_normalized,?)>0"
            params.append(normalize_text(query.strip()))
        results = []
        seen = set()
        has_more = False
        with self.db.connect() as conn:
            # Stream ranked rows until enough distinct windows survive deduplication.
            rows = conn.execute(
                "SELECT m.*, c.name AS chat_name,bm25(message_fts) AS lexical_score "
                "FROM message_fts JOIN indexable_messages m ON m.rowid=message_fts.rowid "
                "JOIN chats c ON c.id=m.chat_id WHERE message_fts MATCH ? "
                f"AND {where} ORDER BY lexical_score,m.timestamp,m.message_id",
                (match, *params),
            )
            for row in rows:
                if (row["chat_id"], row["message_id"]) in seen:
                    continue
                if len(results) == limit:
                    has_more = True
                    break
                context = self.context.get_result_context(conn, row, chunk_size, filters)
                seen.add((row["chat_id"], row["message_id"]))
                results.append(
                    {
                        "chat_id": row["chat_id"],
                        "chat_name": row["chat_name"],
                        "message_id": row["message_id"],
                        "timestamp": row["timestamp"],
                        "lexical_score": row["lexical_score"],
                        "matched_by": ["words"],
                        "messages": context,
                    }
                )
        return {"results": results, "backend": "bm25_messages", "has_more": has_more}

    def chats(self) -> list[dict]:
        with self.db.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT c.*,COUNT(m.rowid) AS messages,COALESCE(SUM(m.has_photo),0) AS photos,"
                    "MIN(m.timestamp) AS date_from,MAX(m.timestamp) AS date_to "
                    "FROM chats c LEFT JOIN messages m ON m.chat_id=c.id GROUP BY c.id "
                    "ORDER BY c.created_at,c.id"
                )
            ]

    def authors(self, chat_ids: list[str] | None = None) -> list[dict]:
        where, params = Filters(chat_ids=chat_ids or []).sql()
        with self.db.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT author_id,MAX(author) AS name,COUNT(*) AS messages FROM messages m "
                    f"WHERE {where} AND author_id IS NOT NULL GROUP BY author_id ORDER BY name",
                    params,
                )
            ]
