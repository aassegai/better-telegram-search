import time
from datetime import UTC, datetime

from telegram_search.indexing.estimates import record_rate
from telegram_search.indexing.prefetch import BatchPrefetch
from telegram_search.inference.resources import (
    backoff_indexing_batch,
    indexing_batch_size,
    memory_exhausted,
)
from telegram_search.search.chunking.builder import EpisodeBuilder
from telegram_search.search.chunking.policy import LEGACY_POLICY_ID, ChunkPolicy
from telegram_search.search.chunks import ChunkBuilder, SourceMessage
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import normalize_text


class WorkInterrupted(Exception):
    pass


class ContextChanged(Exception):
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
        self.default_batch = batch_size
        self.should_stop = should_stop
        self.builder = ChunkBuilder(
            encoder.tokenizer,
            passage_prefix=encoder.spec.manifest.get("passage_prefix", "passage: "),
        )

    def _current(self, conn, work) -> bool:
        row = conn.execute(
            "SELECT s.target_generation,s.chunking_policy_id,t.active_space_id,"
            "t.paused,t.enabled,w.state,c.text_paused "
            "FROM index_segments s JOIN index_work w ON w.chat_id=s.chat_id "
            "AND w.utc_day=s.utc_day JOIN chats c ON c.id=w.chat_id "
            "JOIN semantic_state t ON t.id=1 WHERE w.id=?",
            (work["id"],),
        ).fetchone()
        return bool(
            row
            and row["target_generation"] == work["generation"]
            and row["chunking_policy_id"] == work["chunking_policy_id"]
            and row["active_space_id"] == self.encoder.space_id
            and row["enabled"]
            and not row["paused"]
            and not row["text_paused"]
            and row["state"] == "running"
        )

    def _guard(self, conn, work):
        if self.should_stop() or not self._current(conn, work):
            raise WorkInterrupted()

    def _build(self, work):
        start = int(datetime.fromisoformat(work["utc_day"]).replace(tzinfo=UTC).timestamp())
        policy = None
        if work["chunking_policy_id"] != LEGACY_POLICY_ID:
            with self.lock, self.db.connect() as conn:
                self._guard(conn, work)
                row = conn.execute(
                    "SELECT policy_json FROM chunking_policies WHERE id=?",
                    (work["chunking_policy_id"],),
                ).fetchone()
                if not row:
                    raise UserError("Политика чанкинга не найдена. Требуется переиндексация.")
                policy = ChunkPolicy.from_json(row[0])
                if policy.identity != work["chunking_policy_id"]:
                    raise UserError("Политика чанкинга повреждена. Требуется переиндексация.")
                # Register the cross-day selection fence BEFORE taking the source
                # snapshot. A later, closer neighbor must invalidate this day too.
                conn.execute(
                    "INSERT OR REPLACE INTO segment_context_ranges VALUES(?,?,?,?)",
                    (work["id"], work["chat_id"], start - policy.neighbor_seconds, start),
                )
        with self.db.connect() as reader:
            reader.execute("BEGIN")
            dependencies = {}

            def remember(message_id, row):
                dependencies[message_id] = row["content_hash"] if row else None
                if len(dependencies) >= 64:
                    self._stage_dependencies(work, dependencies)
                    dependencies.clear()

            fields = (
                "m.chat_id,m.message_id,m.timestamp,m.author,m.text,m.has_photo,m.kind,"
                "m.remote_deleted,m.author_id,m.reply_to,m.content_hash,"
                "EXISTS(SELECT 1 FROM media_refs r WHERE r.chat_id=m.chat_id AND "
                "r.message_id=m.message_id) OR "
                "json_extract(m.raw_json,'$.media_type') IS NOT NULL OR "
                "json_extract(m.raw_json,'$.file') IS NOT NULL AS has_attachment,"
                "EXISTS(SELECT 1 FROM json_each(m.raw_json,'$.text_entities') e WHERE "
                "json_extract(CASE WHEN e.type='object' THEN e.value ELSE '{}' END,'$.type') "
                "IN ('mention','text_mention','text_link',"
                "'code','pre','link')) AS protected "
            )

            def lookup(chat_id, message_id):
                row = reader.execute(
                    "SELECT " + fields + "FROM indexable_messages m WHERE m.chat_id=? "
                    "AND m.message_id=? AND m.remote_deleted=0",
                    (chat_id, message_id),
                ).fetchone()
                remember(message_id, row)
                return SourceMessage(**dict(row)) if row else None

            def neighbor_lookup(message):
                row = reader.execute(
                    "SELECT " + fields + "FROM indexable_messages m WHERE m.chat_id=? "
                    "AND m.remote_deleted=0 AND m.kind='message' AND length(trim(m.text))>0 "
                    "AND m.timestamp>=? AND (m.timestamp,m.message_id)<(?,?) "
                    "ORDER BY m.timestamp DESC,m.message_id DESC LIMIT 1",
                    (
                        message.chat_id,
                        message.timestamp - policy.neighbor_seconds,
                        message.timestamp,
                        message.message_id,
                    ),
                ).fetchone()
                if row:
                    remember(row["message_id"], row)
                return SourceMessage(**dict(row)) if row else None

            builder = self.builder
            if policy is not None:
                builder = EpisodeBuilder(
                    self.encoder.tokenizer,
                    policy,
                    passage_prefix=self.encoder.spec.manifest.get("passage_prefix", "passage: "),
                    lookup=lookup,
                    neighbor_lookup=neighbor_lookup,
                )
            # A stable canonical snapshot, streamed without raw_json or the entire day in RAM.
            rows = reader.execute(
                "SELECT " + fields + "FROM indexable_messages m "
                "WHERE m.chat_id=? AND m.timestamp>=? AND m.timestamp<? "
                "ORDER BY m.timestamp,m.message_id",
                (work["chat_id"], start, start + 86400),
            )
            messages = (SourceMessage(**dict(row)) for row in rows)
            pending = []
            for ordinal, chunk in enumerate(
                builder.build(messages, work["generation"], self.encoder.space_id), 1
            ):
                pending.append((ordinal, chunk))
                if len(pending) == 64:
                    self._stage(work, pending)
                    pending = []
            if pending:
                self._stage(work, pending)
            if dependencies:
                self._stage_dependencies(work, dependencies)
            source_messages = getattr(builder, "source_messages", 0)
        with self.lock, self.db.connect() as conn:
            self._guard(conn, work)
            totals = conn.execute(
                "SELECT COUNT(*),COALESCE(SUM(tokens),0) FROM chunks WHERE chat_id=? AND utc_day=? "
                "AND generation=? AND embedding_space_id=?",
                (work["chat_id"], work["utc_day"], work["generation"], self.encoder.space_id),
            ).fetchone()
            conn.execute(
                "UPDATE index_work SET stage='embedding',chunks_total=?,tokens_total=?,"
                "source_messages=?,skipped_messages=? WHERE id=?",
                (
                    *totals,
                    source_messages,
                    max(
                        0,
                        source_messages
                        - conn.execute(
                            "SELECT count(DISTINCT p.message_id) FROM chunk_embedding_parts p "
                            "JOIN chunks c ON c.id=p.chunk_id WHERE c.chat_id=? "
                            "AND c.utc_day=? AND c.generation=? "
                            "AND p.role<>'borrowed_context'",
                            (work["chat_id"], work["utc_day"], work["generation"]),
                        ).fetchone()[0],
                    ),
                    work["id"],
                ),
            )

    def _stage_dependencies(self, work, dependencies):
        with self.lock, self.db.connect() as conn:
            self._guard(conn, work)
            for message_id, content_hash in dependencies.items():
                row = conn.execute(
                    "SELECT content_hash FROM indexable_messages WHERE chat_id=? "
                    "AND message_id=? AND remote_deleted=0",
                    (work["chat_id"], message_id),
                ).fetchone()
                if (row[0] if row else None) != content_hash:
                    raise ContextChanged()
                conn.execute(
                    "INSERT OR REPLACE INTO segment_context_dependencies VALUES(?,?,?,?)",
                    (work["id"], work["chat_id"], message_id, content_hash),
                )

    def _stage(self, work, batch):
        with self.lock, self.db.connect() as conn:
            self._guard(conn, work)
            for ordinal, chunk in batch:
                for message_id, content_hash in chunk.dependencies:
                    actual = conn.execute(
                        "SELECT content_hash FROM indexable_messages WHERE chat_id=? "
                        "AND message_id=? AND remote_deleted=0",
                        (chunk.chat_id, message_id),
                    ).fetchone()
                    if (actual[0] if actual else None) != content_hash:
                        raise ContextChanged()
                conn.execute(
                    "INSERT INTO chunks(id,chat_id,utc_day,generation,embedding_space_id,ordinal,"
                    "text,text_normalized,tokens,chunking_policy_id,lexical_text) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        chunk.id,
                        chunk.chat_id,
                        chunk.day,
                        chunk.generation,
                        self.encoder.space_id,
                        ordinal,
                        chunk.text,
                        normalize_text(
                            chunk.lexical_text if chunk.lexical_text is not None else chunk.text
                        ),
                        chunk.tokens,
                        chunk.policy_id,
                        chunk.lexical_text,
                    ),
                )
                conn.executemany(
                    "INSERT INTO chunk_parts VALUES(?,?,?,?,?,?)",
                    [
                        (chunk.id, i, chunk.chat_id, part.message_id, part.start, part.end)
                        for i, part in enumerate(chunk.source_parts or chunk.parts)
                    ],
                )
                conn.executemany(
                    "INSERT INTO chunk_embedding_parts VALUES(?,?,?,?,?,?,?)",
                    [
                        (
                            chunk.id,
                            i,
                            chunk.chat_id,
                            part.message_id,
                            part.start,
                            part.end,
                            part.role,
                        )
                        for i, part in enumerate(chunk.parts)
                    ],
                )
                conn.executemany(
                    "INSERT INTO chunk_context_dependencies VALUES(?,?,?,?)",
                    [
                        (chunk.id, chunk.chat_id, message_id, value)
                        for message_id, value in chunk.dependencies
                    ],
                )

    def _prepared_batch(self, work, checkpoint, batch_size):
        with self.db.connect() as conn:
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
                        batch_size,
                    ),
                )
            ]
        prepared = None
        if rows and hasattr(self.encoder, "prepare_text"):
            prepared = self.encoder.prepare_text([row["text"] for row in rows], "passage")
        return rows, prepared

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
            chat = conn.execute(
                "SELECT text_paused FROM chats WHERE id=?", (work["chat_id"],)
            ).fetchone()
            if self.should_stop() or not state["enabled"] or state["paused"] or not chat or chat[0]:
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
        prefetch = BatchPrefetch()
        try:
            if work["stage"] == "building":
                self._build(work)
            while True:
                with self.db.connect() as conn:
                    self._guard(conn, work)
                    chosen = conn.execute(
                        "SELECT text_batch FROM chats WHERE id=?", (work["chat_id"],)
                    ).fetchone()[0]
                    configured = chosen or self.default_batch
                    # Explicit constructor limits are useful for CLI/tests. Per-chat
                    # preferences take effect on the next batch without restarting work.
                    self.batch_size = indexing_batch_size(self.encoder, configured)
                    checkpoint = conn.execute(
                        "SELECT chunks_done FROM index_work WHERE id=?", (work_id,)
                    ).fetchone()[0]
                size = self.batch_size
                key = (checkpoint, size)
                rows, prepared = prefetch.take(
                    key,
                    lambda checkpoint=checkpoint, size=size: self._prepared_batch(
                        work, checkpoint, size
                    ),
                )
                if not rows:
                    break
                next_checkpoint = rows[-1]["ordinal"]
                prefetch.submit(
                    (next_checkpoint, size),
                    lambda checkpoint=next_checkpoint, size=size: self._prepared_batch(
                        work, checkpoint, size
                    ),
                )
                with self.db.connect() as conn:
                    self._guard(conn, work)
                began = time.perf_counter()
                try:
                    kwargs = {"_prepared": prepared} if prepared is not None else {}
                    embeddings = self.encoder.encode_text(
                        [row["text"] for row in rows], "passage", **kwargs
                    )
                except Exception as exc:
                    if memory_exhausted(exc) and len(rows) > 1:
                        self.batch_size = backoff_indexing_batch(self.encoder, len(rows))
                        continue
                    raise
                publish_started = time.perf_counter()
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
                    record_rate(
                        conn,
                        "e5",
                        self.encoder,
                        sum(len(row["text"]) for row in rows),
                        time.perf_counter() - began,
                    )
                if hasattr(self.encoder, "last_timings"):
                    self.encoder.last_timings = {
                        **self.encoder.last_timings,
                        "publish_seconds": time.perf_counter() - publish_started,
                        "batch_seconds": time.perf_counter() - began,
                    }
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
        except ContextChanged:
            from telegram_search.storage.generations import invalidate_segments

            with self.lock, self.db.connect() as conn:
                if self._current(conn, work):
                    invalidate_segments(conn, work["chat_id"], {work["utc_day"]}, "context_changed")
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
        finally:
            prefetch.close()
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM index_work WHERE id=?", (work_id,)).fetchone()
            return dict(row) if row else {"state": "deleted"}
