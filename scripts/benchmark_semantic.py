"""Public synthetic gold evaluation; model weights are read from an ignored workspace."""

import argparse
import json
import math
import statistics
import tempfile
import threading
import time
from pathlib import Path

import psutil

from telegram_search.config.model_registry import model_spec
from telegram_search.indexing.service import SemanticService
from telegram_search.indexing.worker import SegmentWorker
from telegram_search.inference.bundles import BundleStore
from telegram_search.inference.e5 import E5Encoder
from telegram_search.ingestion.importer import ImportService
from telegram_search.search.hybrid import HybridSearch
from telegram_search.search.lexical import Filters
from telegram_search.storage.database import Database


def latency(values):
    values = sorted(values)
    return {
        "p50_ms": round(statistics.median(values), 2),
        "p95_ms": round(values[math.ceil(len(values) * 0.95) - 1], 2),
    }


def run(args):
    root = Path(__file__).resolve().parents[1]
    topics = json.loads((root / "tests/fixtures/synthetic/relevance.json").read_text())
    long_text = topics[23][0] * 30 + " Последнее напоминание: коды лежат в отдельном сейфе."
    tail_start = long_text.index("коды лежат в отдельном сейфе")
    spec = model_spec(args.profile)
    bundle = BundleStore(args.workspace).verify(spec)
    process = psutil.Process()
    peak = [process.memory_info().rss]
    stopped = threading.Event()

    def sample():
        while not stopped.wait(0.05):
            peak[0] = max(peak[0], process.memory_info().rss)

    monitor = threading.Thread(target=sample, daemon=True)
    monitor.start()
    with tempfile.TemporaryDirectory(prefix="benchmark-semantic-", dir=args.workspace) as temporary:
        database = Database(Path(temporary))
        database.initialize()
        importer = ImportService(database)
        semantic = SemanticService(database, importer.lifecycle_lock, start_background=False)
        gold, chat_ids = {}, []
        try:
            for chat_number in range(3):
                messages = []
                selected = (
                    list(range(chat_number * 12, (chat_number + 1) * 12))
                    if chat_number < 2
                    else list(range(24))
                )
                local_gold = {}
                for i, topic in enumerate(selected):
                    mid = i * 3 + 1
                    stamp = 1750000000 + i * 7200
                    text = topics[topic][0]
                    if topic == 23:
                        text = long_text
                    for offset, body in enumerate(
                        (text, "Это наш план, всё согласовано.", "Хорошо, подтверждаю.")
                    ):
                        messages.append(
                            {
                                "id": mid + offset,
                                "type": "message",
                                "text": body,
                                "date_unixtime": str(stamp + offset * 10),
                                "from": "Алиса" if offset == 0 else "Борис",
                                "from_id": "alice" if offset == 0 else "bob",
                            }
                        )
                    local_gold[topic] = mid
                source = Path(temporary) / f"export-{chat_number}"
                source.mkdir()
                path = source / "result.json"
                path.write_text(
                    json.dumps(
                        {
                            "id": 900 + chat_number,
                            "name": f"Synthetic {chat_number}",
                            "type": "personal_chat",
                            "messages": messages,
                        },
                        ensure_ascii=False,
                    )
                )
                result = importer.run(importer.prepare(str(path)))
                assert result["state"] == "completed"
                chat_ids.append(result["chat_id"])
                for topic, mid in local_gold.items():
                    gold.setdefault(topic, set()).add((result["chat_id"], mid))
            encoder = E5Encoder(spec, bundle, threads=4)
            semantic.activate(encoder)
            worker = SegmentWorker(
                database, encoder, semantic._vector_store(), importer.lifecycle_lock
            )
            with database.connect() as conn:
                works = [
                    row[0]
                    for row in conn.execute(
                        "SELECT id FROM index_work WHERE state='pending' ORDER BY id"
                    )
                ]
            began = time.perf_counter()
            first = worker.run(works[0])
            first_seconds = time.perf_counter() - began
            assert first["state"] == "done"
            search = HybridSearch(database, semantic, importer.lifecycle_lock)
            cases = [
                (query, Filters(), gold[i]) for i, topic in enumerate(topics) for query in topic[1:]
            ]
            for i in range(4):
                expected = {item for item in gold[i] if item[0] == chat_ids[2]}
                cases.append(
                    (
                        topics[i][1],
                        Filters(chat_ids=[chat_ids[2]], author_ids=["alice"], content_type="text"),
                        expected,
                    )
                )
            cases += [
                ("восстановление входа", Filters(), gold[23]),
                ("коды лежат в отдельном сейфе", Filters(), gold[23]),
            ]
            empty_cases = [
                ("поезд", Filters(author_ids=["absent"])),
                ("встреча", Filters(date_from=1900000000)),
            ]
            assert len(cases) + len(empty_cases) == 56

            def remaining():
                for work in works[1:]:
                    assert worker.run(work)["state"] == "done"

            indexing = threading.Thread(target=remaining)
            indexing.start()
            contention = []
            for query, filters, _ in cases[:20]:
                began = time.perf_counter()
                search.search(query, filters, mode="hybrid")
                contention.append((time.perf_counter() - began) * 1000)
            indexing.join()
            results = {}
            for mode in ("words", "meaning", "hybrid"):
                recalls, ndcgs, timings = [], [], []
                for query, filters, expected in cases:
                    began = time.perf_counter()
                    response = search.search(query, filters, limit=10, mode=mode)
                    timings.append((time.perf_counter() - began) * 1000)
                    relevant, seen = [], set()
                    for hit in response["results"]:
                        matches = expected & {
                            (hit["chat_id"], part["message_id"])
                            for part in hit.get(
                                "matched_parts", [{"message_id": hit["message_id"]}]
                            )
                            if query != "коды лежат в отдельном сейфе"
                            or part.get("char_end", len(long_text)) > tail_start
                        }
                        fresh = matches - seen
                        relevant.append(bool(fresh))
                        seen |= matches
                    recalls.append(len(seen) / len(expected))
                    dcg = sum(
                        int(value) / math.log2(rank + 2) for rank, value in enumerate(relevant)
                    )
                    ideal = sum(1 / math.log2(rank + 2) for rank in range(min(10, len(expected))))
                    ndcgs.append(dcg / ideal)
                results[mode] = {
                    "recall_at_10": round(statistics.mean(recalls), 4),
                    "ndcg_at_10": round(statistics.mean(ndcgs), 4),
                    **latency(timings),
                    "empty_scope_checks": [
                        len(search.search(q, f, mode=mode)["results"]) for q, f in empty_cases
                    ],
                }
            with database.connect() as conn:
                stats = dict(
                    conn.execute(
                        "SELECT COUNT(*) AS chunks,SUM(tokens) AS tokens,"
                        "MAX(tokens) AS max_tokens FROM chunks"
                    ).fetchone()
                )
                index = dict(
                    conn.execute(
                        "SELECT SUM(chunks_done) AS chunks,SUM(embedding_seconds) AS seconds "
                        "FROM index_work WHERE state='done'"
                    ).fetchone()
                )
            report = {
                "corpus": "public synthetic manual gold; not private archive relevance",
                "profile": args.profile,
                "embedding_space_id": encoder.space_id,
                "queries": 56,
                "ranked_queries": len(cases),
                "empty_scopes": len(empty_cases),
                "messages": 144,
                "chats": 3,
                "chunks": stats,
                "indexing": index,
                "first_segment_cold_seconds": round(first_seconds, 3),
                "query_during_indexing": latency(contention),
                "retrieval": results,
                "peak_process_rss_bytes": peak[0],
                "runtime": encoder.backend_info(),
                "notes": [
                    "Exact LanceDB, no ANN; candidates=100, RRF k=60, equal weights.",
                    "Words uses message BM25; hybrid branches both use chunks.",
                    "Gold judges matched messages; the split-tail query also requires its range.",
                    "Synthetic query repetitions may hit the bounded query cache.",
                ],
            }
        finally:
            semantic.shutdown()
            importer.shutdown()
            stopped.set()
            monitor.join()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path("workspace"))
    parser.add_argument("--profile", choices=["small", "base"], default="small")
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args())
