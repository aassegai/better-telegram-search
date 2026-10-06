"""Measure exact vs IVF_FLAT on reproducible synthetic vectors, including prefilters."""

import json
import math
import statistics
import tempfile
import time
from pathlib import Path

import numpy as np

from telegram_search.search.vectors import VectorStore

root = Path(__file__).resolve().parents[1]
rng = np.random.default_rng(20261006)
embeddings = rng.normal(size=(4096, 384)).astype(np.float32)
embeddings /= np.linalg.norm(embeddings, axis=1, keepdims=True)
with tempfile.TemporaryDirectory(prefix="ann-", dir=root / "workspace") as temporary:
    store = VectorStore(Path(temporary))
    for start in range(0, len(embeddings), 128):
        rows = [
            {
                "id": f"synthetic-{i}",
                "chat_id": "tail" if i >= 3968 else "other",
                "utc_day": "2025-06-15",
                "generation": 1,
            }
            for i in range(start, min(start + 128, len(embeddings)))
        ]
        store.upsert("a" * 64, 384, rows, embeddings[start : start + 128])
    table = store.table("a" * 64, 384)
    began = time.perf_counter()
    table.create_index(
        index_type="IVF_FLAT",
        metric="cosine",
        num_partitions=32,
        max_iterations=20,
        accelerator=None,
    )
    report = {
        "vectors": 4096,
        "dimension": 384,
        "dtype": "float32",
        "index": "IVF_FLAT",
        "partitions": 32,
        "nprobes": 8,
        "training_seconds": round(time.perf_counter() - began, 3),
        "scopes": {},
    }
    for scope, predicate in (("all", None), ("filtered_128", "chat_id='tail'")):
        overlaps, exact_times, ann_times = [], [], []
        for i in range(30):
            query = embeddings[3968 + i] + rng.normal(0, 0.01, 384).astype(np.float32)
            query /= np.linalg.norm(query)

            def retrieve(exact, query=query, predicate=predicate):
                search = table.search(query).distance_type("cosine")
                if exact:
                    search = search.bypass_vector_index()
                else:
                    search = search.nprobes(8)
                if predicate:
                    search = search.where(predicate, prefilter=True)
                return search.select(["id", "_distance"]).limit(10).to_list()

            began = time.perf_counter()
            expected = retrieve(True)
            exact_times.append((time.perf_counter() - began) * 1000)
            began = time.perf_counter()
            actual = retrieve(False)
            ann_times.append((time.perf_counter() - began) * 1000)
            overlaps.append(
                len({row["id"] for row in expected} & {row["id"] for row in actual}) / 10
            )
        report["scopes"][scope] = {"recall_at_10_vs_exact": round(statistics.mean(overlaps), 4)}
        for name, values in (("exact", exact_times), ("ann", ann_times)):
            report["scopes"][scope][name] = {
                "p50_ms": round(statistics.median(values), 3),
                "p95_ms": round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 3),
            }
    report["decision"] = (
        "Keep exact for current corpus; ANN recall is not guaranteed, especially for filters."
    )
    report["limitations"] = (
        "Synthetic random vectors, engine timings only; not Telegram recall or end-to-end latency."
    )
output = root / "docs/benchmarks/ann-synthetic.json"
output.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
