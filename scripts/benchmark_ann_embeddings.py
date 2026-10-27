"""CPU-only ANN comparison on pinned BERTA embeddings of public generated texts.

No archive paths, working application DBs, remote code, model auto-downloads, or
inference in retrieval timings. Each candidate gets a fresh spawned process/table.
"""

import argparse
import hashlib
import importlib.metadata
import json
import multiprocessing
import os
import platform
import tempfile
import threading
import time
from pathlib import Path

from telegram_search.evaluation.ann import VERSION, CanonicalScope, Scope, corpus, latency, recall
from telegram_search.evaluation.files import check_output, write_report
from telegram_search.security.paths import is_link_or_reparse

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_OUTPUT = ROOT / "docs/benchmarks/ann-embeddings.json"
MODEL_WORKSPACE = Path("/tmp/bts-ann-benchmark")
CANDIDATES = ("IVF_FLAT", "IVF_HNSW_SQ", "IVF_HNSW_FLAT")


class RssSample:
    def __enter__(self):
        import psutil

        self.process = psutil.Process()
        self.peak = self.process.memory_info().rss
        self.stop = threading.Event()

        def sample():
            while not self.stop.wait(0.02):
                self.peak = max(self.peak, self.process.memory_info().rss)

        self.thread = threading.Thread(target=sample, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.peak = max(self.peak, self.process.memory_info().rss)
        self.stop.set()
        self.thread.join()


def encode(model_workspace, texts, queries, threads):
    import numpy as np

    from telegram_search.config.model_registry import model_spec
    from telegram_search.inference.bundles import BundleStore
    from telegram_search.inference.e5 import E5Encoder

    spec = model_spec("berta")
    encoder = E5Encoder(spec, BundleStore(model_workspace).verify(spec), threads=threads)
    start = time.perf_counter()
    with RssSample() as memory:
        matrix = np.concatenate(
            [encoder.encode_text(texts[i : i + 32], "passage") for i in range(0, len(texts), 32)]
        )
        query_matrix = encoder.encode_text(queries, "query")
    return (
        matrix,
        query_matrix,
        {
            "model_id": spec.model_id,
            "revision": spec.revision,
            "manifest_identity": spec.identity,
            "embedding_space": encoder.space_id,
            "dimension": spec.dimension,
            "dtype": str(matrix.dtype),
            "provider": encoder.backend_info()["provider"],
            "seconds": round(time.perf_counter() - start, 3),
            "sampled_process_peak_rss_bytes": memory.peak,
            "unique_vectors": len(np.unique(matrix, axis=0)),
        },
    )


class DirectNative:
    """Benchmark adapter for the shared canonical prefilter/merge implementation.

    Deliberately in-process: HNSW is a comparison candidate, not an enabled
    application index. Production child/IPC costs are measured separately for IVF.
    """

    def __init__(self, table, kind, ef, refine):
        self.table, self.kind, self.ef, self.refine = table, kind, ef, refine

    def query(self, *, query, predicate, probes, limit, version, **_kwargs):
        if self.table.version != version:
            raise RuntimeError("Benchmark snapshot changed")
        search = self.table.search(query).distance_type("cosine").nprobes(probes)
        if "HNSW" in self.kind:
            search = search.ef(self.ef)
            if self.kind.endswith("SQ"):
                search = search.refine_factor(self.refine)
        if predicate:
            search = search.where(predicate, prefilter=True)
        return search.select(["id", "_distance"]).limit(limit).to_list()


def compare_scope(store, witness, scope, vectors, queries, kind, partitions, probes, ef, refine):
    import statistics

    from telegram_search.evaluation.ann import SCOPES
    from telegram_search.search.ann import AnnOptions, retrieve
    from telegram_search.search.execution import SearchExecution, execution_context

    dimension = vectors.shape[1]
    table = store.table("benchmark", dimension)
    options = AnnOptions(partitions=partitions, probes=probes)
    total = witness.select(scope)
    native = DirectNative(table, kind, ef, refine)
    expected = []
    exact_ms = []
    exact_diagnostics = None
    for query in queries:
        execution = SearchExecution()
        began = time.perf_counter()
        with execution_context(execution):
            expected.append(
                store.exact_filtered(
                    "benchmark",
                    dimension,
                    query,
                    witness.eligible,
                    10,
                    predicate=scope.predicate,
                    eligible_batches=witness.batches(),
                    total=total,
                )
            )
        exact_ms.append((time.perf_counter() - began) * 1000)
        exact_diagnostics = execution.snapshot()["diagnostics"]
    samples, recalls = [], []
    fresh_hits = 0
    first_ms = None
    diagnostics = None
    for repeat in range(2):
        for i, query in enumerate(queries):
            execution = SearchExecution()
            began = time.perf_counter()
            with execution_context(execution):
                actual = retrieve(
                    table,
                    dimension,
                    query,
                    witness.eligible,
                    witness.batches(),
                    10,
                    scope.predicate,
                    options,
                    native,
                    store.location,
                    {"table_version": table.version, "partitions": partitions},
                    "benchmark",
                )
            elapsed = (time.perf_counter() - began) * 1000
            if first_ms is None:
                first_ms = elapsed
            if repeat == 1:
                samples.append(elapsed)
                recalls.append(recall(expected[i], actual))
                if scope.name == "fresh_changes":
                    fresh_hits += any(row["id"] == f"fresh-{i:03}" for row in actual)
            if set(row["id"] for row in actual) - set(witness.eligible([r["id"] for r in actual])):
                raise RuntimeError("ANN result outside canonical benchmark scope")
            diagnostics = execution.snapshot()["diagnostics"]
    report = {
        "eligible_rows": total,
        "first_native_ms": round(first_ms, 3),
        "warm_native_with_scope": latency(samples),
        "recall_at_10_mean": round(statistics.mean(recalls), 6),
        "recall_at_10_min": round(min(recalls), 6),
        "application_exact": latency(exact_ms),
        "last_native_diagnostics": diagnostics,
        "last_exact_diagnostics": exact_diagnostics,
    }
    if scope.name == "fresh_changes":
        report["known_fresh_top10_hits"] = fresh_hits
        if fresh_hits != len(queries):
            raise RuntimeError("ANN lost a known-nearest unindexed fresh vector")
    # Only the real production-compatible IVF index can enter search_filtered.
    if kind == "IVF_FLAT":
        store.close()
        ipc_ms, ipc_recalls, effective, reasons = [], [], set(), set()
        for i, query in enumerate(queries):
            execution = SearchExecution()
            execution.requested_strategy = "ann"
            began = time.perf_counter()
            with execution_context(execution):
                actual = store.search_filtered(
                    "benchmark",
                    dimension,
                    query,
                    witness.eligible,
                    10,
                    predicate=scope.predicate,
                    eligible_batches=witness.batches(),
                    total=total,
                    ann_options=options,
                )
            ipc_ms.append((time.perf_counter() - began) * 1000)
            ipc_recalls.append(recall(expected[i], actual))
            if set(row["id"] for row in actual) - set(witness.eligible([r["id"] for r in actual])):
                raise RuntimeError("Application result outside canonical benchmark scope")
            snapshot = execution.snapshot()
            effective.add(snapshot["effective_strategy"])
            reasons.update(snapshot["fallback_reasons"])
        report["application_ann"] = {
            "first_call_ms": round(ipc_ms[0], 3),
            "subsequent": latency(ipc_ms[1:]),
            "effective_strategies": sorted(effective),
            "fallback_reasons": sorted(reasons),
            "recall_at_10_mean": round(statistics.mean(ipc_recalls), 6),
            "recall_at_10_min": round(min(ipc_recalls), 6),
            "last_diagnostics": snapshot["diagnostics"],
        }
    assert scope in SCOPES or scope.name == "fresh_changes"
    return report


def worker(root, kind, config):
    """Only called via spawn with this parent's fresh generated temporary directory."""
    os.environ["RAYON_NUM_THREADS"] = str(config["threads"])
    os.environ["LANCE_CPU_THREADS"] = str(config["threads"])
    os.environ["OMP_NUM_THREADS"] = str(config["threads"])
    import numpy as np

    from telegram_search.evaluation.ann import SCOPES
    from telegram_search.search.ann_process import _exit_with_parent
    from telegram_search.search.vectors import VectorStore

    _exit_with_parent()
    root = Path(root)
    # Only arrays and metadata just generated by the parent, never pickle/code.
    with np.load(root / "embeddings.npz", allow_pickle=False) as data:
        vectors, queries = data["vectors"], data["queries"]
    rows = json.loads((root / "rows.json").read_text(encoding="utf-8"))
    directory = root / kind
    directory.mkdir()
    store = VectorStore(directory)
    store.ann_memory_limit_mib = 1024
    store.ann_threads = config["threads"]
    witness = CanonicalScope(directory / "canonical.sqlite", rows)
    try:
        for start in range(0, len(rows), 512):
            store.upsert(
                "benchmark",
                vectors.shape[1],
                rows[start : start + 512],
                vectors[start : start + 512],
            )
        table = store.table("benchmark", vectors.shape[1])
        name = f"bts_ivfflat_cosine_v1_d{vectors.shape[1]}_p{config['partitions']}"
        began = time.perf_counter()
        with RssSample() as memory:
            table.create_index(
                index_type=kind,
                metric="cosine",
                num_partitions=config["partitions"],
                sample_rate=32,
                max_iterations=30,
                name=name,
                m=20,
                ef_construction=300,
            )
        build_seconds = time.perf_counter() - began
        stats = table.index_stats(name)
        if stats is None or stats.num_indexed_rows != len(rows):
            raise RuntimeError("Incomplete benchmark index")
        report = {
            "index_type": kind,
            "build_seconds": round(build_seconds, 3),
            "build_sampled_process_peak_rss_bytes": memory.peak,
            "native_index_bytes": sum(
                p.stat().st_size
                for p in store.table_path("benchmark").joinpath("_indices").rglob("*")
                if p.is_file()
            ),
            "scopes": {},
        }
        for probes in config["probes"]:
            comparisons = {}
            with RssSample() as query_memory:
                for scope in SCOPES:
                    witness.connection.execute("BEGIN")
                    try:
                        comparisons[scope.name] = compare_scope(
                            store,
                            witness,
                            scope,
                            vectors,
                            queries,
                            kind,
                            config["partitions"],
                            probes,
                            config["ef"],
                            config["refine"],
                        )
                    finally:
                        witness.connection.rollback()
            report["scopes"][str(probes)] = comparisons
            report.setdefault("query_sampled_process_peak_rss_bytes", {})[str(probes)] = (
                query_memory.peak
            )
        # Same vectors/model/metadata, but fresh appends plus canonical deletion.
        # New exact passage vectors are queries here, exercising the unindexed tail.
        tail = [dict(rows[i], id=f"fresh-{i:03}", generation=2) for i in range(len(queries))]
        store.upsert("benchmark", vectors.shape[1], tail, queries)
        witness.connection.executemany(
            "INSERT INTO items VALUES(:id,:chat_id,:utc_day,:day,:author,:forwarded,:generation)",
            tail,
        )
        witness.connection.execute("DELETE FROM items WHERE id IN ('doc-00000','doc-00001')")
        witness.connection.commit()
        fresh_status = store.table("benchmark", vectors.shape[1]).index_stats(name)
        witness.connection.execute("BEGIN")
        try:
            report["fresh_changes"] = compare_scope(
                store,
                witness,
                Scope("fresh_changes"),
                vectors,
                queries,
                kind,
                config["partitions"],
                config["probes"][-1],
                config["ef"],
                config["refine"],
            )
        finally:
            witness.connection.rollback()
        report["fresh_unindexed_rows"] = fresh_status.num_unindexed_rows
        # Assertions concern correctness, not a guaranteed quality threshold.
        if fresh_status.num_unindexed_rows < len(queries):
            raise RuntimeError("Fresh benchmark tail was not exercised")
        (root / f"{kind}.json").write_text(json.dumps(report, allow_nan=False))
    finally:
        store.close()
        witness.close()


def run_candidate(root, kind, config):
    process = multiprocessing.get_context("spawn").Process(target=worker, args=(root, kind, config))
    try:
        process.start()
        deadline = time.monotonic() + 900
        while process.is_alive() and time.monotonic() < deadline:
            process.join(timeout=0.25)
        if process.pid is not None and process.is_alive():
            process.kill()
            process.join()
            raise RuntimeError("ANN candidate benchmark timed out")
        if process.exitcode:
            raise RuntimeError("ANN candidate benchmark failed")
        return json.loads((root / f"{kind}.json").read_text(encoding="utf-8"))
    finally:
        if process.pid is not None and process.is_alive():
            process.kill()
            process.join()
        process.close()


def run(args):
    import numpy as np

    for parent in (*args.model_workspace.parents, args.model_workspace):
        if is_link_or_reparse(parent):
            raise ValueError("Benchmark model path must not contain links")
    allowed = (MODEL_WORKSPACE, ROOT / "workspace/benchmarks/ann")
    if args.model_workspace.resolve() not in allowed:
        raise ValueError("Use the dedicated benchmark model workspace")
    output = check_output(args.output, ROOT, "benchmark", VERSION, (PUBLIC_OUTPUT,))
    topics = json.loads(
        (ROOT / "tests/fixtures/synthetic/relevance.json").read_text(encoding="utf-8")
    )
    texts, rows = corpus(topics, args.rows)
    queries = [topic[1] for topic in topics[: args.queries]]
    matrix, query_matrix, inference = encode(args.model_workspace, texts, queries, args.threads)
    config = dict(threads=args.threads, partitions=64, probes=[8, 32, 64], ef=128, refine=4)
    with tempfile.TemporaryDirectory(prefix="bts-ann-embeddings-") as temporary:
        root = Path(temporary)
        np.savez(root / "embeddings.npz", vectors=matrix, queries=query_matrix)
        (root / "rows.json").write_text(json.dumps(rows))
        candidates = []
        for kind in CANDIDATES:
            candidates.append(run_candidate(root, kind, config))
    report = {
        "benchmark": VERSION,
        "corpus": "public authored generated Russian text templates; no private archive",
        "rows": args.rows,
        "queries": len(queries),
        "k": 10,
        "input_sha256": hashlib.sha256(
            json.dumps([texts, rows, queries], ensure_ascii=False).encode()
        ).hexdigest(),
        "vectors_sha256": hashlib.sha256(matrix.tobytes() + query_matrix.tobytes()).hexdigest(),
        "runtime": {
            key: importlib.metadata.version(key)
            for key in ("lancedb", "numpy", "pyarrow", "onnxruntime", "tokenizers")
        },
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "config": config,
        "inference": inference,
        "candidates": candidates,
        "decision": (
            "Keep exact as default; this generated text benchmark does not validate "
            "archive or visual quality."
        ),
        "limitations": [
            "Real BERTA/ORT CPU outputs; texts and OCR-labelled rows are generated, "
            "not real OCR or images.",
            "Recall is strict top-10 ID overlap against application exact cosine, "
            "not judged semantic relevance; near-ties may reorder.",
            "First call is not OS cold: imports, writes and exact queries may warm caches. "
            "Warm native samples are a second full pass; application samples follow that pass.",
            "Native HNSW uses shared canonical prefilter/merge in-process; "
            "only IVF includes production child/IPC measurements.",
            "Canonical benchmark SQLite rows are published-vector witnesses, "
            "not production chunk/message authorization joins.",
            "RSS is sampled whole candidate process; query child RSS is not included.",
            "Small scopes deliberately test native candidates and separately "
            "expose application exact fallback.",
            "Fresh known-nearest probes append query-role vectors to test tail inclusion, "
            "not ordinary passage retrieval quality.",
            "No SigLIP image embeddings, audio, actual archive distribution "
            "or large-corpus default decision in this report.",
            "Native centroid training has no shared fixed seed; low-probe differences "
            "include different coarse partitions, not only HNSW or quantization.",
            "One build per candidate, K=10; no top-100, larger-corpus or OS-cold acceptance.",
            "narrow_date is text-only; native_index_bytes includes the scalar ID index.",
        ],
    }
    write_report(
        output,
        report,
        root=ROOT,
        owner_key="benchmark",
        owner_value=VERSION,
        public_paths=(PUBLIC_OUTPUT,),
    )
    print(json.dumps({"benchmark": VERSION, "rows": args.rows, "candidates": len(candidates)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-workspace", type=Path, default=MODEL_WORKSPACE)
    parser.add_argument("--rows", type=int, choices=[4096, 8192, 16384, 32768], default=8192)
    parser.add_argument("--queries", type=int, choices=range(4, 25), default=16)
    parser.add_argument("--threads", type=int, choices=range(1, 9), default=4)
    parser.add_argument("--output", type=Path, default=PUBLIC_OUTPUT)
    run(parser.parse_args())
