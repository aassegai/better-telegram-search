"""Bounded canonical source/context reads for a CPU-only chunk comparison."""

import sqlite3
import time
from collections import OrderedDict

from telegram_search.search.chunking.legacy import SourceMessage
from telegram_search.search.chunking.noise.statistics import MAX_METADATA_CHARS, MAX_TEXT_CHARS
from telegram_search.storage.contention import database_busy


class DryRunLimited(Exception):
    def __init__(self, reason):
        self.reason = reason


# Keep the worker's source semantics, including service/deleted rows. All JSON
# evaluation is guarded; oversized inputs stop the comparison, never truncate it.
FIELDS = f"""
substr(m.chat_id,1,257) AS chat_id,m.message_id,m.timestamp,
substr(m.author,1,1025) AS author,
substr(m.text,1,{MAX_TEXT_CHARS + 1}) AS text,m.has_photo,substr(m.kind,1,65) AS kind,
m.remote_deleted,substr(m.author_id,1,1025) AS author_id,m.reply_to,
substr(m.content_hash,1,129) AS content_hash,
length(m.raw_json)>{MAX_METADATA_CHARS} AS metadata_limited,
CASE WHEN length(m.raw_json)>{MAX_METADATA_CHARS} THEN 0 ELSE
EXISTS(SELECT 1 FROM media_refs r WHERE r.chat_id=m.chat_id AND r.message_id=m.message_id)
OR json_extract(m.raw_json,'$.media_type') IS NOT NULL
OR json_extract(m.raw_json,'$.file') IS NOT NULL END AS has_attachment,
CASE WHEN length(m.raw_json)>{MAX_METADATA_CHARS} THEN 0 ELSE
EXISTS(SELECT 1 FROM json_each(m.raw_json,'$.text_entities') e WHERE
json_extract(CASE WHEN e.type='object' THEN e.value ELSE '{{}}' END,'$.type')
IN ('mention','text_mention','text_link','code','pre','link')) END AS protected
"""


class NoiseSource:
    def __init__(self, db, chat_id, guard, check, interrupted):
        self.db, self.chat_id = db, chat_id
        self.guard, self.check, self.interrupted = guard, check, interrupted
        self.cache, self.cache_bytes = OrderedDict(), 0
        self.scanned, self.limited = 0, False

    def _read(self, clause, params, *, guarded=False):
        while True:
            self.check()
            try:
                with self.db.connection_timeout(0.2), self.db.connect() as conn:
                    conn.set_progress_handler(lambda: int(self.interrupted()), 1000)
                    if guarded:
                        conn.execute("BEGIN")
                        self.guard(conn)
                    rows = conn.execute(
                        "SELECT "
                        + FIELDS
                        + " FROM indexable_messages m WHERE m.chat_id=? "
                        + clause,
                        (self.chat_id, *params),
                    ).fetchall()
                # Canonical read transactions end before validation/tokenization.
                self.check()
                return [self._convert(row) for row in rows]
            except sqlite3.Error as exc:
                self.check()
                if not database_busy(exc):
                    raise
                # A concurrent writer cannot turn a transient read into a dead
                # worker; the global comparison deadline bounds these retries.
                time.sleep(0.05)

    @staticmethod
    def _convert(row):
        values = dict(row)
        limited = values.pop("metadata_limited")
        if limited or any(
            len(values[field] or "") > limit
            for field, limit in (
                ("text", MAX_TEXT_CHARS),
                ("author", 1024),
                ("author_id", 1024),
                ("chat_id", 256),
                ("kind", 64),
                ("content_hash", 128),
            )
        ):
            raise DryRunLimited("source_length_limit")
        return SourceMessage(**values)

    def _remember(self, message_id, value):
        weight = 128 + (
            sum(len(field.encode()) for field in vars(value).values() if isinstance(field, str))
            if value
            else 0
        )
        previous = self.cache.pop(message_id, None)
        if previous:
            self.cache_bytes -= previous[1]
        self.cache[message_id] = value, weight
        self.cache_bytes += weight
        while len(self.cache) > 512 or self.cache_bytes > 4 * 1024**2:
            _, (_, removed) = self.cache.popitem(last=False)
            self.cache_bytes -= removed
        return value

    def own(self, message_id):
        self.check()
        cached = self.cache.get(message_id)
        if cached is not None:
            self.cache.move_to_end(message_id)
            return cached[0]
        rows = self._read("AND m.message_id=?", (message_id,))
        return self._remember(message_id, rows[0] if rows else None)

    def lookup(self, chat_id, message_id):
        if chat_id != self.chat_id:
            return None
        value = self.own(message_id)
        return value if value and not value.remote_deleted else None

    def neighbor(self, message, seconds):
        rows = self._read(
            "AND m.remote_deleted=0 AND m.kind='message' AND length(trim(m.text))>0 "
            "AND m.timestamp>=? AND (m.timestamp,m.message_id)<(?,?) "
            "ORDER BY m.timestamp DESC,m.message_id DESC LIMIT 1",
            (message.timestamp - seconds, message.timestamp, message.message_id),
        )
        return self._remember(rows[0].message_id, rows[0]) if rows else None

    def messages(self, maximum):
        cursor = (-(2**63), -(2**63))
        self.scanned, self.limited = 0, False
        while self.scanned < maximum:
            rows = self._read(
                "AND (m.timestamp,m.message_id)>(?,?) ORDER BY m.timestamp,m.message_id LIMIT ?",
                (*cursor, min(128, maximum - self.scanned)),
                guarded=True,
            )
            if not rows:
                return
            for message in rows:
                self.check()
                self._remember(message.message_id, message)
                self.scanned += 1
                yield message
            cursor = rows[-1].timestamp, rows[-1].message_id
        # Peek only at existence: a large next message outside the measured
        # prefix must not invalidate the successfully measured sample.
        while True:
            self.check()
            try:
                with self.db.connection_timeout(0.2), self.db.connect() as conn:
                    conn.execute("BEGIN")
                    self.guard(conn)
                    self.limited = (
                        conn.execute(
                            "SELECT 1 FROM indexable_messages WHERE chat_id=? AND "
                            "(timestamp,message_id)>(?,?) LIMIT 1",
                            (self.chat_id, *cursor),
                        ).fetchone()
                        is not None
                    )
                return
            except sqlite3.Error as exc:
                self.check()
                if not database_busy(exc):
                    raise
                time.sleep(0.05)
