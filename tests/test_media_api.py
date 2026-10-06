from conftest import export, load, message
from fastapi.testclient import TestClient

from telegram_search.backend.api import create_app


def test_media_and_workspace_mutations_require_local_session(db, importer, tmp_path):
    job = load(importer, export(tmp_path / "source", [message(text="заказ 12345")]))
    # Release the exclusive application lease before opening the actual API lifespan.
    importer.shutdown()
    with TestClient(create_app(db.workspace), base_url="http://127.0.0.1") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"x-session-token": token}
        source = client.get("/api/sources").json()[0]
        for path, method, body in (
            ("/api/settings", "PATCH", {"cpu_threads": 2}),
            (f"/api/sources/{source['id']}/check", "POST", None),
            ("/api/media-index/pause", "POST", None),
            ("/api/storage/compact", "POST", None),
        ):
            assert client.request(method, path, json=body).status_code == 403
        assert (
            client.patch("/api/settings", json={"cpu_threads": True}, headers=headers).status_code
            == 400
        )
        assert (
            client.patch("/api/settings", json={"cpu_threads": 2}, headers=headers).status_code
            == 200
        )
        assert client.get("/api/settings").json()["cpu_threads"] == 2
        assert client.post(f"/api/sources/{source['id']}/check", headers=headers).status_code == 200
        assert client.post("/api/media-index/pause", headers=headers).json()["paused"] == 1
        assert client.get(f"/api/chats/{job['chat_id']}/deletion-estimate").json()["messages"] == 1
        result = client.get("/api/search", params={"q": "заказ", "tab": "all"}).json()
        assert result["effective_mode"] == "mixed" and len(result["results"]) == 1
        assert client.post("/api/storage/compact", headers=headers).status_code == 200
        assert (
            client.post(
                "/api/storage/compact", headers={**headers, "origin": "https://evil.invalid"}
            ).status_code
            == 403
        )
