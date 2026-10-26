import json
import multiprocessing
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
from fastapi.testclient import TestClient

np = pytest.importorskip("numpy")
pytest.importorskip("lancedb")

from telegram_search.backend.api import create_app  # noqa: E402
from telegram_search.indexing.ann import AnnIndexService  # noqa: E402
from telegram_search.search.ann_process import AnnProcess, AnnUnavailable  # noqa: E402
from telegram_search.search.cache import SearchCache  # noqa: E402
from telegram_search.search.execution import SearchExecution  # noqa: E402
from telegram_search.search.vectors import VectorStore  # noqa: E402
from telegram_search.shared.errors import UserError  # noqa: E402
from telegram_search.sources.service import WorkspaceService  # noqa: E402


def wait_until(predicate, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if result := predicate():
            return result
        time.sleep(0.02)
    pytest.fail("ANN worker did not reach the expected state")


@pytest.fixture
def archive(db):
    store = VectorStore(db.workspace)
    matrix = np.random.default_rng(39).normal(size=(1024, 16)).astype(np.float32)
    rows = [
        {"id": str(i), "chat_id": "chat", "utc_day": "day", "generation": 1}
        for i in range(len(matrix))
    ]
    store.upsert("space", 16, rows, matrix)
    semantic = SimpleNamespace(
        available=True,
        vectors=store,
        encoder=SimpleNamespace(space_id="space", spec=SimpleNamespace(dimension=16)),
        _vector_store=lambda: store,
    )
    media = SimpleNamespace(clip=None)
    lifecycle = threading.RLock()
    service = AnnIndexService(db, semantic, media, lifecycle, start_background=False)
    service._step()
    try:
        yield service, store, matrix, rows, semantic, media, lifecycle
    finally:
        service.close()
        store.close()


def ready(service):
    value = service.status()
    return value["entries"] and value["entries"][0]["ready"] and value["running"] is None


def test_native_background_prepare_and_restart_adopt_existing_index(archive, db):
    service, store, _, _, semantic, media, lifecycle = archive
    assert not service.status()["enabled"]
    assert service.status()["entries"][0]["state"] == "not_prepared"
    service.prepare()
    service.start()
    wait_until(lambda: ready(service))
    first = service.status()["entries"][0]
    assert first["indexed_rows"] == 1024 and first["metric"] == "cosine"
    assert first["total"] is None
    service.close()
    restored = AnnIndexService(db, semantic, media, lifecycle, start_background=False)
    try:
        restored._step()
        assert ready(restored)
        assert restored.status()["entries"][0]["name"] == first["name"]
        assert restored.current is None
    finally:
        restored.close()
    assert store.ann_status("space", 16)["ready"]


def _controlled_build(connection, entered, release):
    import lancedb

    from telegram_search.search.ann import AnnOptions, build_index

    request = json.loads(connection.recv_bytes())
    table = lancedb.connect(request["location"]).open_table(request["table"])
    entered.set()
    end = time.monotonic() + 15
    while not Path(release).exists():
        if time.monotonic() > end:
            return
        time.sleep(0.02)
    result = build_index(table, request["dimension"], AnnOptions(**request["options"]))
    connection.send_bytes(json.dumps({"status": result}).encode())
    try:
        connection.recv_bytes()
    except EOFError:
        pass


def controlled_factory(monkeypatch, root):
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release_path = root / "release-native-test"
    # A killed child must not hold a multiprocessing.Condition mutex that the
    # parent will later acquire during cleanup.
    release = SimpleNamespace(set=release_path.touch)
    children = []

    def factory(**kwargs):
        native = AnnProcess(**kwargs)
        connection, child = context.Pipe()
        process = context.Process(
            target=_controlled_build, args=(child, entered, release_path), daemon=True
        )
        process.start()
        child.close()
        native.process, native.connection = process, connection
        children.append(process.pid)
        return native

    monkeypatch.setattr("telegram_search.indexing.ann.AnnProcess", factory)
    return entered, release, children


def test_training_releases_lifecycle_and_vector_locks_and_keeps_concurrent_append(
    archive, monkeypatch
):
    service, store, matrix, rows, _, _, lifecycle = archive
    entered, release, children = controlled_factory(monkeypatch, store.workspace)
    service.prepare()
    service.start()
    try:
        assert entered.wait(15)
        assert service.status()["entries"][0]["state"] == "building"
        assert lifecycle.acquire(timeout=0.2)
        lifecycle.release()
        # Routine cleanup with no stale rows is read-only and must not starve
        # a long build while ordinary indexing keeps publishing new segments.
        store.prune_segment([{"id": "space", "dimension": 16}], "chat", "day", "space", 1)
        store.prune_media("space", 16, lambda ids: set(ids))
        assert not service.current.invalidated
        # The native builder has opened the old table. Queries and a real native
        # append must proceed before it commits its index.
        found = store.exact_filtered("space", 16, matrix[0], lambda ids: ids, 1)
        assert found[0]["id"] == rows[0]["id"]
        fresh = {**rows[0], "id": "new", "generation": 2}
        store.upsert("space", 16, [fresh], -matrix[:1])
        release.set()
        wait_until(lambda: ready(service))
        native = store.table("space", 16)
        assert native.count_rows() == 1025
        nearest = native.search(-matrix[0]).distance_type("cosine").nprobes(64).limit(1).to_list()
        assert nearest[0]["id"] == "new"
    finally:
        release.set()
        service.close()
    assert all(not psutil.pid_exists(pid) for pid in children)


@pytest.mark.parametrize(
    "operation", ["clear", "compact", "delete_chat", "prune_media", "prune_segment"]
)
def test_destructive_operations_stop_real_writer_before_modifying_table(
    archive, monkeypatch, operation
):
    service, store, matrix, rows, *_ = archive
    if operation == "prune_media":
        store.upsert("space", 16, [dict(row, utc_day="media") for row in rows], matrix)
    entered, release, children = controlled_factory(monkeypatch, store.workspace)
    service.prepare()
    service.start()
    assert entered.wait(15)
    spaces = [{"id": "space", "dimension": 16}]
    try:
        if operation == "clear":
            original = store.db.drop_table

            def drop(name):
                assert not psutil.pid_exists(children[0])
                original(name)

            monkeypatch.setattr(store.db, "drop_table", drop)
            store.clear(spaces, predicate="utc_day='day'")
        elif operation == "compact":
            store.compact(spaces)
        elif operation == "delete_chat":
            store.delete_chat(spaces, "chat")
        elif operation == "prune_media":
            store.prune_media("space", 16, lambda ids: set())
        else:
            store.prune_segment(spaces, "chat", "day", "space", 2)
        assert not psutil.pid_exists(children[0])
        if operation in {"clear", "delete_chat", "prune_segment"}:
            store.upsert(
                "space", 16, [dict(rows[0], id="new-generation", generation=2)], matrix[:1]
            )
            assert store.table("space", 16).count_rows() == 1
        release.set()
        wait_until(lambda: service.current is None)
        assert not store.ann_status("space", 16)["ready"]
        assert service.status()["entries"][0]["state"] != "ready"
    finally:
        release.set()


def test_clear_cancels_lease_before_late_native_process_creation(archive, monkeypatch):
    service, store, matrix, rows, *_ = archive
    entered, release = threading.Event(), threading.Event()
    original = AnnProcess._start

    def held_start(self):
        entered.set()
        assert release.wait(15)
        return original(self)

    monkeypatch.setattr(AnnProcess, "_start", held_start)
    service.prepare()
    service.start()
    try:
        assert entered.wait(15)
        job = service.current
        assert job.process.process is None
        store.clear([{"id": "space", "dimension": 16}], predicate="utc_day='day'")
        store.upsert("space", 16, [dict(rows[0], id="replacement")], matrix[:1])
        release.set()
        wait_until(lambda: service.current is None)
        assert job.process.process is None
        assert store.table("space", 16).count_rows() == 1
        assert not store.ann_status("space", 16)["ready"]
    finally:
        release.set()


def test_auto_build_coalesces_new_tail_and_error_needs_explicit_retry(archive, db, monkeypatch):
    service, store, matrix, rows, *_ = archive
    db.settings = replace(db.settings, ann_indexing_enabled=True)
    service.start()
    wait_until(lambda: ready(service))
    first = service.status()["entries"][0]["name"]
    store.upsert("space", 16, [dict(row, id="tail-" + row["id"]) for row in rows[:32]], matrix[:32])
    wait_until(lambda: service.status()["entries"][0].get("unindexed_rows", 0) == 32)
    assert service.status()["entries"][0]["name"] == first
    assert service.current is None
    store.upsert("space", 16, [dict(row, id="tail-" + row["id"]) for row in rows[32:]], matrix[32:])
    wait_until(lambda: ready(service) and service.status()["entries"][0]["name"] != first)
    assert store.ann_status("space", 16)["indexed_rows"] == 2048
    original = AnnProcess.build
    monkeypatch.setattr(
        AnnProcess,
        "build",
        lambda *a, **k: (_ for _ in ()).throw(AnnUnavailable("ann_memory_limit")),
    )
    service.prepare()
    wait_until(lambda: service.status()["entries"][0]["state"] == "failed")
    assert "space" in service.failed
    assert service.status()["entries"][0]["error"] == "ann_memory_limit"
    monkeypatch.setattr(AnnProcess, "build", original)
    service.prepare()
    wait_until(lambda: ready(service))


def test_model_switch_cancels_only_previous_space_and_does_not_publish_stale_ready(
    archive, monkeypatch
):
    service, store, matrix, rows, semantic, _, lifecycle = archive
    store.upsert("other", 16, rows, matrix)
    entered, release, children = controlled_factory(monkeypatch, store.workspace)
    service.prepare()
    service.start()
    try:
        assert entered.wait(15)
        with lifecycle:
            service.models_changed("space")
            semantic.encoder = SimpleNamespace(space_id="other", spec=SimpleNamespace(dimension=16))
        assert not psutil.pid_exists(children[0])
        wait_until(
            lambda: (
                service.status()["entries"]
                and service.status()["entries"][0]["space_id"] == "other"
            )
        )
        assert not store.ann_status("space", 16)["ready"]
        assert not service.status()["entries"][0]["ready"]
        release.set()
        service.prepare("other")
        wait_until(lambda: ready(service))
        assert store.ann_status("other", 16)["ready"]
        assert not store.ann_status("space", 16)["ready"]
    finally:
        release.set()


def test_resource_change_restarts_manual_build_with_new_limits(archive, db, monkeypatch):
    service, store, _, _, semantic, media, lifecycle = archive
    entered, release, children = controlled_factory(monkeypatch, store.workspace)
    service.prepare()
    service.start()
    try:
        assert entered.wait(15)
        workspace = WorkspaceService(db, SimpleNamespace(lifecycle_lock=lifecycle), semantic, media)
        workspace.update_settings({"ann_build_threads": 1, "ann_memory_limit_mib": 512})
        assert not psutil.pid_exists(children[0])
        release.set()
        wait_until(lambda: ready(service))
        assert len(children) >= 2
        assert service.status()["resources"] == {"threads": 1, "memory_limit_mib": 512}
        assert not service.status()["enabled"]  # an explicit manual build remains manual
    finally:
        release.set()


def test_ann_settings_live_persistence_and_query_revision(archive, db):
    service, _, _, _, semantic, media, lifecycle = archive
    workspace = WorkspaceService(db, SimpleNamespace(lifecycle_lock=lifecycle), semantic, media)
    before = SearchCache.revision(db)
    workspace.update_settings(
        {"ann_build_threads": 3, "ann_memory_limit_mib": 512, "ann_indexing_enabled": True}
    )
    assert SearchCache.revision(db) == before  # resource/build scheduling is not query meaning
    workspace.update_settings({"ann_probes": 64})
    assert SearchCache.revision(db) != before
    assert db.settings.load(db.workspace).ann_probes == 64
    for values in (
        {"ann_probes": True},
        {"ann_partitions": 0},
        {"ann_memory_limit_mib": 1},
        {"ann_indexing_enabled": 1},
    ):
        with pytest.raises(UserError):
            workspace.update_settings(values)
    assert service.status()["resources"] == {"threads": 3, "memory_limit_mib": 512}


def test_ann_api_memory_only_status_and_session_guards(tmp_path):
    with TestClient(create_app(tmp_path / "workspace"), base_url="http://127.0.0.1") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"X-Session-Token": token}
        assert client.post("/api/ann-indexes/prepare", json={}).status_code == 403
        assert (
            client.post(
                "/api/ann-indexes/prepare", json={"space_id": "../bad"}, headers=headers
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/ann-indexes/prepare", json={"extra": True}, headers=headers
            ).status_code
            == 422
        )
        app = client.app
        held, release = threading.Event(), threading.Event()

        def lock_database():
            with app.state.importer.lifecycle_lock, app.state.db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                held.set()
                release.wait(10)

        holder = threading.Thread(target=lock_database)
        holder.start()
        try:
            assert held.wait(5)
            started = time.monotonic()
            response = client.get("/api/ann-indexes")
            assert response.status_code == 200
            assert time.monotonic() - started < 1
            assert client.post("/api/ann-indexes/cancel", headers=headers).status_code == 200
        finally:
            release.set()
            holder.join(5)


def test_update_confirmation_starts_previously_held_ann_scheduler(tmp_path, monkeypatch):
    nonce = "a" * 32
    monkeypatch.setenv("BTS_UPDATE_HEALTHCHECK", nonce)
    monkeypatch.setattr("telegram_search.updates.installer.commit_update", lambda *args: None)
    with TestClient(create_app(tmp_path / "workspace"), base_url="http://127.0.0.1") as client:
        app = client.app
        assert app.state.ann_indexes.thread is None
        wait_until(lambda: app.state.update_database_check == "ok")
        headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
        response = client.post("/api/updates/confirm", json={"nonce": nonce}, headers=headers)
        assert response.status_code == 200
        assert app.state.ann_indexes.thread is not None
        assert app.state.ann_indexes.thread.is_alive()
        assert not app.state.update_verifying


def test_shutdown_keeps_writer_ownership_until_native_stop_is_terminal(archive, monkeypatch):
    service, _, *_ = archive
    entered, released, closed = threading.Event(), threading.Event(), threading.Event()

    class UnfinishedWriter:
        def __init__(self, **kwargs):
            pass

        def build(self, **kwargs):
            entered.set()
            assert released.wait(30)
            return {"ready": False}

        def close(self):
            return released.is_set()

    monkeypatch.setattr("telegram_search.indexing.ann.AnnProcess", UnfinishedWriter)
    service.prepare()
    service.start()
    assert entered.wait(5)

    def close():
        service.close()
        closed.set()

    closer = threading.Thread(target=close)
    closer.start()
    try:
        # Reproduce the former ten-second observation timeout without allowing
        # it to authorize releasing the application's workspace writer lock.
        assert not closed.wait(10.5)
        assert service.current is not None
    finally:
        released.set()
        closer.join(5)
    assert closed.is_set() and service.current is None


@pytest.mark.parametrize("failure", ["kill", "join"])
def test_native_stop_error_keeps_handle_and_prevents_replacement(failure):
    failing = True
    closed = []

    class Child:
        alive = True

        def is_alive(self):
            return self.alive

        def kill(self):
            if failing and failure == "kill":
                raise OSError("synthetic stop failure")
            self.alive = False

        def join(self, timeout):
            if failing and failure == "join":
                raise OSError("synthetic observation failure")

        def close(self):
            closed.append("process")

    native = AnnProcess()
    child = native.process = Child()
    connection = native.connection = SimpleNamespace(close=lambda: closed.append("connection"))
    assert not native.close()
    assert native.process is child and native.connection is connection
    assert native.stopping and not closed
    with pytest.raises(AnnUnavailable, match="ann_worker_stopping"):
        native._start()
    assert native.process is child and not closed
    failing = False
    assert native.close()
    assert native.process is native.connection is None and not native.stopping
    assert closed == ["process", "connection"]


def test_scheduler_survives_stop_exception_and_retains_destructive_fence(archive, monkeypatch):
    service, store, *_ = archive
    attempted, release = threading.Event(), threading.Event()
    instances = []

    class StopFailure:
        def __init__(self, **kwargs):
            instances.append(self)

        def build(self, **kwargs):
            return {"ready": False}

        def close(self):
            attempted.set()
            if not release.is_set():
                raise OSError("synthetic stop failure")
            return True

    monkeypatch.setattr("telegram_search.indexing.ann.AnnProcess", StopFailure)
    service.prepare()
    service.start()
    try:
        assert attempted.wait(5)
        wait_until(lambda: service.status()["entries"][0]["error"] == "ann_stop_failed")
        assert service.thread.is_alive() and service.current is not None
        service.models_changed("space")
        assert service.current.invalidated and service.current.execution.cancelled.is_set()
        with pytest.raises(UserError, match="ANN останавливается"):
            store.clear([{"id": "space", "dimension": 16}], predicate="utc_day='day'")
        assert store.table("space", 16).count_rows() == 1024
        service.prepare()
        service._step()
        assert len(instances) == 1
    finally:
        release.set()
    wait_until(lambda: service.current is None)
    assert service.thread.is_alive()


def test_model_publication_remains_consistent_when_old_ann_writer_cannot_stop(
    db, importer, monkeypatch
):
    from test_media import services
    from test_model_devices import ExecutionStub

    semantic, media = services(db, importer)
    execution = ExecutionStub("cpu")

    def model(space):
        return SimpleNamespace(
            space_id=space,
            profile="clip",
            unload=lambda: None,
            check_contract=lambda: None,
            execution=execution,
            query_execution=execution,
        )

    media.clip = model("old_visual")
    with db.connect() as conn:
        conn.execute("UPDATE media_state SET visual_space_id='old_visual' WHERE id=1")
    service = AnnIndexService(db, semantic, media, importer.lifecycle_lock, start_background=False)

    def failed_kill():
        raise OSError("synthetic stop failure")

    native = AnnProcess()
    native.process = SimpleNamespace(is_alive=lambda: True, kill=failed_kill)
    job = service.current = SimpleNamespace(
        id="held",
        space="old_visual",
        process=native,
        execution=SearchExecution(),
        started=time.monotonic(),
        invalidated=False,
        cancel_state="cancelled",
    )
    monkeypatch.setattr(media, "_new_clip", lambda *args, **kwargs: model("new_visual"))
    monkeypatch.setattr("telegram_search.inference.providers.Execution", ExecutionStub)
    try:
        result = WorkspaceService(db, importer, semantic, media).change_device(
            "cpu", model="clip", reindex=True
        )
        assert result["execution"]["device"] == "cpu"
        with db.connect() as conn:
            persisted = conn.execute("SELECT visual_space_id FROM media_state").fetchone()[0]
            assert persisted == media.clip.space_id == "new_visual"
        assert service.current is job and job.invalidated and native.stopping
        assert job.execution.cancelled.is_set()
        with pytest.raises(UserError, match="ANN останавливается"):
            service.invalidate("old_visual")
    finally:
        native.process = None  # This test handle never owned an OS child.
        service.close()
        media.shutdown()
        semantic.shutdown()


def test_resource_notification_cannot_overtake_a_newer_user_cancel(archive, db):
    service, _, _, _, semantic, media, lifecycle = archive
    semantic.encoder = None
    semantic.preparation, semantic.query_cache = None, {}
    media.preparation, media.running, media.ocr_cpu_running, media.ocr = None, False, False, None
    media.wake = threading.Event()
    service.current = SimpleNamespace(
        id="held",
        space="space",
        process=SimpleNamespace(close=lambda: True),
        execution=SearchExecution(),
        started=time.monotonic(),
        invalidated=False,
    )
    workspace = WorkspaceService(db, SimpleNamespace(lifecycle_lock=lifecycle), semantic, media)
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    notify, errors = workspace._notify_ann, []

    def held_notify(values):
        if "cpu_threads" in values:
            entered.set()
            assert release.wait(10)
        notify(values)

    workspace._notify_ann = held_notify

    def resources():
        try:
            workspace.update_settings({"cpu_threads": 3})
        except Exception as exc:
            errors.append(exc)

    def cancel():
        try:
            workspace.update_settings({"ann_indexing_enabled": False})
            cancelled.set()
        except Exception as exc:
            errors.append(exc)

    saving = threading.Thread(target=resources)
    stopping = threading.Thread(target=cancel)
    saving.start()
    try:
        assert entered.wait(5)
        stopping.start()
        assert not cancelled.wait(0.2)
    finally:
        release.set()
        saving.join(5)
        if stopping.ident:
            stopping.join(5)
        service.current = None
    assert not errors and cancelled.is_set()
    assert not db.settings.ann_indexing_enabled
    assert service.suppressed and not service.forced


def _orphan_native(connection):
    from telegram_search.search.ann_process import _exit_with_parent

    _exit_with_parent()
    connection.send_bytes(b"ready")
    time.sleep(30)  # simulate native work that is not reading its command pipe


def _crashing_launcher(report):
    context = multiprocessing.get_context("spawn")
    connection, child = context.Pipe()
    process = context.Process(target=_orphan_native, args=(child,), daemon=True)
    process.start()
    child.close()
    assert connection.recv_bytes() == b"ready"
    report.send_bytes(str(process.pid).encode())
    os._exit(0)


def test_native_watchdog_exits_after_abrupt_launcher_death():
    context = multiprocessing.get_context("spawn")
    connection, report = context.Pipe()
    launcher = context.Process(target=_crashing_launcher, args=(report,))
    launcher.start()
    report.close()
    try:
        assert connection.poll(15)
        pid = int(connection.recv_bytes())
        launcher.join(5)
        assert not launcher.is_alive()

        def exited():
            try:
                return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                return True

        wait_until(exited, timeout=10)
    finally:
        if launcher.is_alive():
            launcher.kill()
            launcher.join(5)
        connection.close()
        launcher.close()
