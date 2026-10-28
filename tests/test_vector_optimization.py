"""Real tiny-fragment compaction, retained snapshots and durable cleanup batches."""

import threading
import time
from dataclasses import asdict, is_dataclass
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("lancedb")

from conftest import export, load, message  # noqa: E402
from test_semantic import setup_index  # noqa: E402

from telegram_search.indexing.service import SemanticService  # noqa: E402
from telegram_search.search.vector_optimization import VectorOptimization  # noqa: E402
from telegram_search.search.vectors import VectorStore  # noqa: E402
from telegram_search.storage.generations import utc_day  # noqa: E402


def stats(table):
    value = table.stats()
    return asdict(value) if is_dataclass(value) else value


def fragmented_table():
    pins = {}
    tags = SimpleNamespace(
        list=lambda: dict(pins),
        create=lambda key, version: pins.update({key: version}),
        update=lambda key, version: pins.update({key: version}),
        delete=lambda key: pins.pop(key),
    )
    return SimpleNamespace(
        stats=lambda: {"fragment_stats": {"num_small_fragments": 500}}, tags=tags, version=1
    )


def test_real_bounded_compaction_keeps_ids_vectors_and_old_reader(tmp_path):
    store = VectorStore(tmp_path)
    table = store.table("synthetic", 4, create=True)
    for i in range(140):
        table.add(
            [
                {
                    "id": str(i),
                    "chat_id": "chat",
                    "utc_day": "day",
                    "generation": 1,
                    "vector": [1.0, float(i), 0.0, 0.0],
                }
            ]
        )
    before = stats(table)
    expected = table.search().limit(None).to_arrow().to_pylist()
    snapshot = store.db.open_table("text_synthetic")
    snapshot.checkout(table.version)
    try:
        assert before["fragment_stats"]["num_small_fragments"] >= 128
        assert store.maybe_optimize([{"id": "synthetic", "dimension": 4}], memory_limit_mib=1024)
        current = store.table("synthetic", 4)
        assert stats(current)["fragment_stats"]["num_fragments"] < 10
        assert sorted(
            current.search().limit(None).to_arrow().to_pylist(), key=lambda r: r["id"]
        ) == sorted(expected, key=lambda r: r["id"])
        assert sorted(
            snapshot.search().limit(None).to_arrow().to_pylist(), key=lambda r: r["id"]
        ) == sorted(expected, key=lambda r: r["id"])
        row = {"id": "0", "chat_id": "chat", "utc_day": "day", "generation": 2}
        store.upsert("synthetic", 4, [row], np.array([[0.0, 1.0, 0.0, 0.0]], dtype=np.float32))
        assert store.table("synthetic", 4).count_rows() == 140  # replay stays idempotent
        assert store.optimization.status()["state"] == "done"
    finally:
        store.close()


def test_small_table_does_not_spawn_or_recheck_every_day(tmp_path, monkeypatch):
    store = VectorStore(tmp_path)
    table = store.table("small", 4, create=True)
    table.add(
        [
            {
                "id": "x",
                "chat_id": "chat",
                "utc_day": "day",
                "generation": 1,
                "vector": [1.0, 0.0, 0.0, 0.0],
            }
        ]
    )
    calls = []
    original = store.table

    def opened(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "table", opened)
    assert not store.maybe_optimize([{"id": "small", "dimension": 4}])
    assert not store.maybe_optimize([{"id": "small", "dimension": 4}])
    assert len(calls) == 1 and store.optimization.process is None
    store.close()


def test_prune_many_days_scans_once_and_preserves_newer_generations(tmp_path, monkeypatch):
    store = VectorStore(tmp_path)
    table = store.table("space", 4, create=True)
    table.add(
        [
            {
                "id": f"{day}-{gen}",
                "chat_id": "chat",
                "utc_day": day,
                "generation": gen,
                "vector": [1.0, 0.0, 0.0, 0.0],
            }
            for day in ("a", "b", "unrelated")
            for gen in (1, 2, 3)
        ]
    )
    scanned = []
    count = table.count_rows

    def counted(predicate=None):
        scanned.append(predicate)
        return count(predicate)

    monkeypatch.setattr(table, "count_rows", counted)
    monkeypatch.setattr(store, "table", lambda *a, **k: table)
    store.prune_segments(
        [{"id": "space", "dimension": 4}],
        [
            {"chat_id": "chat", "utc_day": day, "keep_space_id": "space", "generation": 2}
            for day in ("a", "b")
        ],
    )
    assert len(scanned) == 1
    assert {r["id"] for r in table.search().limit(None).to_list()} == {
        "a-2",
        "a-3",
        "b-2",
        "b-3",
        "unrelated-1",
        "unrelated-2",
        "unrelated-3",
    }
    store.close()


def test_worker_leaves_cleanup_outbox_and_service_drains_it(db, importer, tmp_path, monkeypatch):
    chat, service, worker, work = setup_index(db, importer, tmp_path)
    monkeypatch.setattr(worker.vectors, "prune_segment", lambda *a: pytest.fail("per-day scan"))
    assert worker.run(work)["state"] == "done"
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM vector_segment_cleanup").fetchone()[0] == 1
    service.cleanup()
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM vector_segment_cleanup").fetchone()[0] == 0
    service.shutdown()


def test_background_defers_small_cleanup_then_flushes_at_idle_or_deadline(
    db, importer, tmp_path, monkeypatch
):
    load(importer, export(tmp_path / "export", [message()]))
    service = SemanticService(db, importer.lifecycle_lock, start_background=False)
    service.available = service.background_enabled = True
    with db.connect() as conn:
        conn.execute("INSERT INTO embedding_spaces VALUES('s','small','{}',4,0)")
        conn.execute("UPDATE semantic_state SET enabled=1,paused=0")
        chat = conn.execute("SELECT id FROM chats").fetchone()[0]
        conn.execute(
            "INSERT INTO vector_segment_cleanup VALUES(?,?, 's',1,NULL)",
            (chat, utc_day(1750000000)),
        )
    calls = []
    monkeypatch.setattr(
        service, "_vector_store", lambda: SimpleNamespace(prune_segments=lambda *a: calls.append(a))
    )
    service.cleanup()
    assert calls == []
    service._segment_cleanup_at = time.monotonic() - 1
    service.cleanup()
    assert len(calls) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM vector_segment_cleanup").fetchone()[0] == 0


def test_maintenance_failure_backs_off_without_pausing_indexing(tmp_path, monkeypatch):
    from telegram_search.search import vector_optimization as module

    optimization = VectorOptimization()
    table = fragmented_table()
    entered = []

    class Broken:
        def __init__(self, **kwargs):
            entered.append(kwargs)

        def optimize(self, **kwargs):
            raise OSError("PRIVATE/path")

        def close(self):
            return True

    monkeypatch.setattr(module, "AnnProcess", Broken)
    store = SimpleNamespace(
        table=lambda *a: table,
        location=tmp_path,
        _ann_for_maintenance=lambda *a: None,
        lock=threading.RLock(),
        _ann_dirty=lambda *a: None,
    )
    for _ in range(2):
        assert not optimization.run(
            store, [{"id": "space", "dimension": 4}], threads=2, memory_limit_mib=512
        )
    assert len(entered) == 1 and entered[0]["background"]
    assert optimization.status()["state"] == "deferred"
    assert "PRIVATE" not in str(optimization.status())
    assert optimization.process is None


def test_shutdown_cancels_before_a_late_native_writer_can_start(tmp_path, monkeypatch):
    from telegram_search.search import vector_optimization as module
    from telegram_search.search.execution import checkpoint

    optimization = VectorOptimization()
    entered, release = threading.Event(), threading.Event()
    started = []

    class Held:
        def __init__(self, **kwargs):
            pass

        def close(self):
            return True

        def optimize(self, **kwargs):
            entered.set()
            assert release.wait(15)
            checkpoint()
            started.append(True)

    monkeypatch.setattr(module, "AnnProcess", Held)
    table = fragmented_table()
    store = SimpleNamespace(
        table=lambda *a: table,
        location=tmp_path,
        _ann_for_maintenance=lambda *a: None,
        lock=threading.RLock(),
        _ann_dirty=lambda *a: None,
    )
    thread = threading.Thread(
        target=lambda: optimization.run(
            store, [{"id": "s", "dimension": 4}], threads=1, memory_limit_mib=512
        )
    )
    thread.start()
    assert entered.wait(2)
    optimization.close()
    release.set()
    thread.join(3)
    assert not thread.is_alive() and started == []


def test_compaction_keeps_readers_and_settings_responsive_while_publication_waits(
    tmp_path, monkeypatch
):
    from telegram_search.search import vector_optimization as module

    store = VectorStore(tmp_path)
    row = {"id": "x", "chat_id": "c", "utc_day": "d", "generation": 1}
    store.upsert("s", 4, [row], np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32))
    store.optimization.small_fragments = 1
    entered, release, published = threading.Event(), threading.Event(), threading.Event()
    lifecycle = threading.RLock()

    class Held:
        def __init__(self, **kwargs):
            pass

        def close(self):
            return True

        def optimize(self, **kwargs):
            entered.set()
            assert release.wait(15)

    monkeypatch.setattr(module, "AnnProcess", Held)
    optimizer = threading.Thread(target=lambda: store.maybe_optimize([{"id": "s", "dimension": 4}]))

    def publish():
        with store.publication(lifecycle):
            store.upsert(
                "s", 4, [dict(row, id="y")], np.array([[0.0, 1.0, 0.0, 0.0]], dtype=np.float32)
            )
        published.set()

    optimizer.start()
    assert entered.wait(2)
    publisher = threading.Thread(target=publish)
    publisher.start()
    queried, hits = threading.Event(), []

    def query():
        hits.extend(
            store.exact_filtered(
                "s", 4, np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32), lambda ids: ids, 1
            )
        )
        queried.set()

    reader = threading.Thread(target=query)
    try:
        assert not published.wait(0.1)
        assert lifecycle.acquire(timeout=0.2)
        lifecycle.release()
        reader.start()
        assert queried.wait(3)  # Must complete BEFORE compaction is released.
        assert hits[0]["id"] == "x"
    finally:
        release.set()
        optimizer.join(3)
        publisher.join(3)
        if reader.ident is not None:
            reader.join(3)
        store.close()
    assert published.is_set()


def test_terminal_shutdown_does_not_release_a_live_native_writer(db):
    service = SemanticService(db, threading.RLock(), start_background=False)
    service.vectors = VectorStore(db.workspace)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class HeldWriter:
        def close(self):
            entered.set()
            return release.is_set()

    service.vectors.optimization.process = HeldWriter()
    thread = threading.Thread(target=lambda: (service.shutdown(), finished.set()))
    thread.start()
    try:
        assert entered.wait(2)
        assert not finished.wait(0.15)
        assert service.lock.acquire(timeout=0.2)
        service.lock.release()
    finally:
        release.set()
        thread.join(3)
    assert finished.is_set() and not service.vectors.optimization.writer_active


def test_maintenance_preserves_a_manually_queued_ann_build(db, monkeypatch):
    from telegram_search.indexing.ann import AnnIndexService
    from telegram_search.search import vector_optimization as module

    store = VectorStore(db.workspace)
    store.upsert(
        "s",
        4,
        [{"id": "x", "chat_id": "c", "utc_day": "d", "generation": 1}],
        np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32),
    )
    semantic = SimpleNamespace(
        available=True,
        vectors=store,
        encoder=SimpleNamespace(space_id="s", spec=SimpleNamespace(dimension=4)),
        _vector_store=lambda: store,
    )
    service = AnnIndexService(
        db, semantic, SimpleNamespace(clip=None), threading.RLock(), start_background=False
    )
    service._step()
    service.prepare("s")
    store.optimization.small_fragments = 1

    class Immediate:
        def __init__(self, **kwargs):
            pass

        def close(self):
            return True

        def optimize(self, **kwargs):
            pass

    monkeypatch.setattr(module, "AnnProcess", Immediate)
    try:
        assert store.maybe_optimize([{"id": "s", "dimension": 4}])
        assert "s" in service.forced
        assert service.status()["entries"][0]["state"] == "queued"
    finally:
        service.close()
        store.close()


def test_cleanup_failure_retries_and_cas_keeps_a_newer_outbox_entry(db, monkeypatch):
    service = SemanticService(db, threading.RLock(), start_background=False)
    service.available = True
    with db.connect() as conn:
        conn.execute("INSERT INTO embedding_spaces VALUES('s','small','{}',4,0)")
        conn.executemany(
            "INSERT INTO vector_segment_cleanup VALUES(?,'day','s',1,NULL)",
            [(str(i),) for i in range(65)],
        )
    calls = []

    def prune(spaces, rows):
        calls.append(rows)
        if len(calls) == 1:
            raise OSError("interrupted native cleanup")
        if len(calls) == 2:
            with db.connect() as conn:
                conn.execute(
                    "UPDATE vector_segment_cleanup SET generation=2 WHERE chat_id=?",
                    (rows[0]["chat_id"],),
                )

    monkeypatch.setattr(service, "_vector_store", lambda: SimpleNamespace(prune_segments=prune))
    with pytest.raises(OSError):
        service.cleanup()
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM vector_segment_cleanup").fetchone()[0] == 65
    service.cleanup()
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM vector_segment_cleanup").fetchone()[0] == 2
    service.cleanup()
    assert [len(rows) for rows in calls] == [64, 64, 2]
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM vector_segment_cleanup").fetchone()[0] == 0


def test_pin_keeps_actual_snapshot_vectors_when_cleanup_can_remove_its_version(
    tmp_path, monkeypatch
):
    from datetime import timedelta

    from telegram_search.search import vector_optimization as module
    from telegram_search.search.vector_files import optimize_files

    store = VectorStore(tmp_path)
    table = store.table("s", 4, create=True)
    for i in range(3):
        table.add(
            [
                {
                    "id": str(i),
                    "chat_id": "c",
                    "utc_day": "d",
                    "generation": 1,
                    "vector": [1.0, 0.0, 0.0, 0.0],
                }
            ]
        )
    expected = sorted(table.search().limit(None).to_arrow().to_pylist(), key=lambda r: r["id"])
    observed = []
    store.optimization.small_fragments = 1

    class Pruning:
        def __init__(self, **kwargs):
            pass

        def close(self):
            return True

        def optimize(self, **kwargs):
            reader = store.db.open_table("text_s")
            reader.checkout(table.version)
            current = store.db.open_table("text_s")
            # Zero age models an age-eligible historical latest version. Without
            # the pin count_rows still passes but reading the vectors fails.
            before = current.version
            outcome = optimize_files(current, cleanup_older_than=timedelta(0))
            assert outcome == {"optimized": True, "cleanup_deferred": True}
            assert current.version > before
            assert stats(current)["fragment_stats"]["num_fragments"] == 1
            observed.extend(reader.search().limit(None).to_arrow().to_pylist())
            return outcome

    monkeypatch.setattr(module, "AnnProcess", Pruning)
    try:
        assert store.maybe_optimize([{"id": "s", "dimension": 4}]), observed
        assert sorted(observed, key=lambda r: r["id"]) == expected
        assert VectorOptimization.reader_tag not in table.tags.list()
    finally:
        store.close()


@pytest.mark.parametrize("known,advanced", [(True, True), (True, False), (False, True)])
def test_native_cleanup_refusal_requires_exact_error_and_committed_version(known, advanced):
    from telegram_search.search.vector_files import optimize_files

    table = SimpleNamespace(version=1, checkout_latest=lambda: None)

    def optimize(**kwargs):
        if advanced:
            table.version = 2
        message = (
            "lance error: Cleanup error: 1 tagged version(s) have been marked for cleanup."
            if known
            else "IO error: /synthetic/Cleanup error:/tagged version(s)"
        )
        raise RuntimeError(message)

    table.optimize = optimize
    if known and advanced:
        assert optimize_files(table) == {"optimized": True, "cleanup_deferred": True}
    else:
        with pytest.raises(RuntimeError):
            optimize_files(table)


def test_lazy_media_cleanup_waits_for_first_optimizer_without_holding_lifecycle(
    db, importer, monkeypatch
):
    from telegram_search.indexing.media import MediaService
    from telegram_search.search import vector_optimization as module

    semantic = SemanticService(db, importer.lifecycle_lock, start_background=False)
    media = MediaService(db, importer.lifecycle_lock, semantic, start_background=False)
    store = VectorStore(db.workspace)
    sha = "a" * 64
    store.upsert(
        "visual",
        4,
        [{"id": "x", "chat_id": sha, "utc_day": "media", "generation": 1}],
        np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32),
    )
    store.optimization.small_fragments = 1
    with db.connect() as conn:
        conn.execute("INSERT INTO media_spaces VALUES('visual',4,'image')")
        conn.execute("INSERT INTO media_vector_cleanup VALUES(?)", (sha,))
    entered, release = threading.Event(), threading.Event()

    class Held:
        def __init__(self, **kwargs):
            pass

        def close(self):
            return True

        def optimize(self, **kwargs):
            entered.set()
            assert release.wait(15)

    monkeypatch.setattr(module, "AnnProcess", Held)
    optimizer = threading.Thread(
        target=lambda: store.maybe_optimize([{"id": "visual", "dimension": 4}])
    )

    def lazy():
        semantic.vectors = store
        optimizer.start()
        assert entered.wait(3)
        return store

    monkeypatch.setattr(semantic, "_vector_store", lazy)
    finished, errors = threading.Event(), []

    def cleanup():
        try:
            media.cleanup()
        except Exception as error:
            errors.append(type(error).__name__)
        finally:
            finished.set()

    worker = threading.Thread(target=cleanup)
    worker.start()
    try:
        assert entered.wait(3)
        assert not finished.wait(0.1)
        assert importer.lifecycle_lock.acquire(timeout=0.2)
        importer.lifecycle_lock.release()
    finally:
        # Subsequent calls return the already initialized store normally.
        monkeypatch.setattr(semantic, "_vector_store", lambda: store)
        release.set()
        optimizer.join(3)
        worker.join(3)
        media.shutdown()
        semantic.shutdown()
    assert finished.is_set() and errors == []
