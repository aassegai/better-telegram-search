"""Scoped deletion must retain foreign partitions and content-addressed shared media."""

# ruff: noqa: F811  # Pytest fixture imported from the shared synthetic setup.

import io

import pytest
from conftest import export, load, message
from fastapi.testclient import TestClient
from PIL import Image

np = pytest.importorskip("numpy")
pytest.importorskip("lancedb")

from test_media import photo_export  # noqa: E402
from test_vector_maintenance import mixed, wait_finished  # noqa: E402,F401

from telegram_search.backend.api import create_app  # noqa: E402
from telegram_search.indexing.chats import ChatIndexing  # noqa: E402
from telegram_search.shared.errors import UserError  # noqa: E402


def add_text_chat(db, importer, semantic, root):
    chat = load(importer, export(root, [message(text="поезд другой диалог")], chat_id=300))[
        "chat_id"
    ]
    with db.connect() as conn:
        work = conn.execute(
            "SELECT id FROM index_work WHERE chat_id=? AND state='pending'", (chat,)
        ).fetchone()[0]
    assert semantic._worker(semantic.encoder).run(work)["state"] == "done"
    return chat


def test_text_clear_changes_only_the_selected_chat(db, importer, mixed, tmp_path):
    chat, _, semantic, media, _, maintenance = mixed
    other = add_text_chat(db, importer, semantic, tmp_path / "other")
    with db.connect() as conn:
        foreign = tuple(conn.execute("SELECT * FROM chats WHERE id=?", (other,)).fetchone())
        segment = tuple(
            conn.execute("SELECT * FROM index_segments WHERE chat_id=?", (other,)).fetchone()
        )
        messages = [tuple(row) for row in conn.execute("SELECT * FROM messages")]
        global_pause = conn.execute("SELECT paused FROM semantic_state").fetchone()[0]
    before = maintenance.inventory()["indexes"]
    assert maintenance.inventory(chat)["indexes"]["text"]["vectors"] == 1
    maintenance.start("text", chat)
    wait_finished(maintenance)
    assert maintenance.status()["chat_id"] == chat
    assert maintenance.inventory(chat)["indexes"]["text"]["vectors"] == 0
    assert maintenance.inventory(other)["indexes"]["text"]["vectors"] == 1
    after = maintenance.inventory()["indexes"]
    assert after["text"]["vectors"] == before["text"]["vectors"] - 1
    assert after["ocr"]["vectors"] == before["ocr"]["vectors"]
    with db.connect() as conn:
        assert tuple(conn.execute("SELECT * FROM chats WHERE id=?", (other,)).fetchone()) == foreign
        assert (
            tuple(conn.execute("SELECT * FROM index_segments WHERE chat_id=?", (other,)).fetchone())
            == segment
        )
        assert conn.execute("SELECT paused FROM semantic_state").fetchone()[0] == global_pause
        assert messages == [tuple(row) for row in conn.execute("SELECT * FROM messages")]
        assert conn.execute("SELECT text_paused FROM chats WHERE id=?", (chat,)).fetchone()[0] == 1
    # Resuming rebuilds only that chat; the retained foreign checkpoint stays ready.
    ChatIndexing(db, semantic, media, importer.lifecycle_lock).control(chat, "text", "resume")
    with db.connect() as conn:
        work = conn.execute(
            "SELECT id FROM index_work WHERE chat_id=? AND state='pending'", (chat,)
        ).fetchone()[0]
    assert semantic._worker(semantic.encoder).run(work)["state"] == "done"
    assert maintenance.inventory(chat)["indexes"]["text"]["vectors"] == 1


def colored_png(color):
    stream = io.BytesIO()
    Image.new("RGB", (64, 32), color).save(stream, format="PNG")
    return stream.getvalue()


@pytest.mark.parametrize("kind,column", [("images", "media_paused"), ("ocr", "ocr_dense_paused")])
def test_media_clear_retains_shared_and_foreign_vectors_and_ocr_cache(
    db, importer, mixed, tmp_path, kind, column
):
    _, chat, semantic, media, calls, maintenance = mixed
    shared_chat = load(importer, photo_export(tmp_path / "shared", chat_id=201))["chat_id"]
    blue = photo_export(tmp_path / "blue", photo_data=colored_png("blue"), chat_id=200)
    load(
        importer,
        export(blue.parent, [message(2, "синяя фотография", photo="photo.png")], chat_id=200),
    )
    foreign_chat = load(
        importer, photo_export(tmp_path / "green", photo_data=colored_png("green"), chat_id=202)
    )["chat_id"]
    while media._ocr_one():
        pass
    while media._ocr_embeddings():
        pass
    with db.connect() as conn:
        hashes = [row[0] for row in conn.execute("SELECT sha256 FROM media_blobs")]
        exclusive = conn.execute(
            "SELECT sha256 FROM media_refs WHERE chat_id=? AND message_id=2", (chat,)
        ).fetchone()[0]
    media._publish(
        "visual",
        4,
        "image",
        hashes,
        np.tile(np.array([[1, 0, 0, 0]], dtype=np.float32), (len(hashes), 1)),
    )
    with db.connect() as conn:
        retained = [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM media_embeddings WHERE sha256<>? ORDER BY id", (exclusive,)
            )
        ]
        ocr = [tuple(row) for row in conn.execute("SELECT * FROM ocr_cache ORDER BY rowid")]
        globals_before = tuple(conn.execute("SELECT * FROM media_state").fetchone())
        other_flags = [
            tuple(row)
            for row in conn.execute(
                "SELECT id,media_paused,ocr_paused,ocr_dense_paused FROM chats "
                "WHERE id<>? ORDER BY id",
                (chat,),
            )
        ]
    inventory = maintenance.inventory(chat)["indexes"][kind]
    assert inventory["vectors"] >= 2 and inventory["shared_vectors"] >= 1
    assert inventory["reclaimable_vectors"] == inventory["vectors"] - inventory["shared_vectors"]
    maintenance.start(kind, chat)
    wait_finished(maintenance)
    after = maintenance.inventory(chat)["indexes"][kind]
    assert after["vectors"] == inventory["shared_vectors"] and after["reclaimable_vectors"] == 0
    assert maintenance.inventory(shared_chat)["indexes"][kind]["vectors"] > 0
    assert maintenance.inventory(foreign_chat)["indexes"][kind]["vectors"] > 0
    with db.connect() as conn:
        assert retained == [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM media_embeddings WHERE sha256<>? ORDER BY id", (exclusive,)
            )
        ]
        assert ocr == [tuple(row) for row in conn.execute("SELECT * FROM ocr_cache ORDER BY rowid")]
        assert tuple(conn.execute("SELECT * FROM media_state").fetchone()) == globals_before
        assert other_flags == [
            tuple(row)
            for row in conn.execute(
                "SELECT id,media_paused,ocr_paused,ocr_dense_paused FROM chats "
                "WHERE id<>? ORDER BY id",
                (chat,),
            )
        ]
        assert conn.execute(f"SELECT {column} FROM chats WHERE id=?", (chat,)).fetchone()[0] == 1
    if kind == "images":
        # Late native output from a batch claimed before cleanup is fenced out.
        assert (
            media._publish(
                "visual", 4, "image", [exclusive], np.array([[1, 0, 0, 0]], dtype=np.float32)
            )
            == 0
        )
    else:
        with db.connect() as conn:
            assert not media._ocr_guard(conn, exclusive, semantic.encoder, media.ocr)
        count = len(calls)
        ChatIndexing(db, semantic, media, importer.lifecycle_lock).control(
            chat, "ocr_dense", "resume"
        )
        assert media._ocr_embeddings()
        assert len(calls) == count  # No recognition is repeated.


def test_shared_only_media_cannot_free_foreign_vectors_or_pause_other_chats(
    db, importer, mixed, tmp_path
):
    _, chat, _, media, _, maintenance = mixed
    other = load(importer, photo_export(tmp_path / "shared", chat_id=201))["chat_id"]
    inventory = maintenance.inventory(chat)["indexes"]["images"]
    assert inventory["vectors"] == inventory["shared_vectors"] == 1
    assert inventory["reclaimable_vectors"] == 0 and inventory["bytes"] == 0
    # An active foreign batch does not stall a scoped cleanup indefinitely.
    media._set_active("clip", True)
    try:
        maintenance.start("images", chat)
        wait_finished(maintenance)
    finally:
        media._set_active("clip", False)
    assert maintenance.status()["deleted_vectors"] == 0
    assert maintenance.inventory(other)["indexes"]["images"]["vectors"] == 1
    with db.connect() as conn:
        assert (
            conn.execute("SELECT media_paused FROM chats WHERE id=?", (other,)).fetchone()[0] == 0
        )


def test_scoped_failure_invalidates_only_target_and_can_be_retried(
    db, importer, mixed, tmp_path, monkeypatch
):
    chat, _, semantic, _, _, maintenance = mixed
    other = add_text_chat(db, importer, semantic, tmp_path / "other")
    store = semantic._vector_store()
    actual = store.clear_partitions
    monkeypatch.setattr(
        store, "clear_partitions", lambda *args: (_ for _ in ()).throw(OSError("PRIVATE failure"))
    )
    maintenance.start("text", chat)
    maintenance.worker.join(timeout=10)
    assert maintenance.status()["state"] == "failed"
    assert "PRIVATE" not in maintenance.status()["error"]
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT dense_generation FROM index_segments WHERE chat_id=?", (chat,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT dense_generation=target_generation FROM index_segments WHERE chat_id=?",
                (other,),
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT paused FROM semantic_state").fetchone()[0] == 0
    monkeypatch.setattr(store, "clear_partitions", actual)
    maintenance.start("text", chat)
    wait_finished(maintenance)
    assert maintenance.inventory(chat)["indexes"]["text"]["vectors"] == 0
    assert maintenance.inventory(other)["indexes"]["text"]["vectors"] == 1


def test_missing_chat_is_rejected_before_any_pause_or_invalidation(db, mixed):
    *_, maintenance = mixed
    with db.connect() as conn:
        before = [tuple(row) for row in conn.execute("SELECT * FROM chats")]
    for call in (
        lambda: maintenance.inventory("missing"),
        lambda: maintenance.start("text", "missing"),
    ):
        with pytest.raises(UserError, match="Диалог не найден"):
            call()
    assert maintenance.status()["state"] == "idle"
    with db.connect() as conn:
        assert before == [tuple(row) for row in conn.execute("SELECT * FROM chats")]


def test_scoped_http_endpoints_validate_confirmation_and_local_session(db, tmp_path):
    with TestClient(create_app(db.workspace), base_url="http://127.0.0.1") as client:
        importer = client.app.state.importer
        chat = load(importer, export(tmp_path / "one", [message()], chat_id=100))["chat_id"]
        other = load(importer, export(tmp_path / "two", [message()], chat_id=200))["chat_id"]
        base = f"/api/chats/{chat}/storage/vectors"
        path = base + "/text/clear"
        headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
        assert client.post(path, json={"confirm": True}).status_code == 403
        assert client.post(path, headers=headers, json={"confirm": False}).status_code == 400
        assert client.post(path, headers=headers, json={"confirm": 1}).status_code == 422
        assert (
            client.post(
                path, headers={**headers, "Origin": "https://evil.invalid"}, json={"confirm": True}
            ).status_code
            == 403
        )
        assert client.get(base).status_code == 200
        assert client.get("/api/chats/missing/storage/vectors").status_code == 400
        assert (
            client.post(
                "/api/chats/missing/storage/vectors/text/clear",
                headers=headers,
                json={"confirm": True},
            ).status_code
            == 400
        )
        assert client.app.state.vector_maintenance.status()["state"] == "idle"
        result = client.post(path, headers=headers, json={"confirm": True})
        assert result.status_code == 200 and result.json()["chat_id"] == chat
        wait_finished(client.app.state.vector_maintenance)
        with client.app.state.db.connect() as conn:
            assert (
                conn.execute("SELECT text_paused FROM chats WHERE id=?", (chat,)).fetchone()[0] == 1
            )
            assert (
                conn.execute("SELECT text_paused FROM chats WHERE id=?", (other,)).fetchone()[0]
                == 0
            )


def test_retry_compacts_old_versions_even_when_logical_deletion_already_committed(
    mixed, monkeypatch
):
    chat, _, semantic, _, _, maintenance = mixed
    table = semantic._vector_store().table(
        semantic.encoder.space_id, semantic.encoder.spec.dimension
    )
    original = type(table).optimize
    calls = []

    def fail_once(self, *args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise OSError("Synthetic compaction failure after deletion")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(table), "optimize", fail_once)
    maintenance.start("text", chat)
    maintenance.worker.join(timeout=10)
    assert maintenance.status()["state"] == "failed" and len(calls) == 1
    assert maintenance.inventory(chat)["indexes"]["text"]["vectors"] == 0
    maintenance.start("text", chat)
    wait_finished(maintenance)
    assert len(calls) == 2
    assert maintenance.status()["freed_bytes"] > 0


def test_clear_then_resume_fences_an_old_multibatch_ocr_attempt(db, importer, mixed, monkeypatch):
    _, chat, semantic, media, _, maintenance = mixed
    maintenance.start("ocr", chat)
    wait_finished(maintenance)
    with db.connect() as conn:
        conn.execute(
            "UPDATE ocr_cache SET text=?,text_normalized=?", ("word " * 1500, "word " * 1500)
        )
        conn.execute("UPDATE chats SET text_batch=1 WHERE id=?", (chat,))
    controls = ChatIndexing(db, semantic, media, importer.lifecycle_lock)
    controls.control(chat, "ocr_dense", "resume")
    encode = semantic.encoder.encode_text
    calls = []

    def clear_during_second_batch(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            maintenance.start("ocr", chat)
            wait_finished(maintenance)
            controls.control(chat, "ocr_dense", "resume")
        return encode(*args, **kwargs)

    monkeypatch.setattr(semantic.encoder, "encode_text", clear_during_second_batch)
    assert media._ocr_embeddings()
    assert len(calls) == 2
    with db.connect() as conn:
        assert (
            conn.execute("SELECT count(*) FROM media_embeddings WHERE kind='ocr'").fetchone()[0]
            == 0
        )
    assert not media.ocr_dense_attempts
    # A fresh attempt must rebuild every part, including the removed prefix.
    monkeypatch.setattr(semantic.encoder, "encode_text", encode)
    assert media._ocr_embeddings()
    with db.connect() as conn:
        ids = [row[0] for row in conn.execute("SELECT id FROM media_embeddings WHERE kind='ocr'")]
    assert len(ids) >= 4
    table = semantic._vector_store().table(
        semantic.encoder.space_id, semantic.encoder.spec.dimension
    )
    assert table.count_rows("id IN (" + ",".join(f"'{key}'" for key in ids) + ")") == len(ids)


def test_clearing_a_chat_does_not_cancel_a_foreign_or_shared_ocr_attempt(
    db, importer, mixed, tmp_path
):
    _, chat, _, media, _, maintenance = mixed
    load(importer, photo_export(tmp_path / "shared", chat_id=201))
    load(importer, photo_export(tmp_path / "foreign", chat_id=202, photo_data=colored_png("blue")))
    with db.connect() as conn:
        shas = [row[0] for row in conn.execute("SELECT sha256 FROM media_blobs")]
    tokens = {object(): sha for sha in shas}
    media.ocr_dense_attempts.update(tokens)
    try:
        maintenance.start("ocr", chat)
        wait_finished(maintenance)
        assert media.ocr_dense_attempts == tokens
    finally:
        media.ocr_dense_attempts.clear()
