from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pa = pytest.importorskip("pyarrow")
pytest.importorskip("lancedb")

from telegram_search.search.execution import SearchExecution, execution_context  # noqa: E402
from telegram_search.search.vectors import VectorStore  # noqa: E402
from telegram_search.shared.errors import UserError  # noqa: E402


def test_streamed_cosine_matches_native_search_with_filters_and_unnormalized_vectors(tmp_path):
    store = VectorStore(tmp_path)
    rng = np.random.default_rng(42)
    matrix = rng.normal(size=(1301, 17)).astype(np.float32)
    rows = [
        {"id": f"{i:08x}", "chat_id": "synthetic", "utc_day": "2026-01-01", "generation": 1}
        for i in range(len(matrix))
    ]
    store.upsert("test", 17, rows, matrix)
    query = rng.normal(size=17).astype(np.float32)
    for allowed in ({row["id"] for row in rows[::3]}, {rows[-1]["id"]}, set()):
        batches = []

        def eligible(ids, batches=batches, allowed=allowed):
            batches.append(ids)
            return allowed.intersection(ids)

        streamed = store.exact_filtered("test", 17, query, eligible, 30)
        native = store.exact("test", 17, query, sorted(allowed), 30)
        assert [hit["id"] for hit in streamed] == [hit["id"] for hit in native]
        np.testing.assert_allclose(
            [hit["_distance"] for hit in streamed],
            [hit["_distance"] for hit in native],
            atol=1e-6,
        )
        assert all(len(batch) <= 512 for batch in batches)
        assert len({key for batch in batches for key in batch}) == len(matrix)


def test_scan_handles_arrow_offsets_large_batches_nulls_and_stable_ties(tmp_path, monkeypatch):
    count = 1100
    ids = [f"{i:08x}" for i in range(count)]
    matrix = [[1.0, 0.0] for _ in ids]
    matrix[13] = None
    matrix[14] = [0.0, 0.0]
    matrix[15] = [float("nan"), 1.0]
    batch = pa.record_batch(
        [pa.array(ids), pa.array(matrix, type=pa.list_(pa.float32(), 2))],
        names=["id", "vector"],
    ).slice(11, 1080)
    calls = []

    class Query:
        def select(self, columns):
            return self

        def limit(self, limit):
            return self

        def to_batches(self, batch_size):
            calls.append(batch_size)
            return pa.RecordBatchReader.from_batches(batch.schema, [batch])

    store = VectorStore(tmp_path)
    monkeypatch.setattr(store, "table", lambda *args: SimpleNamespace(search=lambda: Query()))
    seen = []

    def eligible(keys):
        assert len(keys) <= 512
        seen.extend(keys)
        return keys

    found = store.exact_filtered("test", 2, [1.0, 0.0], eligible, 2000)
    expected = sorted(set(ids[11:1091]) - set(ids[13:16]), reverse=True)
    assert [hit["id"] for hit in found] == expected
    assert len(seen) == 1080 and len(set(seen)) == 1080
    assert calls == [512]
    for query in ([0.0, 0.0], [float("nan"), 1.0], [1.0]):
        with pytest.raises(UserError, match="недопустимый embedding"):
            store.exact_filtered("test", 2, query, eligible, 1)


def test_canonical_id_pushdown_reads_only_scoped_vectors_including_fresh_tail(tmp_path):
    store = VectorStore(tmp_path)
    rng = np.random.default_rng(12)
    matrix = rng.normal(size=(1401, 13)).astype(np.float32)
    rows = [
        {"id": f"{i:08x}", "chat_id": "synthetic", "utc_day": "2026-01-01", "generation": 1}
        for i in range(len(matrix))
    ]
    store.upsert("scope", 13, rows[:1000], matrix[:1000])
    store.upsert("scope", 13, rows[1000:], matrix[1000:])
    allowed = [row["id"] for row in rows[::13]]
    batches = [allowed[index : index + 31] for index in range(0, len(allowed), 31)]
    execution = SearchExecution()
    with execution_context(execution):
        found = store.exact_filtered(
            "scope",
            13,
            matrix[-1],
            None,
            30,
            eligible_batches=iter(batches),
            total=len(allowed),
        )
    native = store.exact("scope", 13, matrix[-1], allowed, 30)
    assert [hit["id"] for hit in found] == [hit["id"] for hit in native]
    measured = execution.snapshot()
    assert measured["completed"] == measured["total"] == len(allowed)
    assert measured["diagnostics"]["vectors_read"] == len(allowed)
    assert measured["diagnostics"]["vectors_scored"] == len(allowed)
    assert measured["completed"] < len(matrix)


def test_full_clear_recreates_id_index_without_restarting_the_store(tmp_path):
    store = VectorStore(tmp_path)
    rows = [{"id": "one", "chat_id": "synthetic", "utc_day": "day", "generation": 1}]
    store.upsert("scope", 2, rows, np.array([[1.0, 0.0]], dtype=np.float32))
    assert any("id" in index.columns for index in store.table("scope", 2).list_indices())
    store.clear([{"id": "scope", "dimension": 2}], predicate="utc_day='day'")
    store.upsert("scope", 2, rows, np.array([[1.0, 0.0]], dtype=np.float32))
    assert any("id" in index.columns for index in store.table("scope", 2).list_indices())
