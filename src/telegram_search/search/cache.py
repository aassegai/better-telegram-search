"""Bounded, ephemeral search snapshots: paging never runs model inference."""

import hashlib
import json
import secrets
import threading
import time
from collections import OrderedDict


class SearchCache:
    capacity = 100

    def __init__(self, *, ttl=300, max_entries=4, max_bytes=32 * 1024 * 1024, clock=None):
        self.ttl, self.max_entries, self.max_bytes = ttl, max_entries, max_bytes
        self.clock = clock or time.monotonic
        self.entries = OrderedDict()
        self.bytes = 0
        self.lock = threading.Lock()

    @staticmethod
    def revision(db):
        from telegram_search.config.model_registry import rerank_spec, visual_specs

        # Finished indexing may add matches without invalidating this ordered snapshot.
        # Archive edits, author exclusions, chat deletion and model/settings changes do.
        with db.connect() as conn:
            epoch = conn.execute(
                "SELECT generation FROM search_archive_epoch WHERE id=1"
            ).fetchone()[0]
            model = tuple(
                conn.execute(
                    "SELECT active_space_id,enabled FROM semantic_state WHERE id=1"
                ).fetchone()
            )
            media = tuple(
                conn.execute(
                    "SELECT ocr_enabled,images_enabled,visual_profile,visual_space_id "
                    "FROM media_state WHERE id=1"
                ).fetchone()
            )
            rerank = tuple(
                conn.execute(
                    "SELECT manifest_id,preparation_state FROM rerank_state WHERE id=1"
                ).fetchone()
            )
        # Progress, pauses and download counters do not change the meaning of stored hits.
        visual = [spec.identity for spec in visual_specs(media[2]).values()]
        fingerprint = [
            epoch,
            db.settings.__dict__,
            model,
            media,
            rerank,
            visual,
            rerank_spec().identity,
            "giga-top50-rrf-v1",
        ]
        return hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()

    def _drop(self, token):
        self.bytes -= len(self.entries.pop(token)[2])

    def _expire(self):
        now = self.clock()
        for token, (_, created, _) in list(self.entries.items()):
            if now - created >= self.ttl:
                self._drop(token)

    def remember(self, result, revision, limit, chunk_size):
        result = {**result, "limit": limit, "chunk_size": chunk_size}
        raw = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
        token = None
        with self.lock:
            self._expire()
            if len(raw) <= self.max_bytes and len(result["results"]) > limit:
                while self.entries and (
                    len(self.entries) >= self.max_entries or self.bytes + len(raw) > self.max_bytes
                ):
                    self._drop(next(iter(self.entries)))
                token = secrets.token_urlsafe(24)
                self.entries[token] = (revision, self.clock(), raw)
                self.bytes += len(raw)
        return self._page(result, token, 0, limit)

    def page(self, token, revision, offset, limit):
        with self.lock:
            self._expire()
            entry = self.entries.get(token)
            if entry is None:
                raise KeyError(token)
            if entry[0] != revision:
                self._drop(token)
                raise ValueError("stale search snapshot")
            self.entries.move_to_end(token)
            result = json.loads(entry[2])
        return self._page(result, token, offset, limit)

    def _page(self, result, token, offset, limit):
        hits = result["results"]
        end = min(offset + limit, len(hits))
        return {
            **result,
            "results": hits[offset:end],
            "search_id": token,
            "offset": offset,
            "next_offset": end if token and end < len(hits) else None,
            "cached_results": len(hits),
            "has_more": end < len(hits) or result.get("has_more", False),
            "limit": limit,
        }
