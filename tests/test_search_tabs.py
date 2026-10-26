import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from conftest import export, load, message
from fastapi.testclient import TestClient
from pydantic import ValidationError

from telegram_search.backend.api import create_app
from telegram_search.search.history import SearchHistory
from telegram_search.search.jobs import SearchJobs
from telegram_search.search.spec import SearchSpec
from telegram_search.search.tabs import SearchDraft, SearchTabs, TabConflict, TabPatch, TabView


@pytest.fixture
def services(db):
    engine = SimpleNamespace(
        search=lambda *args, **kwargs: {
            "results": [{"marker": i} for i in range(9)],
            "has_more": False,
            "effective_mode": "words",
            "warnings": [],
        }
    )
    jobs = SearchJobs(db, engine, threading.RLock())
    tabs = SearchTabs(db, jobs.history, jobs)
    jobs.tabs = tabs
    yield tabs, jobs
    jobs.close()


def complete(tabs, jobs, tab_id, spec=None):
    job = tabs.start(tab_id, spec or SearchSpec(query="needle", page_size=2))
    jobs.jobs[job["job_id"]].future.result(timeout=5)
    assert jobs.status(job["job_id"])["state"] == "completed"
    return tabs.result(tab_id)


def test_drafts_accept_incomplete_edits_but_jobs_require_valid_spec(services):
    tabs, jobs = services
    draft = SearchDraft(
        date_from="2026-",
        date_to="2025-02-",
        modalities=[],
        scope={"mode": "selected", "chat_ids": []},
    )
    first = tabs.create(draft)
    assert first["draft"]["scope"] == {"mode": "selected", "chat_ids": []}
    assert first["draft"]["modalities"] == []
    with pytest.raises(ValidationError):
        SearchSpec.model_validate(first["draft"])
    assert not jobs.jobs
    updated = tabs.patch(
        first["id"],
        TabPatch(revision=0, draft=SearchDraft(query="new"), view=TabView(scroll_y=1234.5)),
    )
    assert updated["revision"] == 1 and updated["view"]["scroll_y"] == 1234.5
    with pytest.raises(TabConflict):
        tabs.patch(first["id"], TabPatch(revision=0, draft=SearchDraft(query="LATE")))
    assert tabs.metadata(first["id"])["draft"]["query"] == "new"
    for bad in ({"scroll_y": float("inf")}, {"visible_count": 101}, {"scroll_y": -1}):
        with pytest.raises(ValidationError):
            TabView(**bad)


def test_tab_bound_order_active_and_close_neighbor(services):
    tabs, _ = services
    rows = [tabs.create(SearchDraft(query=str(i))) for i in range(tabs.capacity)]
    assert tabs.list()["active_tab_id"] == rows[-1]["id"]
    with pytest.raises(TabConflict):
        tabs.create()
    tabs.activate(rows[3]["id"])
    tabs.close(rows[3]["id"])
    assert tabs.list()["active_tab_id"] == rows[2]["id"]
    new = tabs.create()
    assert new["position"] > rows[-1]["position"]
    for row in tabs.list()["tabs"]:
        tabs.close(row["id"])
    assert tabs.list() == {"tabs": [], "active_tab_id": None, "capacity": 8}


def test_snapshot_survives_history_eviction_and_can_be_favorited(services, db):
    tabs, jobs = services
    tab = tabs.create()
    old = complete(tabs, jobs, tab["id"])
    sid = old["entry"]["snapshot_id"]
    for i in range(11):
        job = jobs.create(SearchSpec(query=f"other-{i}"))
        jobs.jobs[job["job_id"]].future.result(timeout=5)
    assert len(jobs.history.list()["entries"]) == 10
    assert tabs.metadata(tab["id"])["snapshot"]["id"] is None
    restored = tabs.result(tab["id"], offset=2, limit=2, snapshot_id=sid)
    assert [hit["marker"] for hit in restored["result"]["results"]] == [2, 3]
    entry = tabs.favorite(tab["id"], True)
    assert entry["favorite"] and not entry["recent"]
    assert len(jobs.history.list()["entries"]) == 11
    tabs.close(tab["id"])
    assert jobs.history.get(entry["id"])["result"] is not None
    jobs.history.favorite(entry["id"], False)
    jobs.close()
    with db.connect() as conn:
        assert conn.execute("SELECT 1 FROM search_snapshots WHERE id=?", (sid,)).fetchone() is None


def test_repeated_query_does_not_replace_another_tabs_snapshot_or_view(services):
    tabs, jobs = services
    first = tabs.create()
    old = complete(tabs, jobs, first["id"])
    tabs.patch(first["id"], TabPatch(revision=1, view=TabView(visible_count=6, scroll_y=90)))
    second = tabs.create()
    latest = complete(tabs, jobs, second["id"])
    assert latest["entry"]["id"] == old["entry"]["id"]
    assert latest["entry"]["snapshot_id"] != old["entry"]["snapshot_id"]
    assert len(tabs.result(first["id"])["result"]["results"]) == 6
    assert len(tabs.result(second["id"])["result"]["results"]) == 2
    assert tabs.metadata(first["id"])["view"]["scroll_y"] == 90
    with pytest.raises(TabConflict):
        tabs.result(first["id"], snapshot_id=latest["entry"]["snapshot_id"])


def test_browse_tabs_have_independent_bounded_windows_and_cursors(services, importer, tmp_path):
    tabs, jobs = services
    load(importer, export(tmp_path / "source", [message(i) for i in range(1, 112)]))
    first = tabs.create()
    old = complete(tabs, jobs, first["id"], SearchSpec(query="", page_size=2))
    sid = old["entry"]["snapshot_id"]
    second = tabs.create(snapshot_id=sid)
    assert second["snapshot"]["snapshot_id"] != sid
    initial = tabs.result(second["id"])
    assert initial["result"]["snapshot_id"] == second["snapshot"]["snapshot_id"]
    cursor = initial["result"]["browse_cursor"]
    page = tabs.result(second["id"], cursor=cursor, limit=100)
    tabs.patch(second["id"], TabPatch(revision=0, view=TabView(visible_count=100)))
    window = tabs.result(second["id"])["result"]
    assert len(window["results"]) == 100
    assert window["browse_cursor"] == page["result"]["browse_cursor"]
    assert [hit["message_id"] for hit in tabs.result(first["id"])["result"]["results"]] == [
        111,
        110,
    ]
    assert tabs.result(first["id"])["result"]["browse_cursor"] == old["result"]["browse_cursor"]
    # Replaying an old cursor must not rewind the durable window.
    tabs.result(second["id"], cursor=cursor, limit=100)
    assert tabs.result(second["id"])["result"] == window


def test_restart_keeps_drafts_active_tab_results_scroll_and_interrupts_pending(services, db):
    tabs, jobs = services
    first, second = tabs.create(), tabs.create()
    complete(tabs, jobs, first["id"])
    tabs.patch(
        first["id"],
        TabPatch(
            revision=1,
            draft=SearchDraft(query="UNRUN DRAFT"),
            view=TabView(visible_count=4, scroll_y=456),
        ),
    )
    tabs.activate(first["id"])
    with db.connect() as conn:
        conn.execute(
            "UPDATE search_tabs SET run_job_id='interrupted-process' WHERE id=?", (second["id"],)
        )
    jobs.close()
    history = SearchHistory(db, jobs.lifecycle_lock)
    history.recover()
    restored = SearchTabs(db, history, None)
    restored.recover()
    state = restored.list()
    assert state["active_tab_id"] == first["id"]
    assert state["tabs"][0]["draft"]["query"] == "UNRUN DRAFT"
    assert state["tabs"][0]["view"]["scroll_y"] == 456
    assert state["tabs"][1]["interrupted"] and state["tabs"][1]["job_id"] is None
    assert len(restored.result(first["id"])["result"]["results"]) == 4
    assert not restored.list()["tabs"][0]["interrupted"]


def test_revision_or_cache_clear_hides_tab_text_without_losing_intent_and_star(services, db):
    tabs, jobs = services
    tab = tabs.create(SearchDraft(query="draft"))
    complete(tabs, jobs, tab["id"])
    tabs.favorite(tab["id"], True)
    with db.connect() as conn:
        conn.execute("UPDATE search_archive_epoch SET generation=generation+1")
    restored = tabs.result(tab["id"])
    assert restored["result"] is None and restored["entry"]["favorite"]
    assert tabs.metadata(tab["id"])["draft"]["query"] == "draft"
    assert not tabs.list()["tabs"][0]["snapshot"]["cache_available"]


def test_closing_running_or_queued_tab_cancels_only_its_job_and_never_resurrects(services):
    tabs, jobs = services
    entered, release = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {"results": [], "has_more": False, "effective_mode": "words", "warnings": []}

    jobs.engine.search = blocked
    first, second, third = tabs.create(), tabs.create(), tabs.create()
    running = tabs.start(first["id"], SearchSpec(query="one"))
    assert entered.wait(5)
    queued = tabs.start(second["id"], SearchSpec(query="two"))
    remaining = tabs.start(third["id"], SearchSpec(query="three"))
    try:
        with pytest.raises(TabConflict):
            tabs.start(first["id"], SearchSpec(query="duplicate"))
        tabs.close(second["id"])
        assert jobs.status(queued["job_id"])["state"] == "cancelled"
        tabs.close(first["id"])
        assert jobs.status(running["job_id"])["state"] == "cancelling"
        assert jobs.status(remaining["job_id"])["state"] == "queued"
    finally:
        release.set()
    jobs.jobs[remaining["job_id"]].future.result(timeout=5)
    assert len(tabs.list()["tabs"]) == 1
    assert tabs.metadata(third["id"])["snapshot"] is not None
    assert [entry["spec"]["query"] for entry in jobs.history.list()["entries"]] == ["three"]


def test_stopping_queued_tab_allows_another_run_and_retains_previous_result(services):
    tabs, jobs = services
    tab = tabs.create()
    previous = complete(tabs, jobs, tab["id"])
    blocker = threading.Event()
    entered = threading.Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert blocker.wait(5)
        return {"results": [], "has_more": False, "effective_mode": "words", "warnings": []}

    jobs.engine.search = blocked
    other = jobs.create(SearchSpec(query="blocker"))
    assert entered.wait(5)
    queued = tabs.start(tab["id"], SearchSpec(query="queued"))
    try:
        jobs.cancel(queued["job_id"])
        assert tabs.metadata(tab["id"])["job_id"] is None
        assert tabs.metadata(tab["id"])["last_state"] == "cancelled"
        assert tabs.result(tab["id"])["entry"]["snapshot_id"] == previous["entry"]["snapshot_id"]
        again = tabs.start(tab["id"], SearchSpec(query="again"))
        jobs.cancel(again["job_id"])
    finally:
        blocker.set()
    jobs.jobs[other["job_id"]].future.result(timeout=5)


def test_failed_publication_rolls_back_history_tab_and_owners_together(services, db):
    tabs, jobs = services
    tab = tabs.create()
    old = complete(tabs, jobs, tab["id"])
    with db.connect() as conn:
        conn.execute(
            "CREATE TRIGGER fail_tab_bind BEFORE UPDATE OF snapshot_id ON search_tabs "
            "BEGIN SELECT RAISE(ABORT,'Synthetic bind failure'); END"
        )
    job = tabs.start(tab["id"], SearchSpec(query="new"))
    jobs.jobs[job["job_id"]].future.result(timeout=5)
    assert jobs.status(job["job_id"])["state"] == "failed"
    assert tabs.result(tab["id"])["entry"]["snapshot_id"] == old["entry"]["snapshot_id"]
    assert [entry["snapshot_id"] for entry in jobs.history.list()["entries"]] == [
        old["entry"]["snapshot_id"]
    ]
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM search_snapshots").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT count(*) FROM search_snapshot_owners WHERE owner=?",
                ("job:" + job["job_id"],),
            ).fetchone()[0]
            == 0
        )


def test_failed_durable_start_never_leaves_an_orphan_inference_job(services, db):
    tabs, jobs = services
    tab = tabs.create()
    with db.connect() as conn:
        conn.execute(
            "CREATE TRIGGER fail_start BEFORE UPDATE OF run_job_id ON search_tabs "
            "WHEN NEW.run_job_id IS NOT NULL "
            "BEGIN SELECT RAISE(ABORT,'Synthetic registration failure'); END"
        )
    jobs.engine.search = lambda *a, **kw: pytest.fail("Orphan job must not execute inference")
    with pytest.raises(sqlite3.IntegrityError):
        tabs.start(tab["id"], SearchSpec(query="orphan"))
    job = next(iter(jobs.jobs.values()))
    if not job.future.cancelled():
        job.future.result(timeout=5)
    assert job.state == "cancelled"
    assert tabs.metadata(tab["id"])["job_id"] is None
    assert jobs.history.list()["entries"] == []


@pytest.mark.parametrize("include_snapshot", [False, True])
def test_late_patch_cannot_undo_completed_publication_or_its_view_reset(services, include_snapshot):
    tabs, jobs = services
    tab = tabs.create()
    old = complete(tabs, jobs, tab["id"])
    late = TabPatch(
        revision=1,
        view=TabView(visible_count=8, scroll_y=999),
        **({"snapshot_id": old["entry"]["snapshot_id"]} if include_snapshot else {}),
    )
    new = complete(tabs, jobs, tab["id"])
    with pytest.raises(TabConflict):
        tabs.patch(tab["id"], late)
    current = tabs.metadata(tab["id"])
    assert current["snapshot"]["snapshot_id"] == new["entry"]["snapshot_id"]
    assert current["view"]["scroll_y"] == 0 and current["revision"] == 2


def test_failed_tab_cleanup_does_not_kill_worker_and_can_be_reconciled(services, db):
    tabs, jobs = services
    tab = tabs.create()
    with db.connect() as conn:
        conn.execute(
            "CREATE TRIGGER fail_cleanup BEFORE UPDATE OF run_job_id ON search_tabs "
            "WHEN NEW.run_job_id IS NULL "
            "BEGIN SELECT RAISE(ABORT,'PRIVATE cleanup failure'); END"
        )
    failed = tabs.start(tab["id"], SearchSpec(query="first"))
    jobs.jobs[failed["job_id"]].future.result(timeout=5)
    status = jobs.status(failed["job_id"])
    assert status["state"] == "failed" and "PRIVATE" not in json.dumps(status)
    assert jobs.worker.is_alive() and jobs.running is None
    # A failure specific to a tab must not strand unrelated queued work.
    other = jobs.create(SearchSpec(query="other"))
    jobs.jobs[other["job_id"]].future.result(timeout=5)
    assert jobs.status(other["job_id"])["state"] == "completed"
    with db.connect() as conn:
        conn.execute("DROP TRIGGER fail_cleanup")
    assert tabs.metadata(tab["id"])["job_id"] is None
    assert tabs.metadata(tab["id"])["last_state"] == "failed"
    assert complete(tabs, jobs, tab["id"])["entry"]["spec"]["query"] == "needle"


def test_queued_cancel_cleanup_failure_does_not_break_cancel_or_tab_close(services, db):
    tabs, jobs = services
    blocker, entered = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert blocker.wait(5)
        return {"results": [], "has_more": False, "effective_mode": "words", "warnings": []}

    jobs.engine.search = blocked
    first = jobs.create(SearchSpec(query="blocker"))
    assert entered.wait(5)
    tab = tabs.create()
    queued = tabs.start(tab["id"], SearchSpec(query="queued"))
    with db.connect() as conn:
        conn.execute(
            "CREATE TRIGGER fail_cleanup BEFORE UPDATE OF run_job_id ON search_tabs "
            "WHEN NEW.run_job_id IS NULL "
            "BEGIN SELECT RAISE(ABORT,'Synthetic cleanup failure'); END"
        )
    try:
        assert jobs.cancel(queued["job_id"])["state"] == "cancelled"
        tabs.close(tab["id"])
        assert tabs.list()["tabs"] == []
    finally:
        blocker.set()
    jobs.jobs[first["job_id"]].future.result(timeout=5)


def test_http_tabs_validation_session_guard_and_restart_without_inference(db, monkeypatch):
    with TestClient(create_app(db.workspace), base_url="http://127.0.0.1") as client:
        assert client.post("/api/search-tabs", json={}).status_code == 403
        headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
        tab = client.post(
            "/api/search-tabs",
            headers=headers,
            json={"draft": {"query": "PRIVATE", "modalities": []}},
        ).json()
        tab_id = tab["id"]
        assert (
            client.patch(
                f"/api/search-tabs/{tab_id}",
                headers=headers,
                json={"revision": 0, "draft": {"query": "another"}},
            ).status_code
            == 200
        )
        assert (
            client.patch(
                f"/api/search-tabs/{tab_id}",
                headers=headers,
                json={"revision": 0, "draft": {"query": "late"}},
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"/api/search-tabs/{tab_id}/jobs",
                headers=headers,
                json={"date_from": "2026-", "query": "bad"},
            ).status_code
            == 422
        )
        response = client.post(
            f"/api/search-tabs/{tab_id}/jobs",
            headers=headers,
            json={"query": "words", "mode": "words", "modalities": ["text"]},
        )
        assert response.status_code == 202
        client.app.state.search_jobs.jobs[response.json()["job_id"]].future.result(timeout=5)
        assert client.get(f"/api/search-tabs/{tab_id}/result").json()["result"]["results"] == []
        assert client.get(f"/api/search-tabs/{tab_id}/result?offset=-1").status_code == 422
        assert client.get(f"/api/search-tabs/{tab_id}/result?snapshot_id=wrong").status_code == 409
        assert (
            client.post(
                "/api/search-tabs", headers=headers, json={"snapshot_id": "missing"}
            ).status_code
            == 404
        )
        assert len(client.get("/api/search-tabs").json()["tabs"]) == 1
        assert (
            client.post(
                "/api/search-tabs", headers=headers, json={"draft": {"query": "x" * 4097}}
            ).status_code
            == 422
        )
    with TestClient(create_app(db.workspace), base_url="http://127.0.0.1") as client:

        def forbidden(*args, **kwargs):
            pytest.fail("Opening tabs cannot start search or inference")

        monkeypatch.setattr(client.app.state.search, "search", forbidden)
        assert client.get("/api/search-tabs").json()["active_tab_id"] == tab_id
        assert client.get(f"/api/search-tabs/{tab_id}/result").json()["result"]["results"] == []
        headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
        assert client.delete(f"/api/search-tabs/{tab_id}", headers=headers).status_code == 204
        assert client.get(f"/api/search-tabs/{tab_id}").status_code == 404
        assert client.get("/api/search-tabs").json()["tabs"] == []
        assert "PRIVATE" not in json.dumps(client.get("/api/search-tabs").json())
