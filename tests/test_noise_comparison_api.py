import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_noise_comparisons import prepare
from test_noise_dry_run import make_tokenizer

from telegram_search.backend.api import create_app
from telegram_search.indexing.noise_dry_run import NoiseDryRun, TokenContract


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "workspace"), base_url="http://127.0.0.1") as value:
        yield value


def setup(client, tmp_path, monkeypatch):
    state = client.app.state
    services = state.noise_analyses, state.noise_policies, state.noise_comparisons
    chat, parent = prepare(state.db, state.importer, tmp_path, services, monkeypatch)
    headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
    return chat, parent, headers, f"/api/chats/{chat}/noise-analyses/{parent}/dry-runs"


def test_comparison_api_validates_session_scope_limits_and_returns_durable_result(
    client, tmp_path, monkeypatch
):
    chat, parent, headers, route = setup(client, tmp_path, monkeypatch)
    assert client.get(route).json() == {"comparison": None}
    assert client.app.state.noise_comparisons.thread is None
    assert client.post(route, json={"revision": 1}).status_code == 403
    assert (
        client.post(
            route, json={"revision": 1}, headers={**headers, "Origin": "https://untrusted.test"}
        ).status_code
        == 403
    )
    assert client.get(route, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    for body in (
        {},
        {"revision": True},
        {"revision": 1, "max_messages": 0},
        {"revision": 1, "time_limit_seconds": 3601},
        {"revision": 1, "report_path": "/tmp/invalid"},
    ):
        assert client.post(route, json=body, headers=headers).status_code == 422
    assert client.post(route, json={"revision": 0}, headers=headers).status_code == 409
    response = client.post(route, json={"revision": 1}, headers=headers)
    assert response.status_code == 202
    job = response.json()["id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = client.get(f"{route}/{job}").json()
        if not status["active"]:
            break
        time.sleep(0.01)
    assert status["state"] == "ready" and status["summary"]["delta"]["removed_own_messages"] == 1
    assert client.get(route).json()["comparison"] == status
    assert (
        client.get(route.replace(f"/chats/{chat}/", "/chats/other/") + f"/{job}").status_code == 404
    )
    assert client.get(route.replace(parent, "0" * 32) + f"/{job}").status_code == 404
    assert client.get(f"{route}/invalid").status_code == 422
    assert "fence_json" not in status and "chat_id" not in status
    assert (
        client.delete(f"/api/chats/{chat}/noise-analyses/{parent}", headers=headers).status_code
        == 204
    )
    assert client.get(f"{route}/{job}").status_code == 404


def test_api_comparison_uses_actual_rust_tokenizer_without_inference(client, tmp_path, monkeypatch):
    _chat, _parent, headers, route = setup(client, tmp_path, monkeypatch)
    _, tokenizer = make_tokenizer(tmp_path)
    contract = TokenContract(
        tokenizer, "synthetic", "synthetic/test", "passage: ", 480, 3, "synthetic"
    )
    monkeypatch.setattr(
        "telegram_search.indexing.noise_dry_run.local_token_contract", lambda *a: contract
    )
    created = client.post(route, json={"revision": 1}, headers=headers).json()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = client.get(f"{route}/{created['id']}").json()
        if not status["active"]:
            break
        time.sleep(0.01)
    assert status["state"] == "ready"
    result = status["summary"]
    before = "passage: Тестовый автор: Содержательная фраза\nбял"
    after = "passage: Тестовый автор: Содержательная фраза"
    assert result["before"]["tokens"] == tokenizer.count(before)
    assert result["after"]["tokens"] == tokenizer.count(after)
    assert result["before"]["padded_token_slots"] == tokenizer.batch([before])["input_ids"].size
    assert result["delta"]["removed_own_messages"] == 1


def test_comparison_cancel_is_available_during_maintenance_and_stale_scope(
    client, tmp_path, monkeypatch
):
    chat, _parent, headers, route = setup(client, tmp_path, monkeypatch)
    entered = threading.Event()
    original = NoiseDryRun.compare

    def held(self, *args, **kwargs):
        entered.set()
        while not kwargs["cancelled"]():
            time.sleep(0.005)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(NoiseDryRun, "compare", held)
    created = client.post(route, json={"revision": 1}, headers=headers).json()
    assert entered.wait(5)
    wrong_scope = route.replace(f"/chats/{chat}/", "/chats/other/")
    assert client.post(wrong_scope, json={"revision": 1}, headers=headers).status_code == 404
    assert client.delete(wrong_scope.removesuffix("/dry-runs"), headers=headers).status_code == 404
    maintenance = client.app.state.vector_maintenance
    try:
        client.app.state.vector_maintenance = SimpleNamespace(busy=True)
        assert client.post(route, json={"revision": 1}, headers=headers).status_code == 409
        with client.app.state.db.connect() as conn:
            conn.execute("UPDATE chats SET revision=revision+1 WHERE id=?", (chat,))
        status = client.get(f"{route}/{created['id']}").json()
        assert status["stale"] and status["active"]
        assert client.post(f"{route}/{created['id']}/cancel").status_code == 403
        assert client.post(f"{route}/{created['id']}/cancel", headers=headers).status_code == 200
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = client.get(f"{route}/{created['id']}").json()
            if not status["active"]:
                break
            time.sleep(0.01)
        assert not status["active"] and status["summary"] is None
    finally:
        client.app.state.vector_maintenance = maintenance
