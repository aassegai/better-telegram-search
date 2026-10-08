"""Whole-queue estimates from device-specific observed throughput."""

import time


def provider(encoder):
    execution = getattr(encoder, "execution", None)
    if execution is None:
        return "cpu"
    selected = getattr(execution, "provider", None) or execution.info().get("provider", "cpu")
    return "CPU+" + selected if getattr(encoder, "cpu_peer", None) else selected


def rate_space(encoder):
    if hasattr(encoder, "space_id"):
        return encoder.space_id
    identity = f"{encoder.version}:threads={getattr(encoder, 'threads', 1)}"
    config = getattr(encoder, "rate_settings", None)
    return f"{identity}:batch={config}" if config is not None else identity


def record_rate(conn, kind, encoder, units, seconds):
    if units <= 0 or seconds <= 0:
        return
    conn.execute(
        "INSERT INTO index_rates VALUES(?,?,?,?,1) "
        "ON CONFLICT(kind,space_id,provider) DO UPDATE SET "
        "seconds_per_unit=index_rates.seconds_per_unit*0.8+excluded.seconds_per_unit*0.2,"
        "batches=index_rates.batches+1",
        (kind, rate_space(encoder), provider(encoder), seconds / units),
    )


class Estimates:
    def __init__(self, db):
        self.db = db
        self.cache = {}

    def rate(self, conn, kind, encoder):
        row = conn.execute(
            "SELECT seconds_per_unit,batches FROM index_rates "
            "WHERE kind=? AND space_id=? AND provider=?",
            (kind, rate_space(encoder), provider(encoder)),
        ).fetchone()
        return row[0] if row and row[1] >= 2 else None

    def images(self, encoder, remaining):
        return self.media("clip", encoder, remaining)

    def ocr(self, engine, remaining):
        return self.media("ocr", engine, remaining)

    def media(self, kind, encoder, remaining):
        if remaining <= 0:
            return 0.0
        if not encoder:
            return None
        with self.db.connect() as conn:
            rate = self.rate(conn, kind, encoder)
        return round(rate * remaining, 1) if rate is not None else None

    def text(self, encoder, chat_id=None):
        if not encoder:
            return None
        key = (encoder.space_id, provider(encoder), chat_id)
        cached = self.cache.get(key)
        if cached and time.monotonic() - cached[0] < 15:
            return cached[1]
        with self.db.connect() as conn:
            rate = self.rate(conn, "e5", encoder)
            if rate is None:
                return None
            scope = " AND w.chat_id=?" if chat_id is not None else ""
            args = (chat_id,) if chat_id is not None else ()
            # Staged work has an exact remaining character count, including overlap.
            known = conn.execute(
                "SELECT COALESCE(SUM(length(c.text)),0) FROM index_work w "
                "JOIN index_segments s ON s.chat_id=w.chat_id AND s.utc_day=w.utc_day "
                "AND s.target_generation=w.generation JOIN chunks c ON c.chat_id=w.chat_id "
                "AND c.utc_day=w.utc_day AND c.generation=w.generation "
                "AND c.embedding_space_id=? AND c.ordinal>w.chunks_done "
                "WHERE w.state IN ('pending','running','failed') AND w.stage='embedding'" + scope,
                (encoder.space_id, *args),
            ).fetchone()[0]
            # Include every not-yet-built day; never infer the entire archive's ETA
            # from only chunks_total of the currently staged segment.
            unknown = conn.execute(
                "SELECT COALESCE(SUM(length(m.text)+length(m.author)+32),0) FROM index_work w "
                "JOIN index_segments s ON s.chat_id=w.chat_id AND s.utc_day=w.utc_day "
                "AND s.target_generation=w.generation JOIN "
                "indexable_messages m ON m.chat_id=w.chat_id "
                "AND m.timestamp>=CAST(strftime('%s',w.utc_day) AS INTEGER) "
                "AND m.timestamp<CAST(strftime('%s',w.utc_day) AS INTEGER)+86400 "
                "WHERE w.state IN ('pending','running','failed') AND w.stage='building'" + scope,
                args,
            ).fetchone()[0]
            # Author prefixes and overlapping messages add work. Calibrate the
            # expansion from built segments rather than using device-independent time.
            built_scope = " AND w.chat_id=?" if chat_id is not None else ""
            built = conn.execute(
                "SELECT COALESCE(SUM(length(c.text)),0) FROM chunks c JOIN index_work w "
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
        expansion = max(1.0, min(4.0, built / canonical)) if canonical else 1.25
        result = round((known + unknown * expansion) * rate, 1)
        self.cache[key] = (time.monotonic(), result)
        return result
