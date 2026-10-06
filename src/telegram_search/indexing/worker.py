import time
from datetime import UTC, datetime

from telegram_search.search.chunks import ChunkBuilder, SourceMessage
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import normalize_text


class WorkInterrupted(Exception):
    pass


class SegmentWorker:
    """Durable per-day staging, idempotent vector batches and generation publication."""

    def __init__(
        self, db, encoder, vectors, lifecycle_lock, *, batch_size=4, should_stop=lambda: False
    ):
        self.db = db
        self.encoder = encoder
        self.vectors = vectors
        self.lock = lifecycle_lock
        self.batch_size = batch_size
        self.should_stop = should_stop
        self.builder = ChunkBuilder(encoder.tokenizer)

    def _current(self, conn, work) -> bool:
        row = conn.execute(
            "SELECT s.target_generation,t.active_space_id,t.paused,t.enabled,w.state "
            "FROM index_segments s JOIN index_work w ON w.chat_id=s.chat_id "
            "AND w.utc_day=s.utc_day JOIN semantic_state t ON t.id=1 WHERE w.id=?",
            (work["id"],),
        ).fetchone()
        return bool(
            row
            and row["target_generation"] == work["generation"]
            and row["active_space_id"] == self.encoder.space_id
            and row["enabled"]
            and not row["paused"]
            and row["state"] == "running"
        )

    def _guard(self, conn, work):
        if self.should_stop() or not self._current(conn, work):
            raise WorkInterrupted()

    def _build(self, work):
        start = int(datetime.fromisoformat(work["utc_day"]).replace(tzinfo=UTC).timestamp())
        with self.db.connect() as reader:
            reader.execute("BEGIN")
            # A stable canonical snapshot, streamed without raw_json or the entire day in RAM.
            rows = reader.execute(
                "SELECT chat_id,message_id,timestamp,author,text,has_photo,kind FROM messages "
                "WHERE chat_id=? AND timestamp>=? AND timestamp<? ORDER BY timestamp,message_id",
                (work["chat_id"], start, start + 86400),
            )
            messages = (SourceMessage(**dict(row)) for row in rows)
            pending = []
            for ordinal, chunk in enumerate(
                self.builder.build(messages, work["generation"], self.encoder.space_id), 1
            ):
                pending.append((ordinal, chunk))
                if len(pending) == 64:
                    self._stage(work, pending)
                    pending = []
            if pending:
                self._stage(work, pending)
        with self.lock, self.db.connect() as conn:
            self._guard(conn, work)
            totals = conn.execute(
                "SELECT COUNT(*),COALESCE(SUM(tokens),0) FROM chunks WHERE chat_id=? AND utc_day=? "
                "AND generation=? AND embedding_space_id=?",
                (work["chat_id"], work["utc_day"], work["generation"], self.encoder.space_id),
            ).fetchone()
            conn.execute(
                "UPDATE index_work SET stage='embedding',chunks_total=?,tokens_total=? WHERE id=?",
                (*totals, work["id"]),
            )

    def _stage(self, work, batch):
        with self.lock, self.db.connect() as conn:
            self._guard(conn, work)
            for ordinal, chunk in batch:
                conn.execute(
                    "INSERT INTO chunks(id,chat_id,utc_day,generation,embedding_space_id,ordinal,"
                    "text,text_normalized,tokens) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        chunk.id,
                        chunk.chat_id,
                        chunk.day,
                        chunk.generation,
                        self.encoder.space_id,
                        ordinal,
                        chunk.text,
                        normalize_text(chunk.text),
                        chunk.tokens,
                    ),
                )
                conn.executemany(
                    "INSERT INTO chunk_parts VALUES(?,?,?,?,?,?)",
                    [
                        (chunk.id, i, chunk.chat_id, part.message_id, part.start, part.end)
                        for i, part in enumerate(chunk.parts)
                    ],
                )

    def run(self, work_id: str) -> dict:
        with self.lock, self.db.connect() as conn:
            row = conn.execute("SELECT * FROM index_work WHERE id=?", (work_id,)).fetchone()
            if row is None or row["state"] not in {"pending", "failed", "running"}:
                return {"state": "unavailable"}
            work = dict(row)
            state = conn.execute("SELECT * FROM semantic_state WHERE id=1").fetchone()
            target = conn.execute(
                "SELECT target_generation FROM index_segments WHERE chat_id=? AND utc_day=?",
                (work["chat_id"], work["utc_day"]),
            ).fetchone()
            if self.should_stop() or not state["enabled"] or state["paused"]:
                return {"state": "paused"}
            if (
                not target
                or target[0] != work["generation"]
                or state["active_space_id"] != self.encoder.space_id
            ):
                conn.execute(
                    "UPDATE index_work SET state='superseded',finished_at=? WHERE id=?",
                    (int(time.time()), work_id),
                )
                return {"state": "superseded"}
            # A reimport can reuse the canonical chat ID. Drain its deletion before
            # writing any vectors for the new incarnation under the same lifecycle lock.
            if conn.execute(
                "SELECT 1 FROM vector_deletions WHERE chat_id=?", (work["chat_id"],)
            ).fetchone():
                spaces = [
                    dict(row) for row in conn.execute("SELECT id,dimension FROM embedding_spaces")
                ]
                self.vectors.delete_chat(spaces, work["chat_id"])
                conn.execute("DELETE FROM vector_deletions WHERE chat_id=?", (work["chat_id"],))
            conn.execute(
                "UPDATE index_work SET state='running',attempts=attempts+1,error=NULL,"
                "embedding_space_id=? WHERE id=?",
                (self.encoder.space_id, work_id),
            )
            if work["stage"] == "building":
                conn.execute(
                    "DELETE FROM chunks WHERE chat_id=? AND utc_day=? AND generation=?",
                    (work["chat_id"], work["utc_day"], work["generation"]),
                )
                conn.execute(
                    "UPDATE index_work SET chunks_total=0,chunks_done=0,tokens_total=0,"
                    "embedding_seconds=0 WHERE id=?",
                    (work_id,),
                )
        try:
            if work["stage"] == "building":
                self._build(work)
            while True:
                with self.db.connect() as conn:
                    self._guard(conn, work)
                    checkpoint = conn.execute(
                        "SELECT chunks_done FROM index_work WHERE id=?", (work_id,)
                    ).fetchone()[0]
                    rows = [
                        dict(row)
                        for row in conn.execute(
                            "SELECT * FROM chunks WHERE chat_id=? AND utc_day=? AND generation=? "
                            "AND embedding_space_id=? AND ordinal>? ORDER BY ordinal LIMIT ?",
                            (
                                work["chat_id"],
                                work["utc_day"],
                                work["generation"],
                                self.encoder.space_id,
                                checkpoint,
                                self.batch_size,
                            ),
                        )
                    ]
                if not rows:
                    break
                began = time.perf_counter()
                try:
                    embeddings = self.encoder.encode_text([row["text"] for row in rows], "passage")
                except Exception as exc:
                    cause = exc
                    exhausted = False
                    while cause is not None:
                        exhausted |= isinstance(cause, MemoryError) or any(
                            term in str(cause).lower()
                            for term in ("out of memory", "bad_alloc", "failed to allocate")
                        )
                        cause = cause.__cause__
                    if exhausted and self.batch_size > 1:
                        self.batch_size = max(1, self.batch_size // 2)
                        continue
                    raise
                with self.lock, self.db.connect() as conn:
                    self._guard(conn, work)
                    # If the process dies after this write, replay uses the same stable IDs.
                    self.vectors.upsert(
                        self.encoder.space_id, self.encoder.spec.dimension, rows, embeddings
                    )
                    conn.execute(
                        "UPDATE index_work SET chunks_done=?,embedding_seconds=embedding_seconds+? "
                        "WHERE id=?",
                        (rows[-1]["ordinal"], time.perf_counter() - began, work_id),
                    )
            with self.lock, self.db.connect() as conn:
                self._guard(conn, work)
                conn.execute(
                    "UPDATE index_segments SET chunk_generation=?,dense_generation=?,"
                    "embedding_space_id=? WHERE chat_id=? AND utc_day=?",
                    (
                        work["generation"],
                        work["generation"],
                        self.encoder.space_id,
                        work["chat_id"],
                        work["utc_day"],
                    ),
                )
                conn.execute(
                    "DELETE FROM chunks WHERE chat_id=? AND utc_day=? AND "
                    "(generation<>? OR embedding_space_id<>?)",
                    (work["chat_id"], work["utc_day"], work["generation"], self.encoder.space_id),
                )
                conn.execute(
                    "UPDATE index_work SET state='done',stage='published',finished_at=? WHERE id=?",
                    (int(time.time()), work_id),
                )
                conn.execute(
                    "INSERT INTO vector_segment_cleanup(chat_id,utc_day,keep_space_id,generation) "
                    "VALUES(?,?,?,?) ON CONFLICT(chat_id,utc_day) DO UPDATE SET "
                    "keep_space_id=excluded.keep_space_id,generation=excluded.generation,error=NULL",
                    (work["chat_id"], work["utc_day"], self.encoder.space_id, work["generation"]),
                )
            with self.lock, self.db.connect() as conn:
                spaces = [
                    dict(row) for row in conn.execute("SELECT id,dimension FROM embedding_spaces")
                ]
                self.vectors.prune_segment(
                    spaces,
                    work["chat_id"],
                    work["utc_day"],
                    self.encoder.space_id,
                    work["generation"],
                )
                conn.execute(
                    "DELETE FROM vector_segment_cleanup WHERE chat_id=? AND utc_day=? "
                    "AND generation=?",
                    (work["chat_id"], work["utc_day"], work["generation"]),
                )
        except WorkInterrupted:
            with self.lock, self.db.connect() as conn:
                conn.execute(
                    "UPDATE index_work SET state='pending' WHERE id=? AND state='running'",
                    (work_id,),
                )
        except Exception as exc:
            error = (
                str(exc)
                if isinstance(exc, UserError)
                else "Не удалось обновить semantic index. Повторите задачу."
            )
            with self.lock, self.db.connect() as conn:
                interrupted = self.should_stop()
                conn.execute(
                    "UPDATE index_work SET state=?,error=? WHERE id=? AND state='running'",
                    (
                        "pending" if interrupted else "failed",
                        None if interrupted else error,
                        work_id,
                    ),
                )
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM index_work WHERE id=?", (work_id,)).fetchone()
            return dict(row) if row else {"state": "deleted"}
