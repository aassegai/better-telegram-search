import threading
from types import SimpleNamespace

import pytest
from conftest import export, load, message
from fastapi.testclient import TestClient

from telegram_search.backend.api import create_app
from telegram_search.inference.resources import CpuGate
from telegram_search.search.browse import BrowseService
from telegram_search.search.execution import SearchExecution, checkpoint
from telegram_search.search.jobs import (
    SearchJobs,
    SearchNotReady,
    SearchQueueFull,
    SearchSnapshotExpired,
)
from telegram_search.search.spec import SearchScope, SearchSpec
from telegram_search.shared.errors import UserError


def finish(service, job):
    service.jobs[job["job_id"]].future.result(timeout=5)
    return service.status(job["job_id"])


def test_browse_keyset_filters_ties_and_no_context(importer, db, tmp_path):
    chat = load(
        importer,
        export(
            tmp_path / "first",
            [
                message(1, "first", date=100, author="alice", forwarded_from="source"),
                message(2, "second", date=100, author="alice", forwarded_from="source"),
                message(3, "third", date=200, author="alice", forwarded_from="source"),
                message(4, "wrong author", date=300, author="bob", forwarded_from="source"),
                message(5, "wrong source", date=300, author="alice", forwarded_from="other"),
            ],
        ),
    )["chat_id"]
    load(importer, export(tmp_path / "second", [message(1, date=100)], 200))
    spec = SearchSpec(
        scope=SearchScope(mode="selected", chat_ids=[chat]),
        author_ids=["alice"],
        forwarded_from=["source"],
        page_size=1,
    )
    browser = BrowseService(db)
    cursor, found = None, []
    while True:
        page = browser.search(spec, cursor=cursor)
        assert all(len(hit["messages"]) == 1 for hit in page["results"])
        found.extend(hit["message_id"] for hit in page["results"])
        cursor = page["browse_cursor"]
        if not cursor:
            break
    assert found == [3, 2, 1]
    oldest = browser.search(spec.model_copy(update={"sort": "oldest"}))
    assert oldest["results"][0]["message_id"] == 1
    assert not browser.search(SearchSpec(scope=SearchScope(mode="selected")))["results"]
    # A cursor from a different scope cannot turn into an unfiltered page.
    with pytest.raises(UserError, match="устарела"):
        browser.search(SearchSpec(), cursor=oldest["browse_cursor"])
    with db.connect() as conn:
        conn.execute("UPDATE chats SET revision=revision+1 WHERE id=?", (chat,))
    with pytest.raises(UserError, match="устарела"):
        browser.search(spec, cursor=oldest["browse_cursor"])


def test_browse_and_explicit_empty_scope_do_not_call_models(importer, db, tmp_path):
    load(importer, export(tmp_path / "source", [message(1), message(2)]))
    calls = []
    engine = SimpleNamespace(search=lambda *args, **kwargs: calls.append(args))
    service = SearchJobs(db, engine, importer.lifecycle_lock)
    try:
        job = service.create(SearchSpec(page_size=1))
        assert finish(service, job)["state"] == "completed"
        page = service.result(job["job_id"])
        assert len(page["results"]) == 1 and page["browse_cursor"]
        following = service.result(job["job_id"], cursor=page["browse_cursor"])
        assert following["results"][0]["message_id"] != page["results"][0]["message_id"]
        empty = service.create(SearchSpec(query="anything", scope=SearchScope(mode="selected")))
        assert finish(service, empty)["state"] == "completed"
        assert service.result(empty["job_id"])["results"] == []
        assert calls == []
    finally:
        service.close()


def test_native_call_cancellation_queue_and_no_late_publication(db):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def search(query, *args, **kwargs):
        calls.append(query)
        if query == "first":
            entered.set()
            assert release.wait(5)
        return {"results": [], "warnings": [], "has_more": False}

    service = SearchJobs(db, SimpleNamespace(search=search), threading.RLock(), max_pending=2)
    try:
        first = service.create(SearchSpec(query="first"))
        assert entered.wait(5)
        second = service.create(SearchSpec(query="second"))
        assert second["state"] == "queued" and second["queue_position"] == 1
        with pytest.raises(SearchQueueFull):
            service.create(SearchSpec(query="overflow"))
        assert service.cancel(second["job_id"])["state"] == "cancelled"
        assert service.cancel(first["job_id"])["state"] == "cancelling"
        with pytest.raises(SearchNotReady):
            service.result(first["job_id"])
        release.set()
        assert finish(service, first)["state"] == "cancelled"
        third = service.create(SearchSpec(query="fresh"))
        assert finish(service, third)["state"] == "completed"
        assert calls == ["first", "fresh"]
        assert service.jobs[first["job_id"]].result is None
    finally:
        release.set()
        service.close()


def test_cancel_interrupts_real_sqlite_computation_and_releases_reader(db):
    entered = threading.Event()

    def search(*args, **kwargs):
        with db.connect() as conn:
            entered.set()
            conn.execute(
                "WITH RECURSIVE numbers(n) AS (VALUES(0) UNION ALL "
                "SELECT n+1 FROM numbers WHERE n<100000000) SELECT sum(n) FROM numbers"
            ).fetchone()
        pytest.fail("The long SQL query should have been interrupted")

    service = SearchJobs(db, SimpleNamespace(search=search), threading.RLock())
    try:
        job = service.create(SearchSpec(query="cancel sql"))
        assert entered.wait(5)
        service.cancel(job["job_id"])
        assert finish(service, job)["state"] == "cancelled"
        with db.connect() as conn:
            assert conn.execute("SELECT 42").fetchone()[0] == 42
    finally:
        service.close()


def test_cancel_after_publication_is_terminal_even_before_worker_cleanup(db, monkeypatch):
    finishing, release = threading.Event(), threading.Event()
    original = SearchExecution.finish

    def delayed_finish(execution):
        finishing.set()
        assert release.wait(5)
        original(execution)

    monkeypatch.setattr(SearchExecution, "finish", delayed_finish)
    service = SearchJobs(
        db, SimpleNamespace(search=lambda *a, **k: {"results": []}), threading.RLock()
    )
    try:
        job = service.create(SearchSpec(query="completed"))
        assert finishing.wait(5)
        assert service.cancel(job["job_id"])["state"] == "completed"
        release.set()
        assert finish(service, job)["state"] == "completed"
        assert service.result(job["job_id"])["results"] == []
    finally:
        release.set()
        service.close()


def test_repeated_queued_cancellation_removes_physical_queue_items(db):
    entered, release = threading.Event(), threading.Event()

    def search(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {"results": []}

    service = SearchJobs(db, SimpleNamespace(search=search), threading.RLock(), max_pending=2)
    try:
        first = service.create(SearchSpec(query="native"))
        assert entered.wait(5)
        for index in range(100):
            pending = service.create(SearchSpec(query=f"cancel queued {index}"))
            assert service.cancel(pending["job_id"])["state"] == "cancelled"
            assert not service.queue
            assert len(service.jobs) <= 34
        release.set()
        assert finish(service, first)["state"] == "completed"
    finally:
        release.set()
        service.close()


def test_cancel_while_waiting_for_compute_does_not_leak_gate(db):
    entered, search_started = threading.Event(), threading.Event()
    gate = CpuGate()

    def search(*args, **kwargs):
        search_started.set()
        with gate.slot(interactive=True):
            checkpoint()
            entered.set()
        return {"results": []}

    service = SearchJobs(db, SimpleNamespace(search=search), threading.RLock())
    try:
        with gate.slot():
            job = service.create(SearchSpec(query="waiting"))
            assert search_started.wait(5)
            service.cancel(job["job_id"])
            assert finish(service, job)["state"] == "cancelled"
            assert not entered.is_set()
            assert gate.interactive_waiters == 0 and not gate.waiters
        with gate.slot():
            assert gate.depth == 1
    finally:
        service.close()


def test_completed_pages_are_bounded_and_revalidated(db):
    calls = []

    def search(*args, **kwargs):
        calls.append(1)
        return {"results": [{"message_id": i} for i in range(100)], "has_more": False}

    service = SearchJobs(db, SimpleNamespace(search=search), threading.RLock(), max_results=1)
    try:
        first = service.create(SearchSpec(query="first", page_size=5))
        assert finish(service, first)["state"] == "completed"
        page = service.result(first["job_id"], offset=5)
        assert [hit["message_id"] for hit in page["results"]] == list(range(5, 10))
        assert calls == [1]
        second = service.create(SearchSpec(query="second"))
        assert finish(service, second)["state"] == "completed"
        with pytest.raises(SearchSnapshotExpired):
            service.result(first["job_id"])
        with db.connect() as conn:
            conn.execute("UPDATE search_archive_epoch SET generation=generation+1")
        with pytest.raises(SearchSnapshotExpired):
            service.result(second["job_id"])
    finally:
        service.close()


def test_job_mutations_share_session_origin_host_guards(tmp_path):
    with TestClient(create_app(tmp_path / "workspace"), base_url="http://127.0.0.1") as client:
        assert client.post("/api/search-jobs", json={}).status_code == 403
        token = client.get("/api/session").json()["token"]
        headers = {"X-Session-Token": token}
        assert (
            client.post(
                "/api/search-jobs",
                json={},
                headers={
                    **headers,
                    "Origin": "https://untrusted.test",
                },
            ).status_code
            == 403
        )
        response = client.post("/api/search-jobs", json={}, headers=headers)
        assert response.status_code == 202
        job = response.json()
        client.app.state.search_jobs.jobs[job["job_id"]].future.result(timeout=5)
        assert client.get(f"/api/search-jobs/{job['job_id']}").json()["state"] == "completed"
        assert client.get(f"/api/search-jobs/{job['job_id']}/result").json()["results"] == []
        assert client.post(f"/api/search-jobs/{job['job_id']}/cancel").status_code == 403
        assert (
            client.post(
                "/api/search-jobs",
                json={
                    "scope": {
                        "mode": "all",
                        "chat_ids": ["unrelated"],
                    }
                },
                headers=headers,
            ).status_code
            == 422
        )
        assert client.get("/api/search-jobs/missing").status_code == 404


def test_stop_stays_available_during_maintenance_but_keeps_session_guard(tmp_path):
    with TestClient(create_app(tmp_path / "workspace"), base_url="http://127.0.0.1") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"X-Session-Token": token}
        job = client.post("/api/search-jobs", json={}, headers=headers).json()
        service = client.app.state.search_jobs
        service.jobs[job["job_id"]].future.result(timeout=5)
        original = client.app.state.vector_maintenance
        client.app.state.vector_maintenance = SimpleNamespace(busy=True)
        try:
            url = f"/api/search-jobs/{job['job_id']}/cancel"
            assert client.post(url).status_code == 403
            assert (
                client.post(url, headers={**headers, "Origin": "https://evil.test"}).status_code
                == 403
            )
            assert client.post(url, headers=headers).status_code == 200
            assert client.post("/api/search-jobs", json={}, headers=headers).status_code == 409
        finally:
            client.app.state.vector_maintenance = original
