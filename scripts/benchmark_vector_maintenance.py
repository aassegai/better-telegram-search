"""Synthetic CPU-only storage benchmark. Never opens an application workspace."""

import argparse
import gc
import json
import tempfile
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path

from telegram_search.search.vectors import VectorStore


def statistics(table):
    value = table.stats()
    return asdict(value) if is_dataclass(value) else value


def benchmark(fragments=256, dimension=64):
    if not 128 <= fragments <= 2048 or not 4 <= dimension <= 768:
        raise ValueError("Synthetic benchmark budget exceeded")
    with tempfile.TemporaryDirectory(prefix="bts-maintenance-synthetic-") as temporary:
        store = VectorStore(Path(temporary))
        table = store.table("synthetic", dimension, create=True)
        try:
            for fragment in range(fragments):
                table.add(
                    [
                        {
                            "id": f"{fragment}-{row}",
                            "chat_id": "synthetic",
                            "utc_day": "day",
                            "generation": 1,
                            "vector": [1.0] + [0.0] * (dimension - 1),
                        }
                        for row in range(4)
                    ]
                )
            before = statistics(table)
            version = table.version
            reader = store.db.open_table("text_synthetic")
            reader.checkout(version)
            expected = sorted(
                table.search().limit(None).to_arrow().to_pylist(), key=lambda r: r["id"]
            )
            predicates = [
                f"chat_id='absent-{i}' AND utc_day='day' AND generation<2" for i in range(32)
            ]
            began = time.perf_counter()
            assert sum(table.count_rows(predicate) for predicate in predicates) == 0
            individual = time.perf_counter() - began
            predicate = " OR ".join(f"({value})" for value in predicates)
            began = time.perf_counter()
            assert table.count_rows(predicate) == 0
            batched = time.perf_counter() - began
            began = time.perf_counter()
            assert store.maybe_optimize(
                [{"id": "synthetic", "dimension": dimension}], threads=2, memory_limit_mib=1024
            )
            maintenance = time.perf_counter() - began
            current = store.table("synthetic", dimension)
            after = statistics(current)
            assert (
                sorted(current.search().limit(None).to_arrow().to_pylist(), key=lambda r: r["id"])
                == expected
            )
            began = time.perf_counter()
            assert current.count_rows(predicate) == 0
            compacted = time.perf_counter() - began
            assert (
                sorted(reader.search().limit(None).to_arrow().to_pylist(), key=lambda r: r["id"])
                == expected
            )
            result = {
                "synthetic": True,
                "dimension": dimension,
                "rows": len(expected),
                "fragments_before": before["fragment_stats"]["num_fragments"],
                "fragments_after": after["fragment_stats"]["num_fragments"],
                "individual_32_cleanup_seconds": round(individual, 6),
                "batched_32_cleanup_seconds": round(batched, 6),
                "compacted_batched_cleanup_seconds": round(compacted, 6),
                "maintenance_seconds": round(maintenance, 6),
                "ids_vectors_unchanged": True,
                "old_snapshot_readable": True,
            }
            reader = current = None
            return result
        finally:
            table = None
            store.close()
            store = None
            gc.collect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fragments", type=int, default=256)
    parser.add_argument("--dimension", type=int, default=64)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.fragments, args.dimension)))


if __name__ == "__main__":
    main()
