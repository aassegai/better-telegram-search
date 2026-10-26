from dataclasses import replace

import pytest
from conftest import export, load, message
from fastapi.testclient import TestClient

from telegram_search.backend.api import create_app
from telegram_search.indexing.chats import ChatIndexing
from telegram_search.indexing.media import MediaService
from telegram_search.indexing.service import SemanticService
from telegram_search.shared.errors import UserError


def test_batch_overrides_inherit_new_defaults_and_can_be_cleared(importer, db, tmp_path):
    first = load(importer, export(tmp_path / "first", [message()]))["chat_id"]
    second = load(importer, export(tmp_path / "second", [message()], 200))["chat_id"]
    semantic = SemanticService(db, importer.lifecycle_lock, start_background=False)
    media = MediaService(db, importer.lifecycle_lock, semantic, start_background=False)
    indexing = ChatIndexing(db, semantic, media, importer.lifecycle_lock)
    try:
        before = indexing.performance(first)
        assert all(value is None for value in before["batch_overrides"].values())
        overridden = indexing.settings(
            first,
            {
                "embedding_batch": 32,
                "image_batch": 8,
                "ocr_batch_size": 2,
                "ocr_region_batch_size": 16,
            },
            include_status=False,
        )
        assert overridden["batch_overrides"] == overridden["batches"]
        db.settings = replace(db.settings, embedding_batch=16, image_batch=4, ocr_batch_size=4)
        assert indexing.performance(first)["batches"]["embedding_batch"] == 32
        assert indexing.performance(second)["batches"]["embedding_batch"] == 16
        restored = indexing.settings(
            first, {key: None for key in indexing.batch_fields}, include_status=False
        )
        assert all(value is None for value in restored["batch_overrides"].values())
        assert restored["batches"] == indexing.performance(second)["batches"]
        with db.connect() as conn:
            assert tuple(
                conn.execute(
                    "SELECT text_batch,image_batch,ocr_batch,ocr_region_batch "
                    "FROM chats WHERE id=?",
                    (first,),
                ).fetchone()
            ) == (None, None, None, None)
        for value in (0, True, "8", 1.2, -1, 129):
            with pytest.raises(UserError):
                indexing.settings(first, {"embedding_batch": value}, include_status=False)
    finally:
        media.shutdown()
        semantic.shutdown()


def test_performance_api_does_not_wait_for_model_status_and_preserves_mutation_guards(
    tmp_path, monkeypatch
):
    with TestClient(create_app(tmp_path / "workspace"), base_url="http://127.0.0.1") as client:
        chat = load(client.app.state.importer, export(tmp_path / "source", [message()]))["chat_id"]

        def unexpected(*args, **kwargs):
            pytest.fail("Performance editing must not load heavyweight model status")

        monkeypatch.setattr(client.app.state.semantic, "status", unexpected)
        monkeypatch.setattr(client.app.state.media, "status", unexpected)
        url = f"/api/chats/{chat}/performance"
        value = client.get(url).json()
        assert value["batch_overrides"]["embedding_batch"] is None
        models = client.get("/api/models")
        assert models.status_code == 200
        assert set(models.json()) == {"e5", "clip", "ocr"}
        assert client.patch(url, json={"embedding_batch": 16}).status_code == 403
        headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
        assert (
            client.patch(
                url,
                json={"embedding_batch": 16},
                headers={**headers, "Origin": "https://evil.test"},
            ).status_code
            == 403
        )
        saved = client.patch(url, json={"embedding_batch": 16}, headers=headers)
        assert saved.status_code == 200 and saved.json()["batch_overrides"]["embedding_batch"] == 16
        reset = client.patch(url, json={"embedding_batch": None}, headers=headers)
        assert (
            reset.status_code == 200 and reset.json()["batch_overrides"]["embedding_batch"] is None
        )
        assert (
            client.patch(url, json={"excluded_author_ids": []}, headers=headers).status_code == 400
        )
