"""Compare exact retrieval using generated vectors and a synthetic SQLite whitelist."""

import argparse
import heapq
import importlib.metadata
import json
import platform
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

import numpy as np

from telegram_search.search.vectors import VectorStore


def benchmark(root, count, trials):
    store = VectorStore(root / str(count))
    ids = [f"{index:064x}" for index in range(count)]
    rows = [
        {"id": key, "chat_id": "synthetic", "utc_day": "2026-01-01", "generation": 1} for key in ids
    ]
    matrix = np.random.default_rng(2026).normal(size=(count, 384)).astype(np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    store.upsert("synthetic", 384, rows, matrix)
    with sqlite3.connect(":memory:") as canonical:
        canonical.execute("CREATE TABLE canonical(id TEXT PRIMARY KEY,published INTEGER)")
        canonical.executemany(
            "INSERT INTO canonical VALUES(?,?)",
            [(key, index % 5 != 0) for index, key in enumerate(ids)],
        )

        def eligible(keys):
            placeholders = ",".join("?" for _ in keys)
            return [
                row[0]
                for row in canonical.execute(
                    f"SELECT id FROM canonical WHERE published=1 AND id IN ({placeholders})", keys
                )
            ]

        def previous():
            best = []
            cursor = canonical.execute("SELECT id FROM canonical WHERE published=1")
            while batch := cursor.fetchmany(512):
                for hit in store.exact("synthetic", 384, matrix[0], [row[0] for row in batch], 100):
                    candidate = (-hit["_distance"], hit["id"])
                    if len(best) < 100:
                        heapq.heappush(best, candidate)
                    elif candidate > best[0]:
                        heapq.heapreplace(best, candidate)
            return [key for score, key in sorted(best, reverse=True)]

        def current():
            return [
                hit["id"]
                for hit in store.exact_filtered("synthetic", 384, matrix[0], eligible, 100)
            ]

        # Warm both paths, then alternate their order to avoid a cold/warm comparison.
        assert previous() == current()
        timings = {"previous": [], "current": []}
        for trial in range(trials):
            results = {}
            for name, operation in (
                [("previous", previous), ("current", current)]
                if trial % 2 == 0
                else [("current", current), ("previous", previous)]
            ):
                began = time.perf_counter()
                results[name] = operation()
                timings[name].append(time.perf_counter() - began)
            assert results["previous"] == results["current"]
    old, new = (statistics.median(timings[key]) for key in ("previous", "current"))
    return {
        "vectors": count,
        "dimension": 384,
        "trials": trials,
        "previous_seconds": round(old, 4),
        "current_seconds": round(new, 4),
        "speedup": round(old / new, 2),
        "same_top_100": True,
        "timings": timings,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vectors", nargs="+", type=int, default=[16384, 65536])
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.trials <= 10 or any(not 128 <= count <= 131072 for count in args.vectors):
        parser.error("Use 1–10 trials and 128–131072 vectors per case.")
    report = {
        "synthetic_only": True,
        "scope": "CPU vector retrieval and synthetic SQLite eligibility, excluding model inference",
        "platform": platform.system(),
        "architecture": platform.machine(),
        "numpy": np.__version__,
        "lancedb": importlib.metadata.version("lancedb"),
        "cases": [],
    }
    with tempfile.TemporaryDirectory(prefix="bts-search-benchmark-") as temporary:
        for count in args.vectors:
            result = benchmark(Path(temporary), count, args.trials)
            report["cases"].append(result)
            print(json.dumps(result), flush=True)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
