from types import SimpleNamespace

import pytest
from conftest import export, load, message
from fastapi.testclient import TestClient
from PIL import Image

from telegram_search.backend.api import create_app
from telegram_search.security.paths import safe_media_path
from telegram_search.shared.text import normalize_text


@pytest.fixture
def client(tmp_path):
    with TestClient(
        create_app(tmp_path / "workspace", tmp_path / "unbuilt"), base_url="http://127.0.0.1"
    ) as client:
        yield client


def test_mutations_require_token_host_and_origin(client, tmp_path):
    body = {"json_path": str(export(tmp_path / "a", [message()]))}
    assert client.post("/api/imports", json=body).status_code == 403
    token = client.get("/api/session").json()["token"]
    headers = {"X-Session-Token": token, "Origin": "https://untrusted.test"}
    assert client.post("/api/imports", json=body, headers=headers).status_code == 403
    assert client.get("/api/session", headers={"Host": "untrusted.test"}).status_code == 400
    assert client.get("/api/session", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert (
        client.post("/api/imports", json=body, headers={"X-Session-Token": token}).status_code
        == 202
    )


def test_api_import_search_media_delete_and_shared_blob(client, tmp_path):
    root = tmp_path / "a"
    root.mkdir()
    photo = root / "photo.png"
    Image.new("RGB", (10, 10), "red").save(photo)
    importer = client.app.state.importer
    first = load(importer, export(root, [message(text="Ищем велосипед", photo="photo.png")]))
    second = load(importer, export(root, [message(photo="photo.png")], 200))
    hits = client.get("/api/search", params={"q": "велосипед"}).json()["results"]
    assert hits[0]["message_id"] == 1
    mid = hits[0]["messages"][0]["media"][0]["id"]
    assert client.get(f"/api/media/{mid}").content == photo.read_bytes()
    assert client.get("/api/media/99999").status_code == 404
    headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
    assert client.delete(f"/api/chats/{first['chat_id']}", headers=headers).status_code == 200
    assert not client.get("/api/search", params={"q": "велосипед"}).json()["results"]
    assert photo.exists()
    with client.app.state.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_blobs").fetchone()[0] == 1
    assert client.delete(f"/api/chats/{second['chat_id']}", headers=headers).status_code == 200
    with client.app.state.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_blobs").fetchone()[0] == 0


def test_media_traversal_and_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"private")
    for path in ("../outside.png", "/etc/passwd", "C:\\private.png", "a/../../outside.png"):
        with pytest.raises(ValueError):
            safe_media_path(root, path)
    link = root / "link.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises(ValueError):
        safe_media_path(root, "link.png")


def test_missing_frontend_and_invalid_filters(client):
    assert client.get("/").status_code == 503
    assert client.get("/api/search", params={"q": "a", "date_from": "invalid"}).status_code == 400
    assert client.get("/api/search", params={"q": "a", "limit": 101}).status_code == 422
    result = client.get("/api/doctor").json()
    assert result["device"] == "cpu" and result["models_loaded"] == 0
    assert result["database_check"] == "ok"


def test_saved_search_options_and_request_overrides(client, tmp_path):
    from telegram_search.config.settings import Settings

    load(
        client.app.state.importer,
        export(tmp_path / "a", [message(i, "термин", date=i) for i in range(1, 21)]),
    )
    body = {"search_result_limit": 2, "display_chunk_size": 1}
    assert client.patch("/api/settings", json=body).status_code == 403
    headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
    assert client.patch("/api/settings", json=body, headers=headers).status_code == 200
    assert Settings.load(client.app.state.db.workspace).search_result_limit == 2
    for tab in ("text", "all"):
        result = client.get("/api/search", params={"q": "термин", "tab": tab}).json()
        assert (result["limit"], result["chunk_size"]) == (2, 1)
        assert len(result["results"]) == 2 and result["has_more"]
        assert all(len(hit["messages"]) == 1 for hit in result["results"])
    result = client.get("/api/search", params={"q": "термин", "limit": 3, "chunk_size": 4}).json()
    assert (result["limit"], result["chunk_size"]) == (3, 4)
    assert len(result["results"]) == 3 and result["has_more"]
    assert all(len(hit["messages"]) == 4 for hit in result["results"])
    for value in (0, 101, 1.5, True):
        assert (
            client.patch(
                "/api/settings", json={"display_chunk_size": value}, headers=headers
            ).status_code
            == 400
        )
        assert (
            client.patch(
                "/api/settings", json={"search_result_limit": value}, headers=headers
            ).status_code
            == 400
        )
    assert client.get("/api/search", params={"q": "термин", "chunk_size": 101}).status_code == 422
    assert client.get("/api/settings").json()["display_chunk_size"] == 1


def test_multimodal_api_merges_evidence_and_preserves_filters(client, tmp_path, monkeypatch):
    root = tmp_path / "synthetic"
    root.mkdir()
    Image.new("RGB", (10, 10), "red").save(root / "photo.png")
    job = load(
        client.app.state.importer,
        export(root, [message(1, "заказ"), message(2, "заказ", photo="photo.png", author="user2")]),
    )
    media = client.app.state.media
    media.control("pause")
    with client.app.state.db.connect() as conn:
        sha = conn.execute("SELECT sha256 FROM media_refs").fetchone()[0]
        conn.execute(
            "INSERT INTO ocr_cache(sha256,version,state,text,text_normalized,confidence) "
            "VALUES(?,?,'ready',?,?,91)",
            (sha, "synthetic-ocr", "заказ 12345", normalize_text("заказ 12345")),
        )
        conn.execute(
            "INSERT INTO media_embeddings(id,sha256,space_id,kind) VALUES(?,?,?,'image')",
            ("synthetic-image", sha, "synthetic-clip"),
        )
    media.ocr = SimpleNamespace(version="synthetic-ocr")
    media.clip = SimpleNamespace(
        execution=SimpleNamespace(
            info=lambda: {"device": "cpu", "provider": "CPUExecutionProvider"}
        ),
        space_id="synthetic-clip",
        encode_text=lambda _: [[0.0] * 512],
        unload=lambda: None,
    )
    monkeypatch.setattr(client.app.state.search.media, "_dense", lambda *args: ["synthetic-image"])
    for selected in (["text", "ocr"], ["images", "ocr"], ["text", "images", "ocr"]):
        params = [("q", "заказ"), ("chunk_size", 1), *[("modality", kind) for kind in selected]]
        response = client.get("/api/search", params=params)
        assert response.status_code == 200
        result = response.json()
        assert result["modalities"] == selected and result["effective_mode"] == "mixed"
        photo_hit = next(hit for hit in result["results"] if hit["message_id"] == 2)
        expected = {"text": "words", "images": "image", "ocr": "ocr_words"}
        assert set(photo_hit["matched_by"]) == {expected[kind] for kind in selected}
        assert photo_hit["ocr_text"] == "заказ 12345" and photo_hit["media_id"] is not None
        assert len(photo_hit["messages"]) == 1
        assert len({(hit["chat_id"], hit["message_id"]) for hit in result["results"]}) == len(
            result["results"]
        )
        filtered = client.get("/api/search", params=[*params, ("author_id", "user2")]).json()
        assert [(hit["chat_id"], hit["message_id"]) for hit in filtered["results"]] == [
            (job["chat_id"], 2)
        ]
        for key, value in (
            ("author_id", "absent"),
            ("chat_id", "absent"),
            ("date_from", "2030-01-01"),
            ("date_to", "2020-01-01"),
            ("content_type", "service"),
        ):
            assert not client.get("/api/search", params=[*params, (key, value)]).json()["results"]
    exact = client.get(
        "/api/search",
        params=[
            ("q", "заказ 12345"),
            ("exact", "true"),
            ("modality", "images"),
            ("modality", "ocr"),
        ],
    ).json()
    assert len(exact["results"]) == 1 and exact["results"][0]["matched_by"] == ["ocr_words"]
    assert any("Точная фраза" in warning for warning in exact["warnings"])


@pytest.mark.parametrize("selection", [[""], ["all"], ["text", "gpu"], ["ocr"] * 4])
def test_api_rejects_invalid_modalities(client, selection):
    response = client.get(
        "/api/search", params=[("q", "проверка"), *[("modality", kind) for kind in selection]]
    )
    assert response.status_code == 422


def test_image_mime_uses_bytes_and_blocks_changed_file(client, tmp_path):
    root = tmp_path / "images"
    root.mkdir()
    photo = root / "photo.html"
    Image.new("RGB", (10, 10), "blue").save(photo, format="PNG")
    photo.write_bytes(photo.read_bytes() + b'<script>alert("synthetic")</script>')
    first = load(client.app.state.importer, export(root, [message(photo="photo.html")]))
    hit = client.get(f"/api/chats/{first['chat_id']}/context/1").json()["messages"][0]
    mid = hit["media"][0]["id"]
    response = client.get(f"/api/media/{mid}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    photo.write_text('<script>alert("synthetic")</script>')
    assert client.get(f"/api/media/{mid}").status_code == 404


def test_extended_context_preserves_filter_markers(client, tmp_path):
    first = load(
        client.app.state.importer,
        export(
            tmp_path / "a",
            [
                message(1, author="alice"),
                message(2, author="bob"),
            ],
        ),
    )
    response = client.get(f"/api/chats/{first['chat_id']}/context/1", params={"author_id": "alice"})
    assert [item["matches_filters"] for item in response.json()["messages"]] == [True, False]


def test_preview_and_conflict_contracts_require_token(client, tmp_path):
    importer = client.app.state.importer
    first = load(importer, export(tmp_path / "a", [message(text="текущая", reply_to_message_id=7)]))
    body = {
        "json_path": str(export(tmp_path / "b", [message(text="новая", reply_to_message_id=8)]))
    }
    assert client.post("/api/import-previews", json=body).status_code == 403
    headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
    response = client.post("/api/import-previews", json=body, headers=headers)
    assert response.status_code == 202
    pid = response.json()["id"]
    importer.futures[pid].result(timeout=5)
    assert len(client.get("/api/import-previews").json()) == 1
    assert client.post(f"/api/import-previews/{pid}/apply").status_code == 403
    job = client.post(f"/api/import-previews/{pid}/apply", headers=headers).json()
    importer.futures[job["id"]].result(timeout=5)
    item = client.get(f"/api/imports/{job['id']}/conflicts").json()["results"][0]
    assert item["current_metadata"]["reply_to_message_id"] == 7
    assert item["incoming"]["metadata"]["reply_to_message_id"] == 8
    endpoint = f"/api/imports/{job['id']}/conflicts/1"
    choice = {"choice": "keep_current", "expected_version": item["current_version"]}
    assert client.post(endpoint, json=choice).status_code == 403
    assert client.post(endpoint, json=choice, headers=headers).status_code == 200
    assert client.post(endpoint, json=choice, headers=headers).status_code == 400
    # Deleting the chat removes staged payloads and all segment work.
    client.delete(f"/api/chats/{first['chat_id']}", headers=headers)
    with client.app.state.db.connect() as conn:
        for table in ("import_previews", "preview_entries", "index_work", "index_segments"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
