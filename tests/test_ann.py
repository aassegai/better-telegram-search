import multiprocessing
import threading
import time
from types import SimpleNamespace

import psutil
import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("lancedb")

from telegram_search.search.ann import AnnOptions  # noqa: E402
from telegram_search.search.ann_process import AnnProcess, AnnUnavailable  # noqa: E402
from telegram_search.search.execution import (  # noqa: E402
    SearchCancelled,
    SearchExecution,
    execution_context,
)
from telegram_search.search.vectors import VectorStore  # noqa: E402
from telegram_search.shared.errors import UserError  # noqa: E402

OPTIONS = AnnOptions(partitions=16, probes=16, minimum_rows=32)


@pytest.fixture
def corpus(tmp_path):
    store = VectorStore(tmp_path)
    matrix = np.random.default_rng(72).normal(size=(2049, 24)).astype(np.float32)
    rows = [
        {
            "id": "quote'" if i == 0 else f"id-{i:05}",
            "chat_id": "first" if i % 2 else "second",
            "utc_day": "media" if i % 3 == 0 else "2026-01-01",
            "generation": 1,
        }
        for i in range(len(matrix))
    ]
    store.upsert("ann-test", 24, rows, matrix)
    try:
        yield store, rows, matrix
    finally:
        store.close()


def search(corpus, allowed=None, *, predicate=None, limit=20, query=None):
    store, rows, matrix = corpus
    allowed = {row["id"] for row in rows} if allowed is None else allowed
    calls = []

    def eligible(ids):
        assert len(ids) <= 512
        calls.append(len(ids))
        return allowed.intersection(ids)

    execution = SearchExecution()
    execution.requested_strategy = "ann"
    keys = sorted(allowed)
    with execution_context(execution):
        found = store.search_filtered(
            "ann-test",
            24,
            matrix[0] if query is None else query,
            eligible,
            limit,
            eligible_batches=(keys[start : start + 512] for start in range(0, len(keys), 512)),
            total=len(keys),
            predicate=predicate,
            ann_options=OPTIONS,
        )
    return found, execution.snapshot(), calls


def assert_same(actual, expected):
    assert [hit["id"] for hit in actual] == [hit["id"] for hit in expected]
    np.testing.assert_allclose(
        [hit["_distance"] for hit in actual],
        [hit["_distance"] for hit in expected],
        atol=1e-6,
    )


def test_real_ivf_index_broad_scope_and_honest_native_counters(corpus):
    store, rows, matrix = corpus
    built = store.build_ann("ann-test", 24, options=OPTIONS)
    assert built["ready"] and built["partitions"] == 16
    assert built["dimension"] == 24 and built["metric"] == "cosine"
    assert built["indexed_rows"] == len(rows) and built["unindexed_rows"] == 0
    actual, status, checks = search(corpus)
    expected = store.exact("ann-test", 24, matrix[0], [row["id"] for row in rows], 20)
    assert_same(actual, expected)
    assert status["effective_strategy"] == "ann" and not status["fallback_reasons"]
    assert status["total"] is None and status["eta_seconds"] is None
    assert status["completed"] == status["diagnostics"]["ann_queries"] == 1
    assert status["diagnostics"]["vectors_scored"] is None
    assert status["diagnostics"]["vectors_read"] is None
    assert status["diagnostics"]["eligibility_ids_read"] == len(rows)
    assert max(checks) <= 512
    pid = store.ann_process.process.pid
    search(corpus)
    assert store.ann_process.process.pid == pid  # imports reused, no per-query cold child


def test_resource_change_does_not_replace_query_worker_until_stopped(corpus, monkeypatch):
    store, rows, matrix = corpus
    store.build_ann("ann-test", 24, options=OPTIONS)
    expected, _, _ = search(corpus)
    native = store.ann_process
    pid = native.process.pid
    close = native.close
    store.ann_threads = 1
    monkeypatch.setattr(native, "close", lambda: False)
    try:
        actual, status, _ = search(corpus)
        assert_same(actual, expected)
        assert status["effective_strategy"] == "exact"
        assert status["fallback_reasons"] == ["ann_worker_stopping"]
        assert store.ann_process is native and native.process.pid == pid
    finally:
        monkeypatch.setattr(native, "close", close)
    actual, status, _ = search(corpus)
    assert_same(actual, expected)
    assert status["effective_strategy"] == "ann"
    assert store.ann_process.threads == 1 and store.ann_process.process.pid != pid


def test_canonical_id_batches_prefilter_native_top_k_and_escape_ids(corpus):
    store, rows, matrix = corpus
    store.build_ann("ann-test", 24, options=OPTIONS)
    allowed = {row["id"] for row in rows[:1200]}
    # The query's closest global vector is forbidden. Post-filtered global top-K
    # would fail to return the true top-K in this scope.
    native, scopes = AnnProcess(), []

    def query(**kwargs):
        scopes.append(kwargs["predicate"])
        return native.query(**kwargs)

    store.ann_process = SimpleNamespace(
        query=query, close=native.close, threads=4, memory_limit_mib=512
    )
    actual, status, _ = search(corpus, allowed, query=matrix[-1])
    expected = store.exact("ann-test", 24, matrix[-1], sorted(allowed), 20)
    assert_same(actual, expected)
    assert status["effective_strategy"] == "ann"
    assert len(scopes) == 3 and all("id IN (" in scope for scope in scopes)
    assert any("quote''" in scope for scope in scopes)
    assert all(scope.count(",") < 512 for scope in scopes)


@pytest.mark.parametrize("predicate", ["utc_day='media'", "utc_day<>'media'"])
def test_coarse_modality_predicate_is_preserved(corpus, predicate):
    store, rows, matrix = corpus
    store.build_ann("ann-test", 24, options=OPTIONS)
    allowed = {row["id"] for row in rows if (row["utc_day"] == "media") == ("<>" not in predicate)}
    actual, status, _ = search(corpus, allowed, predicate=predicate)
    expected = store.exact("ann-test", 24, matrix[0], sorted(allowed), 20)
    assert_same(actual, expected)
    assert status["effective_strategy"] == "ann"
    assert status["diagnostics"]["ann_queries"] == 1


def test_new_unindexed_tail_and_replaced_vectors_remain_searchable(corpus):
    store, rows, matrix = corpus
    store.build_ann("ann-test", 24, options=OPTIONS)
    fresh = dict(rows[0], id="new-tail", generation=2)
    rows.append(fresh)
    store.upsert("ann-test", 24, [fresh], matrix[:1])
    # Replacement deletes the indexed old row and publishes a new unindexed one.
    store.upsert("ann-test", 24, [dict(rows[1], generation=2)], -matrix[:1])
    assert store.ann_status("ann-test", 24)["unindexed_rows"] >= 2
    actual, status, _ = search(corpus)
    expected = store.exact("ann-test", 24, matrix[0], [row["id"] for row in rows], 20)
    assert_same(actual, expected)
    assert "new-tail" in [hit["id"] for hit in actual[:2]]
    assert status["effective_strategy"] == "ann"


def test_missing_index_narrow_scope_and_foreign_index_fall_back_explicitly(corpus):
    store, rows, matrix = corpus
    actual, status, _ = search(corpus)
    assert status["effective_strategy"] == "exact"
    assert status["fallback_reasons"] == ["ann_index_unavailable"]
    assert status["diagnostics"]["vectors_scored"] == len(rows)
    allowed = {row["id"] for row in rows[:10]}
    actual, status, _ = search(corpus, allowed)
    assert status["fallback_reasons"] == ["ann_scope_too_small"]
    assert_same(actual, store.exact("ann-test", 24, matrix[0], sorted(allowed), 20))
    assert store.ann_process is None
    store.table("ann-test", 24).create_index(
        index_type="IVF_FLAT",
        metric="l2",
        num_partitions=16,
        sample_rate=16,
        name="foreign-index",
    )
    _, status, _ = search(corpus)
    assert status["fallback_reasons"] == ["ann_index_incompatible"]
    assert store.ann_process is None


def test_small_table_build_and_default_exact_do_not_start_a_child(corpus):
    store, rows, matrix = corpus
    assert store.build_ann("missing", 24)["reason"] == "ann_index_unavailable"
    store.upsert("tiny", 24, rows[:4], matrix[:4])
    assert store.build_ann("tiny", 24)["reason"] == "ann_table_too_small"
    store.build_ann("ann-test", 24, options=OPTIONS)
    execution = SearchExecution()
    with execution_context(execution):
        actual = store.search_filtered("ann-test", 24, matrix[0], lambda ids: ids, 20)
    assert execution.snapshot()["effective_strategy"] == "exact"
    assert actual and store.ann_process is None


def test_failed_second_native_query_rescans_instead_of_reusing_consumed_cursor(corpus):
    store, rows, matrix = corpus
    store.build_ann("ann-test", 24, options=OPTIONS)
    allowed = {row["id"] for row in rows[:1200]}
    native, calls = AnnProcess(), []

    def query(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise AnnUnavailable("ann_query_timeout")
        return native.query(**kwargs)

    store.ann_process = SimpleNamespace(
        query=query, close=native.close, threads=4, memory_limit_mib=512
    )
    actual, status, _ = search(corpus, allowed, query=matrix[-1])
    assert_same(actual, store.exact("ann-test", 24, matrix[-1], sorted(allowed), 20))
    assert status["effective_strategy"] == "exact"
    assert status["fallback_reasons"] == ["ann_query_timeout"]
    assert status["diagnostics"]["ann_queries"] == 2
    assert status["diagnostics"]["vectors_scored"] is None
    assert status["diagnostics"]["exact_vectors_scored"] == len(allowed)


def test_native_candidate_outside_canonical_scope_triggers_full_retry(corpus):
    store, rows, matrix = corpus
    store.build_ann("ann-test", 24, options=OPTIONS)
    allowed = {row["id"] for row in rows[:1200]}
    store.ann_process = SimpleNamespace(
        query=lambda **kwargs: [{"id": rows[-1]["id"], "_distance": 0.0}],
        close=lambda: None,
        threads=4,
        memory_limit_mib=512,
    )
    actual, status, _ = search(corpus, allowed)
    assert_same(actual, store.exact("ann-test", 24, matrix[0], sorted(allowed), 20))
    assert status["fallback_reasons"] == ["ann_scope_changed"]


def _slow_native(connection, entered):
    connection.recv_bytes()
    entered.set()
    time.sleep(30)


@pytest.mark.parametrize("cancel", [True, False])
def test_cancel_and_timeout_kill_native_work_not_just_discard_results(tmp_path, cancel):
    context = multiprocessing.get_context("spawn")
    connection, child = context.Pipe()
    entered = context.Event()
    process = context.Process(target=_slow_native, args=(child, entered), daemon=True)
    process.start()
    child.close()
    pid = process.pid
    native = AnnProcess(timeout=0.2 if not cancel else 10)
    native.process, native.connection = process, connection
    execution, errors = SearchExecution(), []

    def run():
        try:
            with execution_context(execution):
                native.query(
                    location=tmp_path,
                    table="v",
                    version=1,
                    query=np.ones(24),
                    predicate=None,
                    probes=1,
                    limit=10,
                )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        if cancel:
            assert entered.wait(10)
            execution.cancelled.set()
        thread.join(5)
        assert not thread.is_alive() and len(errors) == 1
        assert isinstance(errors[0], SearchCancelled if cancel else AnnUnavailable)
        if not cancel:
            assert errors[0].reason == "ann_query_timeout"
        assert native.process is None and not psutil.pid_exists(pid)
    finally:
        native.close()
        thread.join(5)


def test_invalid_query_is_not_hidden_by_an_ann_fallback(corpus):
    for query in ([0.0] * 24, [float("nan")] * 24, [1.0]):
        with pytest.raises(UserError, match="недопустимый embedding"):
            search(corpus, query=query)


@pytest.mark.parametrize("method", ["search_filtered", "exact_filtered", "exact"])
def test_dimension_mismatch_fails_closed_in_ann_and_every_exact_path(corpus, method):
    store, rows, _ = corpus
    execution = SearchExecution()
    execution.requested_strategy = "ann"
    with execution_context(execution), pytest.raises(UserError, match="Размерность"):
        if method == "exact":
            store.exact("ann-test", 2, np.ones(2), [rows[0]["id"]], 20)
        else:
            options = {"ann_options": OPTIONS} if method == "search_filtered" else {}
            getattr(store, method)(
                "ann-test", 2, np.ones(2), lambda ids: ids, 20, total=len(rows), **options
            )
    assert store.ann_process is None


def test_rebuild_retires_only_old_owned_configurations_after_success(corpus):
    store, _, _ = corpus
    first = store.build_ann("ann-test", 24, options=AnnOptions(partitions=1))
    second = store.build_ann("ann-test", 24, options=AnnOptions(partitions=2))
    assert first["ready"] and second["ready"]
    assert first["name"] != second["name"]
    assert store.ann_status("ann-test", 24)["ready"]
    indices = [
        index for index in store.table("ann-test", 24).list_indices() if "vector" in index.columns
    ]
    assert [index.name for index in indices] == [second["name"]]
    _, status, _ = search(corpus)
    assert status["effective_strategy"] == "ann"


def _fast_native(connection, ready):
    ready.set()
    connection.recv_bytes()
    connection.send_bytes(b'{"results":[]}')
    connection.recv_bytes()


def test_fast_valid_reply_cannot_bypass_reusable_child_memory_budget(tmp_path, monkeypatch):
    context = multiprocessing.get_context("spawn")
    connection, child = context.Pipe()
    ready = context.Event()
    process = context.Process(target=_fast_native, args=(child, ready), daemon=True)
    process.start()
    child.close()
    pid = process.pid
    native = AnnProcess(memory_limit_mib=1)
    native.process, native.connection = process, connection
    check, checks = native._check_memory, []

    def skip_first_start_check(process):
        checks.append(process.pid)
        if len(checks) > 1:
            check(process)

    monkeypatch.setattr(native, "_check_memory", skip_first_start_check)
    try:
        assert ready.wait(10)
        with pytest.raises(AnnUnavailable, match="ann_memory_limit"):
            native.query(
                location=tmp_path,
                table="v",
                version=1,
                query=np.ones(24),
                predicate=None,
                probes=1,
                limit=10,
            )
        assert len(checks) >= 2
        assert native.process is None and not psutil.pid_exists(pid)
    finally:
        native.close()


def test_mixed_branch_strategy_and_unknown_counts_are_aggregated():
    execution = SearchExecution()
    execution.strategy("ann")
    execution.unknown_count("vectors_read", "vectors_scored")
    execution.strategy("exact", "ann_scope_too_small")
    execution.count("vectors_scored", 10)
    execution.count("exact_vectors_scored", 10)
    execution.strategy("exact", "ann_scope_too_small")
    status = execution.snapshot()
    assert status["effective_strategy"] == "mixed"
    assert status["fallback_reasons"] == ["ann_scope_too_small"]
    assert status["diagnostics"]["vectors_scored"] is None
    assert status["diagnostics"]["exact_vectors_scored"] == 10
