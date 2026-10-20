"""Whole-queue estimates from device-specific observed throughput."""

import time

from telegram_search.indexing.forecast import combine_forecasts
from telegram_search.indexing.rates import prediction, rate_space
from telegram_search.indexing.rates import record_rate as record_rate
from telegram_search.inference.resources import indexing_batch_size


class Estimates:
    def __init__(self, db):
        self.db = db
        self.cache = {}

    def media_forecast(self, kind, encoder, remaining, *, batch=None, projection=0.0, variant=None):
        with self.db.connect() as conn:
            return prediction(
                conn,
                kind,
                encoder,
                self.db.settings,
                remaining,
                batch=batch,
                projection=projection,
                variant=variant,
            )

    def images(self, encoder, remaining):
        return self.media_forecast("clip", encoder, remaining)["estimated_remaining_seconds"]

    def ocr(self, encoder, remaining):
        return self.media_forecast("ocr", encoder, remaining)["estimated_remaining_seconds"]

    def text(self, encoder, chat_id=None):
        return self.text_forecast(encoder, chat_id)["estimated_remaining_seconds"]

    def text_forecast(self, encoder, chat_id=None):
        if not encoder:
            return self.media_forecast("e5", encoder, 1)
        key = (
            encoder.space_id,
            rate_space(encoder, self.db.settings),
            chat_id,
            indexing_batch_size(encoder, 128),
        )
        batch = None
        if chat_id is not None:
            with self.db.connect() as conn:
                chosen = conn.execute(
                    "SELECT text_batch FROM chats WHERE id=?", (chat_id,)
                ).fetchone()
            batch = indexing_batch_size(encoder, chosen[0] or self.db.settings.embedding_batch)
        key = (*key, batch)
        cached = self.cache.get(key)
        if cached and time.monotonic() - cached[0] < 5:
            workload, building = cached[1]
            return self._text_prediction(encoder, workload, building)
        with self.db.connect() as conn:
            scope = " AND w.chat_id=?" if chat_id is not None else ""
            args = (chat_id,) if chat_id is not None else ()
            # ONNX consumes padded token slots, not characters or message counts.
            # Each day's remaining chunks form batches independently.
            size = "MIN(COALESCE(h.text_batch,?),?)"
            limits = (self.db.settings.embedding_batch, indexing_batch_size(encoder, 128))
            staged = conn.execute(
                "SELECT size,SUM(n*longest),SUM(tokens) FROM (SELECT " + size + " size,"
                "COUNT(*) n,MAX(c.tokens) longest,SUM(c.tokens) tokens FROM index_work w "
                "JOIN chats h ON h.id=w.chat_id JOIN index_segments s ON s.chat_id=w.chat_id "
                "AND s.utc_day=w.utc_day AND s.target_generation=w.generation "
                "JOIN chunks c ON c.chat_id=w.chat_id AND c.utc_day=w.utc_day "
                "AND c.generation=w.generation AND c.embedding_space_id=? "
                "AND c.ordinal>w.chunks_done "
                "WHERE w.state IN ('pending','running') AND w.stage='embedding'"
                + scope
                + " GROUP BY w.id,(c.ordinal-w.chunks_done-1)/"
                + size
                + ") GROUP BY size",
                (*limits, encoder.space_id, *args, *limits),
            ).fetchall()
            # Include every not-yet-built day; never infer the entire archive's ETA
            # from only chunks_total of the currently staged segment.
            unknown = conn.execute(
                "SELECT " + size + ",SUM(length(m.text)+length(m.author)+32) FROM index_work w "
                "JOIN chats h ON h.id=w.chat_id JOIN index_segments s ON s.chat_id=w.chat_id "
                "AND s.utc_day=w.utc_day AND s.target_generation=w.generation "
                "JOIN indexable_messages m ON m.chat_id=w.chat_id "
                "AND m.timestamp>=CAST(strftime('%s',w.utc_day) AS INTEGER) "
                "AND m.timestamp<CAST(strftime('%s',w.utc_day) AS INTEGER)+86400 "
                "WHERE w.state IN ('pending','running') AND w.stage='building'"
                + scope
                + " GROUP BY 1",
                (*limits, *args),
            ).fetchall()
            # Author prefixes and overlapping messages add work. Calibrate the
            # expansion from built segments rather than using device-independent time.
            built_scope = " AND w.chat_id=?" if chat_id is not None else ""
            built = conn.execute(
                "SELECT COALESCE(SUM(c.tokens),0) FROM chunks c JOIN index_work w "
                "ON w.chat_id=c.chat_id AND w.utc_day=c.utc_day AND w.generation=c.generation "
                "JOIN index_segments s ON s.chat_id=w.chat_id AND s.utc_day=w.utc_day "
                "AND s.target_generation=w.generation "
                "WHERE c.embedding_space_id=? AND w.stage<>'building'" + built_scope,
                (encoder.space_id, *args),
            ).fetchone()[0]
            canonical = conn.execute(
                "SELECT COALESCE(SUM(length(m.text)+length(m.author)+32),0) FROM index_work w "
                "JOIN index_segments s ON s.chat_id=w.chat_id AND s.utc_day=w.utc_day "
                "AND s.target_generation=w.generation JOIN "
                "indexable_messages m ON m.chat_id=w.chat_id "
                "AND m.timestamp>=CAST(strftime('%s',w.utc_day) AS INTEGER) "
                "AND m.timestamp<CAST(strftime('%s',w.utc_day) AS INTEGER)+86400 "
                "WHERE w.embedding_space_id=? AND w.stage<>'building'" + built_scope,
                (encoder.space_id, *args),
            ).fetchone()[0]
        # Unbuilt days are provisional: calibrate token/character expansion from
        # the archive itself, including filtered noise and overlapping messages.
        expansion = max(0.02, min(4.0, built / canonical)) if canonical else 1.25 / 3.5
        known = {row[0]: row[1] for row in staged}
        padding = {row[0]: row[1] / row[2] if row[2] else 1.1 for row in staged}
        projected = {row[0]: row[1] * expansion * padding.get(row[0], 1.1) for row in unknown}
        workload = [
            (
                size,
                known.get(size, 0) + projected.get(size, 0),
                projected.get(size, 0) / max(1, known.get(size, 0) + projected.get(size, 0)),
            )
            for size in known.keys() | projected.keys()
        ]
        building = sum(row[1] for row in unknown)
        self.cache[key] = (time.monotonic(), (workload, building))
        return self._text_prediction(encoder, workload, building)

    def _text_prediction(self, encoder, workload, building):
        parts = [
            self.media_forecast("e5", encoder, amount, batch=batch, projection=uncertain)
            for batch, amount, uncertain in workload
        ]
        if building:
            parts.append(self.media_forecast("text_build", encoder, building))
        return combine_forecasts(parts, sum(row[1] for row in workload))
