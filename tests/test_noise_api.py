import time
from types import SimpleNamespace

import pytest
from conftest import export, load, message
from fastapi.testclient import TestClient

from telegram_search.backend.api import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "workspace"), base_url="http://127.0.0.1") as client:
        yield client


def test_noise_routes_require_local_session_validate_and_keep_job_scope(client, tmp_path):
    chat = load(client.app.state.importer, export(tmp_path / "a", [message(text="бял")]))["chat_id"]
    route = f"/api/chats/{chat}/noise-analyses"
    assert client.get(route).json() == {"analyses": []}
    assert client.app.state.noise_analyses.thread is None
    assert client.post(route, json={}).status_code == 403
    headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
    assert (
        client.post(
            route, json={}, headers={**headers, "Origin": "https://untrusted.test"}
        ).status_code
        == 403
    )
    assert client.get(route, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    for body in (
        {"max_forms": True},
        {"max_messages": 0},
        {"time_limit_seconds": 3601},
        {"report_path": "/tmp/noise.json"},
    ):
        assert client.post(route, json=body, headers=headers).status_code == 422
    response = client.post(route, json={}, headers=headers)
    assert response.status_code == 202
    job_id = response.json()["id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = client.get(f"{route}/{job_id}").json()
        if status["state"] not in {"queued", "running"}:
            break
        time.sleep(0.02)
    assert status["state"] == "ready" and status["summary"] is not None
    assert "excluded_authors" not in status and "fence_json" not in status
    assert client.get(f"/api/chats/other/noise-analyses/{job_id}").status_code == 404
    for suffix in (
        "forms?limit=101",
        "forms?offset=-1",
        "forms?decision=invalid",
        "families?limit=0",
    ):
        assert client.get(f"{route}/{job_id}/{suffix}").status_code == 422
    assert client.get(f"{route}/not-a-job").status_code == 422
    assert client.get(f"{route}/{job_id}/forms").json()["forms"][0]["form"] == "бял"
    assert client.delete(f"{route}/{job_id}").status_code == 403
    assert client.delete(f"{route}/{job_id}", headers=headers).status_code == 204
    assert client.get(f"{route}/{job_id}").status_code == 404


def test_cancel_remains_available_during_vector_maintenance(client, tmp_path):
    chat = load(client.app.state.importer, export(tmp_path / "a", [message()]))["chat_id"]
    route = f"/api/chats/{chat}/noise-analyses"
    headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
    # Stop the worker at a cancellable CPU checkpoint, without a DB transaction.
    service = client.app.state.noise_analyses
    import threading

    entered, release = threading.Event(), threading.Event()
    original = service._analyze

    def held(job):
        entered.set()
        assert release.wait(5)
        service._checkpoint(job)
        return original(job)

    service._analyze = held
    maintenance = client.app.state.vector_maintenance
    try:
        job_id = client.post(route, json={}, headers=headers).json()["id"]
        assert entered.wait(5)
        client.app.state.vector_maintenance = SimpleNamespace(busy=True)
        assert client.post(route, json={}, headers=headers).status_code == 409
        assert client.post(f"{route}/{job_id}/cancel", headers=headers).status_code == 200
        assert client.post(f"{route}/{job_id}/cancel").status_code == 403
    finally:
        client.app.state.vector_maintenance = maintenance
        release.set()
