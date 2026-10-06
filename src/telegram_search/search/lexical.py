import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

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

    def sql(self, alias: str = "m") -> tuple[str, list]:
        clauses, params = [], []
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
            (not self.chat_ids or message["chat_id"] in self.chat_ids)
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
            if not anchor:
                raise UserError("Сообщение не найдено.")
            previous = conn.execute(
                "SELECT * FROM messages WHERE chat_id=? AND (timestamp,message_id)<(?,?) "
                "ORDER BY timestamp DESC,message_id DESC LIMIT ?",
                (chat_id, anchor["timestamp"], message_id, before),
            ).fetchall()
            following = conn.execute(
                "SELECT * FROM messages WHERE chat_id=? AND (timestamp,message_id)>(?,?) "
                "ORDER BY timestamp,message_id LIMIT ?",
                (chat_id, anchor["timestamp"], message_id, after),
            ).fetchall()
            return [
                self.serialize_message(conn, row, filters)
                for row in [*reversed(previous), anchor, *following]
            ]

    def get_chunk_context(self, conn, chunk_id, filters):
        """Show every matched original message plus one neighbour on each side."""
        matched = conn.execute(
            "SELECT DISTINCT m.* FROM chunk_parts p JOIN messages m "
            "ON m.chat_id=p.chat_id AND m.message_id=p.message_id "
            "WHERE p.chunk_id=? ORDER BY m.timestamp,m.message_id",
            (chunk_id,),
        ).fetchall()
        if not matched:
            return []
        first, last = matched[0], matched[-1]
        before = conn.execute(
            "SELECT * FROM messages WHERE chat_id=? AND (timestamp,message_id)<(?,?) "
            "ORDER BY timestamp DESC,message_id DESC LIMIT 1",
            (first["chat_id"], first["timestamp"], first["message_id"]),
        ).fetchall()
        after = conn.execute(
            "SELECT * FROM messages WHERE chat_id=? AND (timestamp,message_id)>(?,?) "
            "ORDER BY timestamp,message_id LIMIT 1",
            (last["chat_id"], last["timestamp"], last["message_id"]),
        ).fetchall()
        return [self.serialize_message(conn, row, filters) for row in [*before, *matched, *after]]


class SearchService:
    def __init__(self, db: Database):
        self.db = db
        self.context = ContextService(db)

    def search(
        self, query: str, filters: Filters | None = None, exact: bool = False, limit: int = 20
    ) -> dict:
        if not 1 <= limit <= 100:
            raise UserError("Лимит поиска должен быть от 1 до 100.")
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
                "FROM message_fts JOIN messages m ON m.rowid=message_fts.rowid "
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
                context = self.context.get_context(
                    row["chat_id"], row["message_id"], before=2, after=3, filters=filters
                )
                seen.update((row["chat_id"], item["message_id"]) for item in context)
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
