"""Exact chunk/batch accounting without inference or canonical database writes."""

import sqlite3
from dataclasses import dataclass

from telegram_search.search.chunking.noise.messages import message_form

DRY_RUN_VERSION = "real-episode-token-batches-v1"


@dataclass
class ChunkMetrics:
    batch_size: int
    chunks: int = 0
    tokens: int = 0
    batches: int = 0
    padded_token_slots: int = 0
    noise_only_chunks: int = 0
    _day: str | None = None
    _size: int = 0
    _maximum: int = 0

    def flush(self):
        if self._size:
            self.batches += 1
            self.padded_token_slots += self._size * self._maximum
        self._size = self._maximum = 0

    def add(self, chunk, noise_forms, lookup):
        # SegmentWorker embeds ordinal batches independently in each UTC day.
        if self._day != chunk.day:
            self.flush()
            self._day = chunk.day
        self.chunks += 1
        self.tokens += chunk.tokens
        self._size += 1
        self._maximum = max(self._maximum, chunk.tokens)
        if self._size == self.batch_size:
            self.flush()
        noise_only = bool(chunk.parts)
        for part in chunk.parts:
            if part.role == "borrowed_context":
                noise_only = False
                break
            message = lookup(part.message_id)
            if (
                message is None
                or message_form(message.text[part.start : part.end]) not in noise_forms
            ):
                noise_only = False
                break
        self.noise_only_chunks += int(noise_only)

    def result(self):
        self.flush()
        return {
            "chunks": self.chunks,
            "tokens": self.tokens,
            "batches": self.batches,
            "padded_token_slots": self.padded_token_slots,
            "padding_tokens": self.padded_token_slots - self.tokens,
            # A diagnostic: all model parts are exact whole noise forms,
            # without borrowed context. Not an estimate of removable vectors.
            "noise_only_chunks": self.noise_only_chunks,
        }


class OwnedMessages:
    """Disk-backed distinct IDs; overlap/long splits do not inflate source counts.

    The unnamed private SQLite database is removed on close. It contains only
    integer IDs, never texts. It cannot contend with the canonical writer.
    """

    def __init__(self, interrupted=lambda: False):
        self.conn = sqlite3.connect("")
        try:
            self.conn.execute("PRAGMA page_size=4096")
            self.conn.execute("PRAGMA max_page_count=32768")
            self.conn.execute("PRAGMA cache_size=-2048")
            self.conn.execute("PRAGMA temp_store=FILE")
            self.conn.execute("CREATE TABLE owned(side INTEGER,id INTEGER,PRIMARY KEY(side,id))")
            self.conn.set_progress_handler(lambda: int(interrupted()), 1000)
        except BaseException:
            self.conn.close()
            raise

    def add(self, side, parts):
        self.conn.executemany(
            "INSERT OR IGNORE INTO owned VALUES(?,?)",
            [(side, part.message_id) for part in parts if part.role != "borrowed_context"],
        )

    def count(self, side):
        return self.conn.execute("SELECT count(*) FROM owned WHERE side=?", (side,)).fetchone()[0]

    def differences(self):
        def difference(first, second):
            return self.conn.execute(
                "SELECT count(*) FROM (SELECT id FROM owned WHERE side=? EXCEPT "
                "SELECT id FROM owned WHERE side=?)",
                (first, second),
            ).fetchone()[0]

        return {"removed_own_messages": difference(0, 1), "restored_own_messages": difference(1, 0)}

    def close(self):
        self.conn.close()
