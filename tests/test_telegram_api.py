import json
import os
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from telegram_fake import FakeAdapter

from telegram_search.backend.api import create_app
from telegram_search.telegram_sync.models import SourceFailure
from telegram_search.telegram_sync.settings import SyncSettings


@pytest.fixture
def api(tmp_path):
    fake = FakeAdapter()
    app = create_app(
        tmp_path / "workspace",
        tmp_path / "unbuilt",
        telegram_factory=lambda *_: fake,
        telegram_settings=SyncSettings(api_id=1, api_hash="a" * 32),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, fake


def test_auth_mutations_csrf_and_validation_never_echo_secrets(api, caplog):
    client, fake = api
    secret = "synthetic-private-2fa-password"
    path = "/api/telegram/auth/start"
    assert client.post(path, json={"phone": "+10000000000"}).status_code == 403
    headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
    for body in ({"phone": secret}, {"phone": "+10000000000", "password": secret}):
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == 422 and secret not in response.text
    fake.need_password = True
    flow = client.post(path, json={"phone": "+10000000000"}, headers=headers).json()
    code = client.post(
        f"/api/telegram/auth/{flow['flow_id']}/code", json={"code": "12345"}, headers=headers
    )
    assert code.json()["state"] == "awaiting_2fa"
    password_path = f"/api/telegram/auth/{flow['flow_id']}/password"
    response = client.post(password_path, json={"password": secret * 20}, headers=headers)
    assert response.status_code == 422 and secret not in response.text
    assert (
        client.post(password_path, json={"password": secret}, headers=headers).json()["state"]
        == "live"
    )
    public = json.dumps(
        [client.get("/api/telegram/connection").json(), client.get("/api/doctor").json()]
    )
    assert not any(
        value in public or value in caplog.text
        for value in (secret, "+10000000000", "synthetic-code-hash")
    )
    with client.app.state.db.connect() as conn:
        dump = " ".join(conn.iterdump())
    assert secret not in dump and "+10000000000" not in dump
    session = next((client.app.state.db.workspace / "private/telegram").glob("*.session"))
    if os.name != "nt":
        assert session.stat().st_mode & 0o777 == 0o600
    assert client.post("/api/telegram/disconnect", headers=headers).status_code == 200
    assert session.exists()
    assert client.post("/api/telegram/logout", headers=headers).json()[
        "server_revocation_confirmed"
    ]
    assert not session.exists() and fake.revoked


@pytest.mark.parametrize(
    "config", ["bad = [", "[telegram_sync]\napi_id = inf", "[telegram_sync]\napi_id = -inf"]
)
def test_optional_malformed_telegram_config_does_not_break_json_app(tmp_path, config):
    root = tmp_path / "workspace"
    (root / "private").mkdir(parents=True)
    (root / "private/telegram.toml").write_text(config)
    with TestClient(create_app(root, tmp_path / "unbuilt"), base_url="http://127.0.0.1") as client:
        assert client.get("/api/chats").status_code == 200
        status = client.get("/api/telegram/connection").json()
        assert not status["configured"] and "конфигурации" in status["error"]


def test_offline_logout_deletes_only_local_session(api):
    client, fake = api
    headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
    flow = client.post(
        "/api/telegram/auth/start", json={"phone": "+10000000000"}, headers=headers
    ).json()
    client.post(
        f"/api/telegram/auth/{flow['flow_id']}/code", json={"code": "12345"}, headers=headers
    )

    async def offline():
        raise SourceFailure("network")

    fake.logout = offline
    result = client.post("/api/telegram/logout", headers=headers).json()
    assert not result["server_revocation_confirmed"] and not result["connected"]
    assert not list((client.app.state.db.workspace / "private/telegram").glob("*.session"))


def test_bundled_identifiers_private_overrides_and_settings_validation(tmp_path, monkeypatch):
    bundle = tmp_path / "telegram-app.json"
    bundle.write_text(json.dumps({"api_id": 1, "api_hash": "a" * 32}))
    monkeypatch.setattr(
        "telegram_search.telegram_sync.settings.bundled_directory", lambda _: bundle
    )
    root = tmp_path / "workspace"
    root.mkdir()
    settings = SyncSettings.load(root)
    assert settings.configured and settings.api_id == 1
    (root / "private").mkdir()
    (root / "private/telegram.toml").write_text("[telegram_sync]\nmedia_quota_mib=100\n")
    assert SyncSettings.load(root).media_quota_mib == 100
    monkeypatch.setenv("BTS_TELEGRAM_API_ID", "2")
    assert SyncSettings.load(root).api_id == 2
    assert "a" * 32 not in repr(replace(settings, api_hash="a" * 32))
