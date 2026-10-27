"""Doctor stays useful while search, SQLite or detailed collection is blocked."""

import threading
import time

from fastapi.testclient import TestClient

from telegram_search.backend.api import create_app
from telegram_search.config.diagnostics import DiagnosticsBusy, DiagnosticsSnapshot
from telegram_search.search.execution import SearchExecution
from telegram_search.search.jobs import SearchJob
from telegram_search.search.spec import SearchSpec


def test_slow_collection_is_single_flight_and_does_not_block_snapshots(db):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def collect(stop):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return {"version": "synthetic", "database_check": "ok"}

    service = DiagnosticsSnapshot(db, collect)
    try:
        assert service.snapshot()["diagnostic_snapshot"]["partial"]
        assert entered.wait(1)
        for _ in range(10):
            before = time.monotonic()
            assert service.snapshot()["diagnostic_snapshot"]["state"] == "collecting"
            assert time.monotonic() - before < 0.5
        assert calls == [1]
        release.set()
        service.worker.join(2)
        ready = service.snapshot()
        assert ready["database_check"] == "ok"
        assert not ready["diagnostic_snapshot"]["partial"]
        assert calls == [1]
    finally:
        release.set()
        service.close()


def test_busy_and_failed_refreshes_keep_previous_report_and_hide_exception_details(db):
    outcome = ["ready"]

    def collect(stop):
        if outcome[0] == "busy":
            raise DiagnosticsBusy()
        if outcome[0] == "failed":
            raise RuntimeError("PRIVATE path/query")
        return {"database_check": "ok", "messages": 123}

    service = DiagnosticsSnapshot(db, collect, interval=0)
    try:
        service.snapshot()
        service.worker.join(2)
        for state, expected in [("busy", "waiting"), ("failed", "failed")]:
            outcome[0] = state
            service.attempted = float("-inf")
            service.snapshot()
            service.worker.join(2)
            service.interval = 1000
            report = service.snapshot()
            assert report["messages"] == 123
            assert report["diagnostic_snapshot"]["state"] == expected
            assert "PRIVATE" not in str(report)
            service.interval = 0
    finally:
        service.close()


def test_doctor_returns_live_search_even_when_archive_sqlite_and_scheduler_are_busy(db):
    with TestClient(create_app(db.workspace), base_url="http://127.0.0.1") as client:
        jobs = client.app.state.search_jobs
        execution = SearchExecution()
        execution.begin("text_words")
        jobs.running = SearchJob(
            "synthetic-job", SearchSpec(query="PRIVATE query"), execution=execution
        )
        held, release = threading.Event(), threading.Event()
        scheduler = threading.Event()

        def hold():
            with client.app.state.importer.lifecycle_lock, db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                held.set()
                assert scheduler.wait(3)
                with jobs.lock:
                    held.set()
                    assert release.wait(3)

        thread = threading.Thread(target=hold)
        thread.start()
        assert held.wait(1)
        try:
            before = time.monotonic()
            response = client.get("/api/doctor")
            assert response.status_code == 200 and time.monotonic() - before < 1
            report = response.json()
            assert report["search"]["active"]["phase"] == "text_words"
            assert report["search"]["active"]["job_id"] == "synthetic-job"
            assert "PRIVATE" not in response.text
            client.app.state.diagnostics.worker.join(1)
            assert client.get("/api/doctor").json()["diagnostic_snapshot"]["state"] == "waiting"
            held.clear()
            scheduler.set()
            assert held.wait(1)
            before = time.monotonic()
            response = client.get("/api/doctor")
            assert response.status_code == 200 and time.monotonic() - before < 1
            assert response.json()["search"] == {"available": False, "reason": "scheduler_busy"}
        finally:
            scheduler.set()
            release.set()
            thread.join(2)
            jobs.running = None


def test_closing_doctor_interrupts_sql_in_model_status_before_integrity_scan(db, monkeypatch):
    with TestClient(create_app(db.workspace), base_url="http://127.0.0.1") as client:
        entered = threading.Event()
        connections = []
        database = client.app.state.db
        cache = client.app.state.diagnostics

        def slow_status():
            with database.connect() as conn:
                connections.append(conn)
                entered.set()
                conn.execute(
                    "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL "
                    "SELECT n+1 FROM seq WHERE n<100000000) SELECT sum(n) FROM seq"
                ).fetchone()
            return {}

        monkeypatch.setattr(client.app.state.media, "status", slow_status)
        try:
            assert client.get("/api/doctor").status_code == 200
            assert entered.wait(2)
            began = time.monotonic()
            cache.close()
            assert time.monotonic() - began < 1
            assert not cache.worker.is_alive()
        finally:
            if cache.worker and cache.worker.is_alive():
                for conn in connections:
                    conn.interrupt()
                cache.worker.join(2)


def test_integrity_scan_is_not_repeated_for_each_diagnostic_refresh(db, monkeypatch):
    from contextlib import contextmanager

    with TestClient(create_app(db.workspace), base_url="http://127.0.0.1") as client:
        database = client.app.state.db
        connect = database.connect
        scans = []

        @contextmanager
        def counted():
            with connect() as conn:
                conn.set_trace_callback(
                    lambda sql: scans.append(sql) if sql.upper() == "PRAGMA QUICK_CHECK" else None
                )
                yield conn

        monkeypatch.setattr(database, "connect", counted)
        cache = client.app.state.diagnostics
        for _ in range(2):
            cache.interval = 0
            assert client.get("/api/doctor").status_code == 200
            cache.worker.join(2)
            assert not cache.worker.is_alive()
            assert cache.state == "ready"
        assert len(scans) == 1
