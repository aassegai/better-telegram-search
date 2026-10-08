"""CLIP queue with one bounded CPU lookahead and guarded vector publication."""

import hashlib
import time

from telegram_search.indexing.estimates import record_rate
from telegram_search.inference.resources import memory_exhausted
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize


class MediaImages:
    def _prepare_image_batch(self, encoder, photos):
        good, values, failures = [], [], []
        for sha in photos:
            if self.stop.is_set():
                break
            try:
                data = self._read_photo(sha)
                value = (
                    encoder.prepare_images([data])[0]
                    if hasattr(encoder, "prepare_images")
                    else data
                )
                values.append(value)
                good.append(sha)
            except UserError:
                failures.append(sha)
        return good, values, failures

    def _image_batch(self):
        if not self.clip:
            return False
        encoder = self.clip
        with self.db.connect() as conn:
            if conn.execute("SELECT paused FROM media_state WHERE id=1").fetchone()[0]:
                return False
            pending = (
                "r.kind='photo' AND r.status='ready' AND NOT EXISTS "
                "(SELECT 1 FROM media_embeddings e WHERE e.sha256=r.sha256 "
                "AND e.space_id=? AND e.kind='image') AND NOT EXISTS "
                "(SELECT 1 FROM media_failures f WHERE f.sha256=r.sha256 AND f.space_id=?)"
            )
            chat = conn.execute(
                "SELECT c.id,c.image_batch FROM chats c WHERE c.media_paused=0 "
                "AND EXISTS (SELECT 1 FROM indexable_media_refs r WHERE r.chat_id=c.id AND "
                + pending
                + ") ORDER BY c.id LIMIT 1",
                (encoder.space_id, encoder.space_id),
            ).fetchone()
            if chat is None:
                return False
            configured = chat["image_batch"] or self.db.settings.image_batch
            limit_key = (id(encoder), chat["id"], configured)
            limit = min(configured, self.image_limits.get(limit_key, configured))
            photos = conn.execute(
                "SELECT DISTINCT r.sha256 FROM indexable_media_refs r WHERE r.chat_id=? AND "
                + pending
                + " ORDER BY r.sha256 LIMIT ?",
                (chat["id"], encoder.space_id, encoder.space_id, limit),
            ).fetchall()
        if not photos:
            return False
        with self._activity("clip"):
            return self._index_images(encoder, chat, configured, limit_key, pending, photos)

    def _index_images(self, encoder, chat, configured, limit_key, pending, photos):
        photos = [row[0] for row in photos]
        key = (id(encoder), tuple(photos), configured)
        good, data, failures = self.image_prefetch.take(
            key, lambda: self._prepare_image_batch(encoder, photos)
        )
        for sha in failures:
            self._failure(sha, encoder.space_id, "Файл недоступен или изменился. Повторите импорт.")
        # Prepare one following batch while the current one uses the device/writes
        # vectors. Retain normalized 224x224 tensors, not full decoded photographs.
        placeholders = ",".join("?" for _ in photos)
        with self.db.connect() as conn:
            upcoming = (
                [
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT r.sha256 FROM indexable_media_refs r "
                        "WHERE r.chat_id=? AND "
                        + pending
                        + f" AND r.sha256 NOT IN ({placeholders}) ORDER BY r.sha256 LIMIT ?",
                        (
                            chat["id"],
                            encoder.space_id,
                            encoder.space_id,
                            *photos,
                            min(configured, self.image_limits.get(limit_key, configured)),
                        ),
                    )
                ]
                if photos
                else []
            )
        if upcoming:
            self.image_prefetch.submit(
                (id(encoder), tuple(upcoming), configured),
                lambda: self._prepare_image_batch(encoder, upcoming),
            )
        if not good:
            return True

        def encode(hashes, values):
            with self.lock, self.db.connect() as conn:
                if self.clip is not encoder or not any(self._current(conn, sha) for sha in hashes):
                    return
            began = time.perf_counter()
            try:
                if hasattr(encoder, "prepare_images"):
                    embeddings = encoder.encode_images([None] * len(values), _prepared=values)
                else:
                    embeddings = encoder.encode_images(values)
            except Exception as exc:
                if len(hashes) == 1:
                    self._failure(
                        hashes[0], encoder.space_id, "CLIP не смог обработать фотографию."
                    )
                    return
                middle = len(hashes) // 2
                if memory_exhausted(exc):
                    self.image_limits[limit_key] = max(1, middle)
                # Halving also isolates corrupt images without discarding successful parts.
                encode(hashes[:middle], values[:middle])
                encode(hashes[middle:], values[middle:])
            else:
                with self.lock:
                    if self.clip is encoder:
                        self._publish(
                            encoder.space_id,
                            512,
                            "image",
                            hashes,
                            embeddings,
                            seconds=time.perf_counter() - began,
                        )

        encode(good, data)
        self.last_used = time.monotonic()
        return True

    def _publish(self, space, dimension, kind, hashes, embeddings, *, seconds=None):
        self._register_space(space, dimension, kind)
        with self.lock, self.db.connect() as conn:
            chosen = [i for i, sha in enumerate(hashes) if self._current(conn, sha)]
            rows = [
                {
                    "id": hashlib.sha256(serialize([space, kind, hashes[i]]).encode()).hexdigest(),
                    "chat_id": hashes[i],
                    "utc_day": "media",
                    "generation": 1,
                }
                for i in chosen
            ]
            if not rows:
                return
            self.semantic._vector_store().upsert(space, dimension, rows, embeddings[chosen])
            conn.execute(
                "INSERT OR IGNORE INTO media_spaces VALUES(?,?,?)", (space, dimension, kind)
            )
            conn.executemany(
                "INSERT OR IGNORE INTO media_embeddings(id,sha256,space_id,kind) VALUES(?,?,?,?)",
                [(row["id"], row["chat_id"], space, kind) for row in rows],
            )
            if seconds is not None and self.clip:
                record_rate(conn, "clip", self.clip, len(rows), seconds)
        if seconds is not None and self.clip:
            finished = time.monotonic()
            self.metrics.record(
                "clip",
                self._stage_identity("clip"),
                finished - seconds,
                finished,
                units=len(rows),
                phases=getattr(self.clip, "last_timings", {}),
            )
