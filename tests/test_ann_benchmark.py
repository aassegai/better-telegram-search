import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_search.evaluation.ann import SCOPES, CanonicalScope, corpus, latency, recall

ROOT = Path(__file__).resolve().parents[1]


def script():
    spec = importlib.util.spec_from_file_location(
        "ann_benchmark", ROOT / "scripts/benchmark_ann_embeddings.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def topics():
    return json.loads(
        (ROOT / "tests/fixtures/synthetic/relevance.json").read_text(encoding="utf-8")
    )


def test_generated_corpus_unique_and_deterministic():
    first = corpus(topics(), 4096)
    assert first == corpus(topics(), 4096)
    texts, rows = first
    assert len(set(texts)) == len({row["id"] for row in rows}) == 4096
    assert {row["chat_id"] for row in rows} == {f"chat-{i}" for i in range(4)}
    assert {row["generation"] for row in rows} == {1}
    assert sum(row["utc_day"] == "media" for row in rows) == 820
    with pytest.raises(ValueError):
        corpus(topics(), 0)


def test_canonical_scope_streams_and_combines_filters(tmp_path):
    _, rows = corpus(topics(), 4096)
    witness = CanonicalScope(tmp_path / "canonical.sqlite", rows)
    try:
        for scope in SCOPES:
            total = witness.select(scope)
            batches = list(witness.batches())
            assert total == sum(map(len, batches)) > 0
            assert max(map(len, batches)) <= 512
            assert all(set(batch) == set(witness.eligible(batch)) for batch in batches)
        witness.select(next(s for s in SCOPES if s.name == "combined"))
        selected = {key for batch in witness.batches() for key in batch}
        assert selected == {
            row["id"]
            for row in rows
            if row["chat_id"] == "chat-1"
            and row["author"] == "author-2"
            and row["day"] <= "2026-01-14"
        }
        with pytest.raises(ValueError, match="Unbounded"):
            witness.eligible(["id"] * 513)
    finally:
        witness.close()


def test_metrics_small_scope_and_duplicate_detection():
    assert recall([{"id": "a"}, {"id": "b"}], [{"id": "a"}]) == 0.5
    assert recall([], []) is None
    with pytest.raises(ValueError, match="Duplicate"):
        recall([{"id": "a"}], [{"id": "a"}, {"id": "a"}])
    assert latency(list(range(1, 21))) == {"samples": 20, "p50_ms": 10.5, "p95_ms": 19}
    for values in ([], [float("nan")], [-1]):
        with pytest.raises(ValueError):
            latency(values)


@pytest.mark.parametrize("interrupt", [False, True])
def test_candidate_start_failure_and_interrupted_wait_close_process(
    tmp_path, monkeypatch, interrupt
):
    module = script()
    events = []

    class FakeProcess:
        pid = None
        exitcode = None
        alive = False

        def start(self):
            events.append("start")
            if not interrupt:
                raise OSError("synthetic start failure")
            self.pid, self.alive = 123, True

        def is_alive(self):
            assert self.pid is not None
            return self.alive

        def join(self, timeout=None):
            events.append("join")
            if self.alive:
                raise KeyboardInterrupt()

        def kill(self):
            events.append("kill")
            self.alive = False

        def close(self):
            assert not self.alive
            events.append("close")

    fake = FakeProcess()
    monkeypatch.setattr(
        module.multiprocessing,
        "get_context",
        lambda _method: SimpleNamespace(Process=lambda **_kwargs: fake),
    )
    with pytest.raises(KeyboardInterrupt if interrupt else OSError):
        module.run_candidate(tmp_path, "IVF_FLAT", {})
    assert events == (
        ["start", "join", "kill", "join", "close"] if interrupt else ["start", "close"]
    )


@pytest.mark.parametrize("kind", ["IVF_FLAT", "IVF_HNSW_SQ", "IVF_HNSW_FLAT"])
def test_native_worker_exercises_filters_and_unindexed_tail(tmp_path, kind):
    np = pytest.importorskip("numpy")
    lancedb = pytest.importorskip("lancedb")
    if kind == "IVF_HNSW_FLAT" and lancedb.__version__ == "0.25.3":
        pytest.skip("LanceDB 0.25.3 does not support IVF_HNSW_FLAT")
    module = script()
    _, rows = corpus(topics(), 512)
    vectors = np.random.default_rng(91).normal(size=(512, 24)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    queries = vectors[:4]
    np.savez(tmp_path / "embeddings.npz", vectors=vectors, queries=queries)
    (tmp_path / "rows.json").write_text(json.dumps(rows))
    module.worker(tmp_path, kind, dict(threads=2, partitions=4, probes=[4], ef=128, refine=4))
    report = json.loads((tmp_path / f"{kind}.json").read_text(encoding="utf-8"))
    assert report["native_index_bytes"] > 0 and report["build_seconds"] > 0
    assert report["fresh_unindexed_rows"] == report["fresh_changes"]["known_fresh_top10_hits"] == 4
    for scope in report["scopes"]["4"].values():
        assert scope["recall_at_10_mean"] >= 0.95
        assert scope["last_native_diagnostics"]["ann_queries"] > 0
        if kind == "IVF_FLAT":
            assert scope["application_ann"]["effective_strategies"] == ["exact"]
            assert scope["application_ann"]["fallback_reasons"] == ["ann_scope_too_small"]
            assert scope["application_ann"]["recall_at_10_mean"] == 1
