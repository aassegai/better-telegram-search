"""Aggregate-only local benchmark; never outputs queries, author names, or source IDs."""

import argparse
import collections
import json
import math
import platform
import re
import sqlite3
import statistics
import threading
import time
from pathlib import Path

import ijson
import psutil
from fastapi.testclient import TestClient

from telegram_search.backend.api import create_app
from telegram_search.ingestion.importer import ImportService
from telegram_search.shared.text import flatten_text
from telegram_search.storage.database import Database


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else 0


def histogram_percentile(counts, fraction):
    target = math.ceil(sum(counts.values()) * fraction)
    seen = 0
    for length, count in sorted(counts.items()):
        seen += count
        if seen >= target:
            return length
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("json_path", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path("workspace/benchmark"))
    args = parser.parse_args()
    db = Database(args.workspace)
    db.initialize()
    with db.connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]:
            parser.error("Benchmark requires an empty workspace. Choose a fresh ignored directory.")
    process = psutil.Process()
    peak = [process.memory_info().rss]
    finished = threading.Event()

    def sample_memory():
        while not finished.wait(0.01):
            peak[0] = max(peak[0], process.memory_info().rss)

    sampler = threading.Thread(target=sample_memory, daemon=True)
    sampler.start()
    importer = ImportService(db)
    try:
        started = time.perf_counter()
        job = importer.prepare(str(args.json_path))
        result = importer.run(job)
        import_ms = (time.perf_counter() - started) * 1000
        if result["state"] != "completed":
            raise RuntimeError("Benchmark import failed")
        repeated = importer.run(importer.prepare(str(args.json_path)))
    finally:
        importer.shutdown()
    lengths = collections.Counter()
    text_types = collections.Counter()
    queries = set()
    with args.json_path.open("rb") as stream:
        for message in ijson.items(stream, "messages.item", use_float=True):
            text = flatten_text(message.get("text"))
            lengths[len(text)] += 1
            text_types[type(message.get("text")).__name__] += 1
            if len(queries) < 40:
                word = next((w for w in re.findall(r"[^\W_]+", text) if len(w) >= 3), None)
                if word:
                    queries.add(word.casefold())
    timings = []
    with TestClient(create_app(args.workspace), base_url="http://127.0.0.1") as client:
        for query in sorted(queries):
            client.get("/api/search", params={"q": query}).raise_for_status()
        for _ in range(5):
            for query in sorted(queries):
                started = time.perf_counter()
                response = client.get("/api/search", params={"q": query})
                response.raise_for_status()
                timings.append((time.perf_counter() - started) * 1000)
    finished.set()
    sampler.join()
    with db.connect() as conn:
        documents = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE text<>'' AND kind='message'"
        ).fetchone()[0]
        photos = conn.execute(
            "SELECT COUNT(DISTINCT sha256) FROM media_refs WHERE kind='photo' AND status='ready'"
        ).fetchone()[0]
    count = sum(lengths.values())
    report = {
        "platform": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "device": "cpu",
        "model": None,
        "batch_size": 100,
        "messages": result["processed"],
        "lexical_documents": documents,
        "dense_chunks": None,
        "unique_readable_photos": photos,
        "missing_attachments": result["missing_media"],
        "invalid_media": result["invalid_media"],
        "json_bytes": args.json_path.stat().st_size,
        "text_types": dict(text_types),
        "text_chars_mean": round(sum(n * c for n, c in lengths.items()) / max(count, 1), 2),
        "text_chars_p50": histogram_percentile(lengths, 0.5),
        "text_chars_p95": histogram_percentile(lengths, 0.95),
        "import_ms": round(import_ms, 2),
        "repeat_unchanged": repeated["unchanged"],
        "query_count": len(queries),
        "warm_api_requests": len(timings),
        "warm_api_p50_ms": round(statistics.median(timings), 2) if timings else None,
        "warm_api_p95_ms": round(percentile(timings, 0.95), 2) if timings else None,
        "sampled_peak_rss_mib": round(peak[0] / 1024**2, 2),
        "database_bytes": db.path.stat().st_size,
    }
    output = db.workspace / "reports" / "lexical.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
