"""Public generated text workloads and metrics for real-embedding ANN experiments."""

import math
import sqlite3
import statistics
from dataclasses import dataclass

from telegram_search.search.execution import id_batches

VERSION = "ann-embeddings-generated-v1"


def corpus(topics, size):
    """Unique authored templates, never samples from an archive or working DB."""
    if not 128 <= size <= 32768 or len(topics) < 20:
        raise ValueError("Invalid generated ANN corpus size")
    cities = ("Казань", "Томск", "Пермь", "Омск", "Тула", "Самара", "Москва")
    texts, rows = [], []
    for i in range(size):
        day = f"2026-01-{i % 28 + 1:02}"
        texts.append(
            f"{topics[i % len(topics)][0]} Город: {cities[(i // len(topics)) % len(cities)]}. "
            f"Обсуждение номер {i + 1}; следующий отчёт через {i % 17 + 1} дней."
        )
        rows.append(
            dict(
                id=f"doc-{i:05}",
                chat_id=f"chat-{i % 4}",
                utc_day="media" if i % 5 == 0 else day,
                day=day,
                author=f"author-{i % 7}",
                forwarded=f"source-{i % 3}",
                generation=1,
            )
        )
    return texts, rows


@dataclass(frozen=True)
class Scope:
    name: str
    canonical: str = "1"
    predicate: str | None = None


SCOPES = (
    Scope("all"),
    Scope("text", "utc_day<>'media'", "utc_day<>'media'"),
    Scope("ocr", "utc_day='media'", "utc_day='media'"),
    Scope("chat", "chat_id='chat-1'", "chat_id='chat-1'"),
    Scope(
        "narrow_date",
        "utc_day BETWEEN '2026-01-03' AND '2026-01-05'",
        "utc_day BETWEEN '2026-01-03' AND '2026-01-05'",
    ),
    Scope("author", "author='author-2'"),
    Scope("forwarded", "forwarded='source-1'"),
    Scope(
        "combined",
        "chat_id='chat-1' AND author='author-2' AND day<='2026-01-14'",
        "chat_id='chat-1'",
    ),
)


class CanonicalScope:
    """Small benchmark-only SQLite witness table; not the application's source schema.

    Both strategies share one read snapshot and bounded 512-ID eligibility calls.
    The generated rows stand for already-published vectors. This does not prove the
    production chunk/message joins or mixed-message witness semantics.
    """

    def __init__(self, path, rows):
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            "CREATE TABLE items(id TEXT PRIMARY KEY, chat_id TEXT, utc_day TEXT, "
            "day TEXT, author TEXT, forwarded TEXT, generation INTEGER)"
        )
        self.connection.executemany(
            "INSERT INTO items VALUES(:id,:chat_id,:utc_day,:day,:author,:forwarded,:generation)",
            rows,
        )
        for column in ("chat_id", "utc_day", "day", "author", "forwarded"):
            self.connection.execute(f"CREATE INDEX by_{column} ON items({column})")
        self.connection.commit()
        self.scope = SCOPES[0]

    def select(self, scope):
        self.scope = scope
        return self.connection.execute(
            f"SELECT COUNT(*) FROM items WHERE {scope.canonical}"
        ).fetchone()[0]

    def eligible(self, ids):
        if not ids:
            return []
        if len(ids) > 512:
            raise ValueError("Unbounded benchmark eligibility")
        marks = ",".join("?" for _ in ids)
        return [
            row[0]
            for row in self.connection.execute(
                f"SELECT id FROM items WHERE ({self.scope.canonical}) AND id IN ({marks})", ids
            )
        ]

    def batches(self):
        return id_batches(
            self.connection.execute(
                f"SELECT id FROM items WHERE {self.scope.canonical} ORDER BY id"
            )
        )

    def close(self):
        self.connection.close()


def latency(values):
    values = sorted(values)
    if not values or not all(math.isfinite(v) and v >= 0 for v in values):
        raise ValueError("Invalid benchmark samples")
    return {
        "samples": len(values),
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(values[math.ceil(len(values) * 0.95) - 1], 3),
    }


def recall(expected, actual):
    wanted = {row["id"] for row in expected}
    found = {row["id"] for row in actual}
    if len(found) != len(actual):
        raise ValueError("Duplicate ANN result")
    return len(wanted & found) / len(wanted) if wanted else None
