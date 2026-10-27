import json
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_custom_adapters import package

from telegram_search.backend.api import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "workspace"), base_url="http://127.0.0.1") as client:
        yield client


def headers(client):
    return {"X-Session-Token": client.get("/api/session").json()["token"]}


def wait(client, job, target=None):
    deadline = time.monotonic() + 15
    while True:
        status = client.get(f"/api/adapters/jobs/{job['id']}").json()
        if status["state"] in {"inspected", "passed", "failed", "cancelled"}:
            return status
        if target and (status.get("progress") or {}).get("phase") == target:
            return status
        assert time.monotonic() < deadline, status
        time.sleep(0.01)


def test_inspect_validate_register_and_catalog_do_not_activate_a_model(client, tmp_path):
    path, _ = package(tmp_path)
    body = {"action": "inspect", "role": "text_embedding", "manifest": str(path)}
    assert client.post("/api/adapters/jobs", json=body).status_code == 403
    token = headers(client)
    job = client.post("/api/adapters/jobs", json=body, headers=token)
    assert job.status_code == 202
    inspected = wait(client, job.json())
    assert inspected["state"] == "inspected"
    assert inspected["report"]["code_executed"] is False
    checked = client.post(
        "/api/adapters/jobs",
        headers=token,
        json={
            **body,
            "action": "validate",
            "python": sys.executable,
            "expected_identity": inspected["report"]["identity"],
        },
    )
    assert checked.status_code == 202
    completed = wait(client, checked.json())
    assert completed["state"] == "passed", completed
    catalog = client.get("/api/adapters").json()
    assert len(catalog["models"]) == 1
    assert catalog["models"][0]["activation_supported"] is True
    assert client.get("/api/semantic").json()["profile"] is None
    assert str(path) not in json.dumps(catalog)
    assert sys.executable not in json.dumps(catalog)


def test_changed_manifest_or_wrong_role_fails_without_loading_adapter(client, tmp_path):
    path, data = package(tmp_path)
    body = {"manifest": str(path), "role": "text_embedding", "action": "inspect"}
    token = headers(client)
    inspected = wait(client, client.post("/api/adapters/jobs", headers=token, json=body).json())
    data["revision"] = "changed"
    path.write_text(json.dumps(data))
    job = client.post(
        "/api/adapters/jobs",
        headers=token,
        json={
            **body,
            "action": "validate",
            "python": sys.executable,
            "expected_identity": inspected["report"]["identity"],
        },
    )
    assert wait(client, job.json())["state"] == "failed"
    assert not client.get("/api/adapters").json()["models"]
    wrong = client.post("/api/adapters/jobs", headers=token, json={**body, "role": "asr"})
    assert wait(client, wrong.json())["state"] == "failed"


def test_adapter_cancel_bypasses_maintenance_but_keeps_local_session_guard(client, monkeypatch):
    reached = []
    monkeypatch.setattr(
        client.app.state.adapter_jobs,
        "cancel",
        lambda identity: reached.append(identity) or {"state": "cancelled"},
    )
    original = client.app.state.vector_maintenance
    client.app.state.vector_maintenance = SimpleNamespace(busy=True)
    try:
        url = "/api/adapters/jobs/" + "a" * 32 + "/cancel"
        assert client.post(url).status_code == 403
        assert not reached
        assert client.post(url, headers=headers(client)).status_code == 200
        assert reached == ["a" * 32]
        assert (
            client.post(
                url, headers={**headers(client), "Origin": "https://untrusted.example"}
            ).status_code
            == 403
        )
    finally:
        client.app.state.vector_maintenance = original


def test_validation_waits_for_shared_cpu_and_can_cancel_before_resources_free(client, tmp_path):
    from telegram_search.inference.providers import CPU
    from telegram_search.inference.resources import compute_gate

    path, _ = package(tmp_path)
    token = headers(client)
    body = {"manifest": str(path), "role": "text_embedding", "action": "inspect"}
    inspected = wait(client, client.post("/api/adapters/jobs", headers=token, json=body).json())
    with compute_gate(SimpleNamespace(provider=CPU, device="cpu")).slot():
        started = client.post(
            "/api/adapters/jobs",
            headers=token,
            json={
                **body,
                "action": "validate",
                "python": sys.executable,
                "expected_identity": inspected["report"]["identity"],
            },
        ).json()
        status = wait(client, started, target="waiting_resources")
        assert status["state"] == "running"
        assert (
            client.post(f"/api/adapters/jobs/{started['id']}/cancel", headers=token).status_code
            == 200
        )
        assert wait(client, started)["state"] == "cancelled"
    assert not client.get("/api/adapters").json()["models"]
