"""Public CPU smoke/latency probes for the pinned pairwise ONNX reranker."""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import threading
import time
from pathlib import Path

from telegram_search.config.model_registry import rerank_spec
from telegram_search.evaluation.ann import latency
from telegram_search.evaluation.files import check_output, write_report
from telegram_search.inference.bundles import BundleStore
from telegram_search.security.paths import is_link_or_reparse

ROOT = Path(__file__).resolve().parents[1]
VERSION = "jina-pairwise-cpu-v1"
OUTPUT = ROOT / "docs/benchmarks/jina-cpu.json"
WORKSPACE = Path("/tmp/bts-jina-runtime")
PROBES = (
    (
        "Which planet is called the Red Planet?",
        (
            "Mars is called the Red Planet because of iron oxide on its surface.",
            "The meeting is scheduled for tomorrow afternoon.",
            "The cake needs flour and eggs.",
            "A train ticket costs forty euros.",
        ),
    ),
    (
        "How do I reset a forgotten password?",
        (
            "Use the reset password link on the sign-in page to receive a recovery email.",
            "The library opens at nine in the morning.",
            "We booked a seaside hotel for the holidays.",
            "The recipe calls for two fresh apples.",
        ),
    ),
    (
        "Where can I fix a punctured bicycle tyre?",
        (
            "The bicycle repair shop replaces punctured tubes and inflates tyres.",
            "Our concert tickets are on the kitchen table.",
            "The new software release is planned for Friday.",
            "Water the tomatoes twice a week.",
        ),
    ),
    (
        "Как восстановить доступ к аккаунту?",
        (
            "Нажмите восстановить пароль и используйте резервные коды для входа.",
            "Завтра встречаемся в кафе у библиотеки.",
            "Поезд отправляется с вокзала в семь часов.",
            "Для пирога нужны мука, яйца и яблоки.",
        ),
    ),
)


def run(args):
    import numpy as np
    import psutil

    from telegram_search.inference.jina import JinaReranker

    output = check_output(args.output, ROOT, "benchmark", VERSION, (OUTPUT,))
    path = args.model_workspace.absolute()
    if any(is_link_or_reparse(part) for part in (*path.parents, path)) or path.resolve() not in (
        WORKSPACE,
        ROOT / "workspace/benchmarks/jina",
    ):
        raise ValueError("Use the dedicated Jina benchmark model workspace without links")
    spec = rerank_spec("jina-v2")
    model = JinaReranker(spec, BundleStore(path).verify(spec), threads=4, device="cpu")
    process, stopped = psutil.Process(), threading.Event()
    peak = [process.memory_info().rss]

    def sample():
        while not stopped.wait(0.02):
            peak[0] = max(peak[0], process.memory_info().rss)

    monitor = threading.Thread(target=sample, daemon=True)
    monitor.start()
    reports, timings, first_seconds = [], [], None
    try:
        for i, (query, texts) in enumerate(PROBES):
            candidates = [dict(candidate_id=str(j), text=text) for j, text in enumerate(texts)]
            began = time.perf_counter()
            actual = model.score_pairs(query, candidates)
            elapsed = time.perf_counter() - began
            if first_seconds is None:
                first_seconds = elapsed
            scores = [row["score"] for row in actual]
            if scores[0] <= max(scores[1:]):
                raise RuntimeError("Public reranker smoke ordering failed")
            single = model.score_pairs(query, candidates[:1])[0]["score"]
            error = abs(single - scores[0])
            if error > 0.0001:
                raise RuntimeError("Reranker padding invariance failed")
            reports.append(
                dict(
                    probe=i,
                    scores=scores,
                    relevant_rank=1,
                    single_vs_batch_max_abs=error,
                    truncated=sum(row["truncated"] for row in actual),
                )
            )
            for _ in range(3):
                began = time.perf_counter()
                model.score_pairs(query, candidates)
                timings.append((time.perf_counter() - began) * 1000)
        long = model.score_pairs(
            "query " * 2000, [dict(candidate_id="long", text="document " * 5000)]
        )
        if not long[0]["truncated"] or not long[0]["query_truncated"]:
            raise RuntimeError("Reranker truncation was not reported")
        report = dict(
            benchmark=VERSION,
            corpus="four public authored smoke queries; not independent relevance validation",
            model=spec.model_id,
            revision=spec.revision,
            manifest_identity=spec.identity,
            input_sha256=hashlib.sha256(
                json.dumps(PROBES, ensure_ascii=False).encode()
            ).hexdigest(),
            license=spec.manifest["license"],
            model_bytes=spec.download_bytes,
            runtime={
                name: importlib.metadata.version(name)
                for name in ("onnxruntime", "tokenizers", "numpy")
            },
            platform=dict(system=platform.system(), machine=platform.machine()),
            provider=model.backend_info()["provider"],
            threads=4,
            batch=4,
            first_call_seconds=round(first_seconds, 3),
            warm_four_pairs=latency(timings),
            sampled_process_peak_rss_bytes=peak[0],
            probes=reports,
            max_padding_abs=float(np.max([row["single_vs_batch_max_abs"] for row in reports])),
            long_pair_bounded_and_truncation_reported=True,
            limitations=[
                "Direct model scores, no search/SQLite/UI or SearchReranker score cache in timing.",
                "First call includes session loading, pair tokenization and inference; "
                "file verification excluded.",
                "Warm batch lengths are short public text, not a 1024-token throughput benchmark.",
                "Sampled RSS is the whole process, including the long-pair probe, "
                "not isolated model allocations.",
                "CPU only; GPU must pass the runtime precision probe "
                "and is not verified on this host.",
                "Four obvious public smoke queries do not establish Telegram relevance "
                "or a quality threshold.",
                "Long pairs use explicit truncation; no sliding-window aggregation is implemented.",
            ],
        )
    finally:
        model.unload()
        stopped.set()
        monitor.join()
    write_report(
        output,
        report,
        root=ROOT,
        owner_key="benchmark",
        owner_value=VERSION,
        public_paths=(OUTPUT,),
    )
    print(
        json.dumps({"benchmark": VERSION, "probes": len(reports), "provider": report["provider"]})
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-workspace", type=Path, default=WORKSPACE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    run(parser.parse_args())
