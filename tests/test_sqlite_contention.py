"""Real SQLite writer contention with synthetic queues; no models or GPU required."""

import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from conftest import export, load, message
from fastapi.testclient import TestClient

from telegram_search.backend.api import create_app
from telegram_search.indexing.service import SemanticService
from telegram_search.storage.contention import database_busy


def wait_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        threading.Event().wait(0.01)


def locker(db):
    conn = sqlite3.connect(db.path, timeout=0.1)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE semantic_state SET error=error WHERE id=1")
    return conn


def stop(service):
    service.stop.set()
    service.wake.set()
    service.background.join(timeout=3)
    assert not service.background.is_alive()


def test_busy_cleanup_keeps_worker_alive_and_recovers_without_error_write(db, monkeypatch, caplog):
    service = SemanticService(db, threading.RLock(), start_background=False)
    service.available = True
    completed = threading.Event()

    def cleanup():
        with db.connect() as conn:
            conn.execute("UPDATE semantic_state SET error=NULL WHERE id=1")
        completed.set()

    monkeypatch.setattr(service, "cleanup", cleanup)
    writer = locker(db)
    service.start_background()
    try:
        wait_until(lambda: service.worker_status()["waiting_reason"] == "database")
        assert service.background.is_alive() and not completed.is_set()
        assert "OperationalError" not in caplog.text
        assert service.worker_status()["retry_in_seconds"] <= 2
        writer.rollback()
        assert completed.wait(3)
        wait_until(lambda: service.worker_status()["phase"] == "idle")
        assert service.worker_status()["error"] is None
        with db.connect() as conn:
            assert conn.execute("SELECT error FROM semantic_state").fetchone()[0] is None
    finally:
        writer.close()
        stop(service)


def test_a_second_failure_while_saving_an_error_cannot_kill_the_worker(db, monkeypatch, caplog):
    service = SemanticService(db, threading.RLock(), start_background=False)
    service.available = True
    first = [True]
    completed = threading.Event()

    def cleanup():
        if first[0]:
            first[0] = False
            raise ValueError("PRIVATE message /private/path")
        completed.set()

    monkeypatch.setattr(service, "cleanup", cleanup)
    writer = locker(db)
    service.start_background()
    try:
        wait_until(lambda: "could not be saved" in caplog.text)
        assert service.background.is_alive()
        assert service.worker_status()["phase"] == "error"
        assert "PRIVATE" not in caplog.text
        assert "private/path" not in str(service.worker_status())
        writer.rollback()
        assert completed.wait(3)
    finally:
        writer.close()
        stop(service)


def test_busy_work_requeues_its_existing_checkpoint_and_continues(
    db, importer, tmp_path, monkeypatch
):
    load(importer, export(tmp_path / "source", [message()]))
    service = SemanticService(db, importer.lifecycle_lock, start_background=False)
    service.available = True
    service.encoder = SimpleNamespace(unload_idle=lambda **kw: False)
    with db.connect() as conn:
        conn.execute("UPDATE semantic_state SET enabled=1")
    claimed, proceed, completed = threading.Event(), threading.Event(), threading.Event()
    attempts = []

    def run(work_id):
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM index_work WHERE id=?", (work_id,)).fetchone()
            attempts.append((row["state"], row["stage"], row["chunks_done"]))
            conn.execute(
                "UPDATE index_work SET state='running',attempts=attempts+1,"
                "stage='embedding',chunks_done=3 WHERE id=?",
                (work_id,),
            )
        if len(attempts) == 1:
            claimed.set()
            assert proceed.wait(3)
        with db.connect() as conn:
            conn.execute("UPDATE index_work SET state='done' WHERE id=?", (work_id,))
        completed.set()
        return {"state": "done"}

    monkeypatch.setattr(service, "_worker", lambda encoder: SimpleNamespace(run=run))
    service.start_background()
    assert claimed.wait(3)
    writer = locker(db)
    try:
        proceed.set()
        wait_until(lambda: service.worker_status()["waiting_reason"] == "database")
        assert service.background.is_alive() and not completed.is_set()
        writer.rollback()
        assert completed.wait(3)
        assert attempts == [("pending", "building", 0), ("pending", "embedding", 3)]
        with db.connect() as conn:
            assert conn.execute("SELECT state FROM index_work").fetchone()[0] == "done"
            assert conn.execute("SELECT paused FROM semantic_state").fetchone()[0] == 0
    finally:
        writer.close()
        stop(service)


def test_background_timeout_is_thread_local_and_restored_after_exception(db):
    def current_timeout():
        with db.connect() as conn:
            return conn.execute("PRAGMA busy_timeout").fetchone()[0]

    observed = []
    with pytest.raises(ValueError), db.connection_timeout(0.025):
        assert current_timeout() == 25
        worker = threading.Thread(target=lambda: observed.append(current_timeout()))
        worker.start()
        worker.join(timeout=2)
        raise ValueError("synthetic")
    assert observed == [30000] and current_timeout() == 30000


@pytest.mark.parametrize("newer_attempt", [False, True])
def test_explicit_dead_worker_restart_recovers_only_its_owned_attempt(
    db, importer, tmp_path, monkeypatch, newer_attempt
):
    load(importer, export(tmp_path / "source", [message()]))
    service = SemanticService(db, importer.lifecycle_lock, start_background=False)
    service.available = True
    service.encoder = SimpleNamespace(unload_idle=lambda **kw: False)
    with db.connect() as conn:
        conn.execute("UPDATE semantic_state SET enabled=1")
    calls, exits = [], []
    completed = threading.Event()

    def run(work_id):
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM index_work WHERE id=?", (work_id,)).fetchone()
            calls.append((row["state"], row["chunks_done"]))
            conn.execute(
                "UPDATE index_work SET state='running',attempts=attempts+1,"
                "stage='embedding',chunks_done=3 WHERE id=?",
                (work_id,),
            )
        if len(calls) == 1:
            raise SystemExit("synthetic thread termination")
        with db.connect() as conn:
            conn.execute("UPDATE index_work SET state='done' WHERE id=?", (work_id,))
        completed.set()
        return {"state": "done"}

    original_loop = service._loop

    def capture_exit():
        try:
            original_loop()
        except SystemExit as exc:
            exits.append(exc)

    monkeypatch.setattr(service, "_worker", lambda encoder: SimpleNamespace(run=run))
    monkeypatch.setattr(service, "_loop", capture_exit)
    service.start_background()
    service.background.join(timeout=3)
    assert exits and not service.worker_status()["alive"]
    if newer_attempt:
        with db.connect() as conn:
            conn.execute("UPDATE index_work SET attempts=attempts+1")
    service.start_background()
    try:
        if newer_attempt:
            wait_until(lambda: service.worker_status()["phase"] == "idle")
            assert len(calls) == 1
            with db.connect() as conn:
                assert conn.execute("SELECT state FROM index_work").fetchone()[0] == "running"
        else:
            assert completed.wait(3)
            assert calls == [("pending", 0), ("pending", 3)]
        assert service.worker_status()["alive"]
    finally:
        stop(service)


def test_repeated_cleanup_retries_keep_the_continuous_database_wait(monkeypatch):
    from telegram_search.indexing.worker_health import WorkerHealth

    now = [10]
    monkeypatch.setattr("telegram_search.indexing.worker_health.time.monotonic", lambda: now[0])
    health = WorkerHealth()
    health.update("waiting_database", delay=1)
    now[0] += 5
    health.update("cleanup", keep_wait=True)
    health.update("waiting_database", delay=2)
    assert health.snapshot(enabled=True, thread=None, stopped=False)["database_wait_seconds"] == 5
    health.update("idle")
    assert health.snapshot(enabled=True, thread=None, stopped=False)["waiting_reason"] is None


@pytest.mark.parametrize("kind", ["chat", "segment"])
def test_cleanup_does_not_hold_sqlite_writer_during_native_vector_io(db, monkeypatch, kind):
    service = SemanticService(db, threading.RLock(), start_background=False)
    service.available = True
    with db.connect() as conn:
        conn.execute("INSERT INTO embedding_spaces VALUES('synthetic','synthetic','{}',1,0)")
        for chat in ("first", "second"):
            if kind == "chat":
                conn.execute("INSERT INTO vector_deletions VALUES(?,0,NULL)", (chat,))
            else:
                conn.execute(
                    "INSERT INTO vector_segment_cleanup VALUES(?,'2026-01-01','synthetic',1,NULL)",
                    (chat,),
                )
    entered, release, errors = threading.Event(), threading.Event(), []

    def native(spaces, chat, *args):
        if chat == "second":
            entered.set()
            assert release.wait(3)

    monkeypatch.setattr(
        service, "_vector_store", lambda: SimpleNamespace(delete_chat=native, prune_segment=native)
    )

    def cleanup():
        try:
            service.cleanup()
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=cleanup)
    thread.start()
    try:
        assert entered.wait(2)
        writer = locker(db)
        writer.rollback()
        writer.close()
    finally:
        release.set()
        thread.join(timeout=3)
    assert not errors and not thread.is_alive()
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM vector_deletions").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM vector_segment_cleanup").fetchone()[0] == 0


def test_busy_pause_publication_does_not_lose_the_first_failed_batch(
    db, importer, tmp_path, monkeypatch
):
    load(importer, export(tmp_path / "source", [message(1), message(2, date=1750086400)]))
    service = SemanticService(db, importer.lifecycle_lock, start_background=False)
    service.available = True
    service.encoder = SimpleNamespace(space_id="synthetic", unload_idle=lambda **kw: False)
    with db.connect() as conn:
        conn.execute("INSERT INTO embedding_spaces VALUES('synthetic','synthetic','{}',1,0)")
        conn.execute("UPDATE semantic_state SET enabled=1,active_space_id='synthetic'")
    failed, proceed = threading.Event(), threading.Event()
    calls = []

    def run(work_id):
        calls.append(work_id)
        with db.connect() as conn:
            conn.execute(
                "UPDATE index_work SET state='failed',attempts=1,embedding_space_id='synthetic',"
                "error='Synthetic GPU failure' WHERE id=?",
                (work_id,),
            )
            result = dict(
                conn.execute("SELECT * FROM index_work WHERE id=?", (work_id,)).fetchone()
            )
        failed.set()
        assert proceed.wait(3)
        return result

    monkeypatch.setattr(service, "_worker", lambda encoder: SimpleNamespace(run=run))
    service.start_background()
    assert failed.wait(3)
    writer = locker(db)
    try:
        proceed.set()
        wait_until(lambda: service.worker_status()["waiting_reason"] == "database")
        writer.rollback()

        def paused():
            with db.connect() as conn:
                return conn.execute("SELECT paused FROM semantic_state").fetchone()[0]

        wait_until(paused)
        assert len(calls) == 1 and service.background.is_alive()
        with db.connect() as conn:
            assert {
                row[0]: row[1]
                for row in conn.execute("SELECT state,count(*) FROM index_work GROUP BY state")
            } == {"failed": 1, "pending": 1}
    finally:
        writer.close()
        stop(service)


@pytest.mark.parametrize(
    "code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY_SNAPSHOT]
)
def test_extended_busy_codes_are_classified_without_using_private_exception_text(code):
    error = sqlite3.OperationalError("PRIVATE")
    error.sqlite_errorcode = code
    assert database_busy(error)
    error.sqlite_errorcode = sqlite3.SQLITE_READONLY
    assert not database_busy(error)
    assert not database_busy(ValueError("database is locked"))


def test_health_endpoint_does_not_wait_for_lifecycle_or_sqlite(db):
    with TestClient(create_app(db.workspace), base_url="http://127.0.0.1") as client:
        held, release = threading.Event(), threading.Event()

        def hold_lifecycle():
            with client.app.state.importer.lifecycle_lock:
                held.set()
                assert release.wait(3)

        holder = threading.Thread(target=hold_lifecycle)
        holder.start()
        assert held.wait(2)
        writer = locker(db)
        try:
            began = time.monotonic()
            response = client.get("/api/indexing/health")
            assert response.status_code == 200 and time.monotonic() - began < 1
            assert "alive" in response.json()["semantic"]
            assert "path" not in response.text and "query" not in response.text
        finally:
            writer.close()
            release.set()
            holder.join(timeout=2)
