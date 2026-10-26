import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import export, load, message
from fastapi.testclient import TestClient

from telegram_search.backend.api import create_app
from telegram_search.indexing.exclusions import update_authors
from telegram_search.search.cache import SearchCache
from telegram_search.search.history import SearchHistory
from telegram_search.search.jobs import SearchJobs
from telegram_search.search.spec import SearchSpec


def complete(service, spec):
    job = service.create(spec)
    service.jobs[job["job_id"]].future.result(timeout=5)
    assert service.status(job["job_id"])["state"] == "completed"
    return service.result(job["job_id"])


def empty_engine():
    return SimpleNamespace(
        search=lambda *args, **kwargs: {
            "results": [],
            "has_more": False,
            "effective_mode": "words",
            "warnings": [],
        }
    )


def test_last_ten_union_favorites_and_repeated_conditions_replace_one_entry(db):
    service = SearchJobs(db, empty_engine(), threading.RLock())
    try:
        first = complete(service, SearchSpec(query="first"))
        service.history.favorite(first["history_id"], True)
        for index in range(11):
            latest = complete(service, SearchSpec(query=f"query-{index}"))
        entries = service.history.list()["entries"]
        assert len(entries) == 11 and sum(entry["recent"] for entry in entries) == 10
        favorite = next(entry for entry in entries if entry["id"] == first["history_id"])
        assert favorite["favorite"] and not favorite["recent"]
        again = complete(service, SearchSpec(query="query-10"))
        assert again["history_id"] == latest["history_id"]
        assert again["snapshot_id"] != latest["snapshot_id"]
        assert len(service.history.list()["entries"]) == 11
        service.history.favorite(first["history_id"], False)
        assert len(service.history.list()["entries"]) == 10
    finally:
        service.close()


def test_condition_identity_preserves_phrases_and_detaches_lists(db):
    service = SearchJobs(db, empty_engine(), threading.RLock())
    try:
        first = complete(service, SearchSpec(query='  "a  b" ', forwarded_from=["b", "a"]))
        repeated = complete(service, SearchSpec(query='"a  b"', forwarded_from=["a", "b", "a"]))
        changed_phrase = complete(service, SearchSpec(query='"a b"', forwarded_from=["a", "b"]))
        assert first["history_id"] == repeated["history_id"]
        assert changed_phrase["history_id"] != first["history_id"]
        assert len(service.history.list()["entries"]) == 2
    finally:
        service.close()


def test_completion_order_survives_wall_clock_moving_backwards(db, monkeypatch):
    import telegram_search.search.history as module

    clock = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    service = SearchJobs(db, empty_engine(), threading.RLock())
    try:
        for index in range(10):
            clock[0] = 1000.0 + index
            complete(service, SearchSpec(query=f"old-{index}"))
        clock[0] = 990.0
        latest = complete(service, SearchSpec(query="latest"))
        entries = service.history.list()["entries"]
        assert len(entries) == 10 and entries[0]["id"] == latest["history_id"]
        assert all(entry["spec"]["query"] != "old-0" for entry in entries)
        repeated = complete(service, SearchSpec(query="old-1"))
        assert service.history.list()["entries"][0]["id"] == repeated["history_id"]
    finally:
        service.close()


def test_paging_uses_the_accepted_snapshot_after_a_repeat_changes_history_pointer(db):
    generation = ["OLD"]
    engine = SimpleNamespace(
        search=lambda *args, **kwargs: {
            "results": [{"message_id": index, "marker": generation[0]} for index in range(5)],
            "has_more": False,
            "effective_mode": "words",
            "warnings": [],
        }
    )
    service = SearchJobs(db, engine, threading.RLock())
    try:
        spec = SearchSpec(query="shared", page_size=2)
        old = complete(service, spec)
        generation[0] = "NEW"
        new = complete(service, spec)
        assert old["history_id"] == new["history_id"]
        restored = service.history.get(
            old["history_id"], offset=2, limit=2, snapshot_id=old["snapshot_id"]
        )
        assert restored["entry"]["snapshot_id"] == old["snapshot_id"]
        assert [hit["marker"] for hit in restored["result"]["results"]] == ["OLD", "OLD"]
        assert service.history.get(new["history_id"])["result"]["results"][0]["marker"] == "NEW"
        with pytest.raises(KeyError):
            service.history.get(old["history_id"], snapshot_id="unrelated-snapshot")
    finally:
        service.close()


def test_history_restores_the_number_of_already_displayed_results(db):
    engine = SimpleNamespace(
        search=lambda *args, **kwargs: {
            "results": [{"message_id": index} for index in range(7)],
            "has_more": False,
            "effective_mode": "words",
            "warnings": [],
        }
    )
    service = SearchJobs(db, engine, threading.RLock())
    try:
        first = complete(service, SearchSpec(query="pages", page_size=2))
        service.result(first["job_id"], offset=2, limit=2)
        restored = service.history.get(first["history_id"])
        assert len(restored["result"]["results"]) == 4
        assert restored["result"]["next_offset"] == 4 and restored["result"]["limit"] == 2
    finally:
        service.close()


def test_activated_ocr_version_is_used_before_the_next_queue_claim(db):
    from telegram_search.indexing.media import MediaService

    media = MediaService.__new__(MediaService)
    media.db = db
    media.ocr = SimpleNamespace(version="previous")
    with db.connect() as conn:
        conn.execute("UPDATE media_state SET ocr_enabled=1")
        conn.execute("INSERT INTO ocr_queue_version VALUES(1,'previous')")
    service = SearchJobs(db, empty_engine(), threading.RLock())
    try:
        first = complete(service, SearchSpec(query="old OCR"))
        media.ocr = SimpleNamespace(version="new-active-engine")
        assert service.history.get(first["history_id"])["result"] is None
        second = complete(service, SearchSpec(query="new OCR"))
        assert second["applied_models"]["ocr"]["version"] == "new-active-engine"
        with db.connect() as conn:
            assert conn.execute("SELECT version FROM ocr_queue_version").fetchone()[0] == "previous"
    finally:
        service.close()


def test_browse_history_restores_loaded_pages_and_cursor_after_restart(importer, db, tmp_path):
    load(importer, export(tmp_path / "source", [message(index) for index in range(1, 6)]))
    service = SearchJobs(db, empty_engine(), importer.lifecycle_lock)
    try:
        first = complete(service, SearchSpec(page_size=2))
        following = service.result(first["job_id"], cursor=first["browse_cursor"])
        assert [hit["message_id"] for hit in following["results"]] == [3, 2]
    finally:
        service.close()
    history = SearchHistory(db, importer.lifecycle_lock)
    history.recover()
    restored = history.get(first["history_id"])["result"]
    assert [hit["message_id"] for hit in restored["results"]] == [5, 4, 3, 2]
    assert restored["browse_cursor"] == following["browse_cursor"]
    small = history.get(first["history_id"], limit=1)["result"]
    after_small = history.get(first["history_id"], cursor=small["browse_cursor"], limit=2)["result"]
    assert [hit["message_id"] for hit in after_small["results"]] == [4, 3]
    last = history.get(first["history_id"], cursor=restored["browse_cursor"], limit=2)["result"]
    assert [hit["message_id"] for hit in last["results"]] == [1]
    restored = history.get(first["history_id"])["result"]
    assert (
        len(restored["results"]) == 5 and not restored["has_more"] and not restored["browse_cursor"]
    )


def test_browse_history_keeps_a_bounded_window_and_continuation(importer, db, tmp_path):
    load(importer, export(tmp_path / "source", [message(index) for index in range(1, 131)]))
    service = SearchJobs(db, empty_engine(), importer.lifecycle_lock)
    try:
        first = complete(service, SearchSpec(page_size=60))
        following = service.result(first["job_id"], cursor=first["browse_cursor"])
        restored = service.history.get(first["history_id"])["result"]
        assert len(restored["results"]) == 100
        assert restored["results"][0]["message_id"] == 110
        assert restored["results"][-1]["message_id"] == 11
        assert restored["browse_cursor"] == following["browse_cursor"]
        assert restored["limit"] == 60
    finally:
        service.close()


def test_shutdown_releases_a_stable_copy_while_status_expires_jobs(db, monkeypatch):
    service = SearchJobs(db, empty_engine(), threading.RLock())
    complete(service, SearchSpec(query="one"))
    complete(service, SearchSpec(query="two"))
    entered, release = threading.Event(), threading.Event()
    original = service.history.release
    held, errors = False, []

    def hold(owner):
        nonlocal held
        if threading.current_thread().name == "history-close" and not held:
            held = True
            entered.set()
            assert release.wait(5)
        original(owner)

    def close():
        try:
            service.close()
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(service.history, "release", hold)
    closer = threading.Thread(target=close, name="history-close")
    closer.start()
    try:
        assert entered.wait(2)
        service.ttl = 0
        with service.lock:
            service._expire()
        assert not service.jobs
    finally:
        release.set()
        closer.join(timeout=5)
    assert not closer.is_alive() and errors == []


def test_job_ttl_and_restart_do_not_expire_empty_or_browse_history(db):
    lock = threading.RLock()
    service = SearchJobs(db, empty_engine(), lock)
    try:
        for spec in (SearchSpec(query="zero matches"), SearchSpec()):
            complete(service, spec)
        service.ttl = 0
        with service.lock:
            service._expire()
        assert not service.jobs
    finally:
        service.close()
    restored = SearchHistory(db, lock)
    restored.recover()
    entries = restored.list()["entries"]
    assert len(entries) == 2
    for entry in entries:
        result = restored.get(entry["id"])["result"]
        assert result is not None and result["results"] == []
        assert result["completed_at"] > 0 and "snapshot_coverage" in result
        assert "applied_models" in result


@pytest.mark.parametrize("mutation", ["archive", "excluded_author", "model", "settings"])
def test_changed_data_invalidates_results_but_keeps_parameters_and_star(
    importer, db, tmp_path, mutation
):
    chat = load(importer, export(tmp_path / "source", [message(author="alice")]))["chat_id"]
    service = SearchJobs(db, empty_engine(), importer.lifecycle_lock)
    try:
        result = complete(service, SearchSpec(query="retain this query"))
        hid = result["history_id"]
        service.history.favorite(hid, True)
        if mutation == "settings":
            db.settings = replace(db.settings, lexical_weight=2.0)
        else:
            with db.connect() as conn:
                if mutation == "excluded_author":
                    update_authors(conn, chat, {"alice"})
                elif mutation == "model":
                    conn.execute("UPDATE semantic_state SET enabled=1")
                else:
                    conn.execute("UPDATE chats SET revision=revision+1")
        restored = service.history.get(hid)
        assert restored["result"] is None
        assert restored["entry"]["favorite"]
        assert restored["entry"]["spec"]["query"] == "retain this query"
        assert restored["entry"]["stale_reason"] == "revision_changed"
        assert not service.history.list()["entries"][0]["cache_available"]
        with db.connect() as conn:
            assert conn.execute("SELECT result_json FROM search_snapshots").fetchone()[0] is None
    finally:
        service.close()


def test_cancelled_and_failed_jobs_never_replace_completed_history(db):
    entered, release = threading.Event(), threading.Event()

    def search(query, *args, **kwargs):
        if query == "cancel":
            entered.set()
            assert release.wait(5)
        if query == "fail":
            raise RuntimeError("private native exception")
        return {"results": [], "has_more": False, "warnings": [], "effective_mode": "words"}

    service = SearchJobs(db, SimpleNamespace(search=search), threading.RLock())
    try:
        good = complete(service, SearchSpec(query="completed"))
        job = service.create(SearchSpec(query="cancel"))
        assert entered.wait(2)
        service.cancel(job["job_id"])
        release.set()
        service.jobs[job["job_id"]].future.result(timeout=5)
        failed = service.create(SearchSpec(query="fail"))
        service.jobs[failed["job_id"]].future.result(timeout=5)
        assert service.status(job["job_id"])["state"] == "cancelled"
        assert service.status(failed["job_id"])["state"] == "failed"
        assert [entry["id"] for entry in service.history.list()["entries"]] == [good["history_id"]]
    finally:
        release.set()
        service.close()


def test_performance_and_display_preferences_preserve_captured_snapshot(db):
    service = SearchJobs(db, empty_engine(), threading.RLock())
    try:
        result = complete(service, SearchSpec(query="stable", page_size=3, chunk_size=5))
        db.settings = replace(
            db.settings,
            cpu_threads=8,
            embedding_batch=16,
            gpu_memory_limit_mib=8192,
            search_result_limit=10,
            display_chunk_size=20,
            gpu_index_load_percent=50,
        )
        restored = service.history.get(result["history_id"])
        assert restored["result"] is not None
        assert restored["entry"]["spec"]["page_size"] == 3
        assert restored["entry"]["spec"]["chunk_size"] == 5
    finally:
        service.close()


def test_distinct_clear_actions_keep_favorites_until_explicit_removal(db):
    service = SearchJobs(db, empty_engine(), threading.RLock())
    try:
        first = complete(service, SearchSpec(query="favorite"))
        complete(service, SearchSpec(query="ordinary"))
        service.history.favorite(first["history_id"], True)
        service.history.clear("cache")
        entries = service.history.list()["entries"]
        assert len(entries) == 2 and all(not entry["cache_available"] for entry in entries)
        service.history.clear("history")
        entries = service.history.list()["entries"]
        assert len(entries) == 1 and entries[0]["favorite"] and not entries[0]["recent"]
        service.history.clear("favorites")
        assert service.history.list()["entries"] == []
        # Ephemeral job owners are released separately; no orphan survives close.
    finally:
        service.close()
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM search_snapshots").fetchone()[0] == 0


def test_http_restart_restore_and_paging_do_not_rerun_search_or_expose_stale_text(
    db, tmp_path, monkeypatch
):
    path = export(tmp_path / "source", [message(i, f"bicycle PRIVATE_{i}") for i in range(1, 8)])
    with TestClient(create_app(db.workspace), base_url="http://127.0.0.1") as client:
        load(client.app.state.importer, path)
        headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
        job = client.post(
            "/api/search-jobs",
            headers=headers,
            json={
                "query": "bicycle",
                "mode": "words",
                "modalities": ["text"],
                "page_size": 2,
            },
        ).json()
        client.app.state.search_jobs.jobs[job["job_id"]].future.result(timeout=5)
        result = client.get(f"/api/search-jobs/{job['job_id']}/result").json()
        hid = result["history_id"]
        assert len(result["results"]) == 2 and result["next_offset"] == 2
        assert (
            client.post(f"/api/search-history/{hid}/favorite", json={"favorite": True}).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/search-history/{hid}/favorite", headers=headers, json={"favorite": True}
            ).status_code
            == 204
        )
    with TestClient(create_app(db.workspace), base_url="http://127.0.0.1") as client:

        def forbidden(*args, **kwargs):
            pytest.fail("Restoring history must not run retrieval or inference")

        monkeypatch.setattr(client.app.state.search, "search", forbidden)
        listed = client.get("/api/search-history").json()["entries"]
        assert len(listed) == 1 and listed[0]["favorite"]
        assert "PRIVATE_" not in json.dumps(listed)
        restored = client.get(f"/api/search-history/{hid}?offset=2&limit=2").json()
        assert len(restored["result"]["results"]) == 2 and restored["result"]["next_offset"] == 4
        assert restored["result"]["snapshot_id"] == result["snapshot_id"]
        assert restored["entry"]["spec"]["query"] == "bicycle"
        with client.app.state.db.connect() as conn:
            conn.execute("UPDATE chats SET revision=revision+1")
        stale = client.get(f"/api/search-history/{hid}").json()
        assert stale["result"] is None and stale["entry"]["favorite"]
        assert "PRIVATE_" not in json.dumps(stale)
        headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
        assert (
            client.delete("/api/search-history/collections/cache", headers=headers).status_code
            == 204
        )
        assert client.get("/api/search-history/missing").status_code == 404
        assert client.get(f"/api/search-history/{hid}?offset=-1").status_code == 422


def test_cache_fingerprint_still_rejects_cross_workspace_results(db, tmp_path):
    from telegram_search.storage.database import Database

    other = Database(tmp_path / "other-workspace")
    other.initialize()
    # Records live in their own database even if two empty workspaces have the
    # same model/settings fingerprint. IDs never open a record in another workspace.
    assert SearchCache.revision(db) == SearchCache.revision(other)
    service = SearchJobs(db, empty_engine(), threading.RLock())
    try:
        result = complete(service, SearchSpec(query="workspace-specific"))
        other_store = SearchHistory(other, threading.RLock())
        with pytest.raises(KeyError):
            other_store.get(result["history_id"])
    finally:
        service.close()
