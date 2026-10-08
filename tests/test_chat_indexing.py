import json
import time
from types import SimpleNamespace

import pytest
from conftest import export, load, message
from test_semantic import TestEncoder, setup_index

from telegram_search.indexing.chats import ChatIndexing
from telegram_search.indexing.estimates import record_rate
from telegram_search.indexing.media import MediaService
from telegram_search.indexing.worker import SegmentWorker
from telegram_search.shared.errors import UserError


def controls(db, importer, service):
    media = MediaService(db, importer.lifecycle_lock, service, start_background=False)
    return ChatIndexing(db, service, media, importer.lifecycle_lock), media


def test_resume_one_chat_preserves_legacy_pause_and_other_checkpoint(db, importer, tmp_path):
    chat, service, worker, work = setup_index(db, importer, tmp_path)
    second = load(importer, export(tmp_path / "second", [message()], chat_id=200))["chat_id"]
    indexing, media = controls(db, importer, service)
    worker.encoder.on_encode = lambda texts: service.control("pause")
    partial = worker.run(work)
    assert partial["stage"] == "embedding" and partial["chunks_done"] == 0
    indexing.control(second, "text", "resume")
    assert indexing.status(chat)["semantic"]["paused"]
    assert not indexing.status(second)["semantic"]["paused"]
    assert worker.run(work)["state"] == "paused"
    indexing.control(chat, "text", "resume")
    worker.encoder.on_encode = None
    assert worker.run(work)["state"] == "done"
    assert not indexing.status(second)["semantic"]["paused"]
    media.control("pause")
    indexing.control(chat, "media", "resume")
    assert indexing.status(second)["media"]["paused"]


def test_text_batch_changes_next_batch_and_oom_keeps_checkpoint(db, importer, tmp_path):
    chat, service, worker, work = setup_index(
        db, importer, tmp_path, ["поезд " * 180 for _ in range(160)]
    )
    indexing, _ = controls(db, importer, service)
    indexing.settings(chat, {"embedding_batch": 64})

    def encode(texts):
        if len(texts) > 16:
            raise MemoryError("synthetic GPU out of memory")
        if worker.encoder.calls.count(16) == 1:
            indexing.settings(chat, {"embedding_batch": 8})

    worker.encoder.on_encode = encode
    done = worker.run(work)
    assert done["state"] == "done"
    assert worker.encoder.calls[:4] == [64, 32, 16, 8]
    assert worker.vectors.table("synthetic", 4).count_rows() == done["chunks_total"]
    for value in (0, 129, True, 4.5, "8"):
        with pytest.raises(UserError):
            indexing.settings(chat, {"embedding_batch": value})


def test_image_batch_is_chat_scoped_and_halves_on_oom(db, importer, tmp_path, monkeypatch):
    import numpy as np

    chat, service, _, _ = setup_index(db, importer, tmp_path)
    other = load(importer, export(tmp_path / "other", [message()], chat_id=200))["chat_id"]
    indexing, media = controls(db, importer, service)
    with db.connect() as conn:
        root = conn.execute("SELECT id FROM source_roots WHERE chat_id=?", (chat,)).fetchone()[0]
        for i in range(40):
            sha = f"{i:064x}"
            conn.execute("INSERT INTO media_blobs(sha256,size) VALUES(?,1)", (sha,))
            conn.execute(
                "INSERT INTO media_refs(chat_id,message_id,kind,relative_path,source_root_id,"
                "sha256,status) VALUES(?,1,'photo',?,?,?,'ready')",
                (chat, f"{i}.png", root, sha),
            )
    calls = []

    def encode(data):
        calls.append(len(data))
        if len(data) > 8:
            raise MemoryError()
        return np.tile(np.eye(1, 512, dtype=np.float32), (len(data), 1))

    media.clip = SimpleNamespace(
        space_id="clip-test",
        encode_images=encode,
        execution=SimpleNamespace(info=lambda: {"device": "gpu", "provider": "CUDA"}),
    )
    monkeypatch.setattr(media, "_read_photo", lambda sha: sha.encode())
    indexing.settings(chat, {"image_batch": 32})
    indexing.settings(other, {"image_batch": 1})
    # status needs only execution metadata, not a loaded real GPU session.
    media.clip.execution = SimpleNamespace(info=lambda: {"device": "gpu", "provider": "CUDA"})
    assert media._image_batch()
    assert calls[:3] == [32, 16, 8]
    assert indexing.status(chat)["media"]["images_ready"] == 32
    assert indexing.status(other)["media"]["images_ready"] == 0
    assert media.image_limits
    indexing.settings(chat, {"image_batch": 32})
    assert not media.image_limits  # Explicit settings changes retry the requested capacity.
    calls.clear()
    assert media._image_batch() and calls == [8]
    assert indexing.status(chat)["media"]["images_ready"] == 40
    assert not media._image_batch()


def test_eta_includes_unbuilt_days_and_isolates_device_rates(db, importer, tmp_path):
    chat, service, worker, work = setup_index(db, importer, tmp_path)
    # Stage a partial day, with later days still entirely unbuilt.
    worker.encoder.on_encode = lambda texts: service.control("pause")
    worker.run(work)
    with db.connect() as conn:
        for _ in range(2):
            record_rate(conn, "e5", service.encoder, 100, 1)
            record_rate(conn, "clip", service.encoder, 10, 2)
    before = service.estimates.text(service.encoder, chat)
    assert before > 0
    load(
        importer,
        export(
            tmp_path / "export",
            [
                message(100 + i, "новый день " * 100, date=1750000000 + 86400 * (i + 1))
                for i in range(3)
            ],
        ),
    )
    service.estimates.cache.clear()
    after = service.estimates.text(service.encoder, chat)
    assert after > before + 30
    assert service.estimates.images(service.encoder, 100) == 20
    service.encoder.execution = SimpleNamespace(provider="CUDAExecutionProvider")
    assert service.estimates.text(service.encoder, chat) is None
    assert service.estimates.images(service.encoder, 100) is None
    assert service.estimates.images(service.encoder, 0) == 0


def test_repeat_prepare_adopts_legacy_space_and_resumes_partial_cpu_index(
    db, importer, tmp_path, monkeypatch
):
    from telegram_search.inference.bundles import BundleStore
    from telegram_search.shared.text import serialize

    _, service, worker, work = setup_index(db, importer, tmp_path)
    old = service.encoder
    manifest = {
        "model": "synthetic-pinned-model",
        "tokenizer": "pinned",
        "pooling": "mean",
        "runtime": {
            "backend": "onnxruntime",
            "provider": "CPUExecutionProvider",
            "version": "1.30.0",
            "dtype": "float32",
        },
    }
    old.space_manifest = manifest
    with db.connect() as conn:
        conn.execute("UPDATE embedding_spaces SET manifest_json=?", (serialize(manifest),))
    old.on_encode = lambda texts: service.control("pause") if len(old.calls) == 2 else None
    partial = worker.run(work)
    assert 0 < partial["chunks_done"] < partial["chunks_total"]
    with db.connect() as conn:
        snapshot = [
            tuple(row) for row in conn.execute("SELECT id,generation FROM chunks ORDER BY id")
        ]
    candidate = TestEncoder("new-runtime-id")
    candidate.space_manifest = json.loads(json.dumps(manifest))
    candidate.space_manifest["runtime"]["version"] = "1.23.2"
    candidate.execution = SimpleNamespace(provider="CUDAExecutionProvider", info=lambda: {})

    def adopt(value):
        candidate.space_manifest = json.loads(value)
        candidate.space_id = old.space_id

    candidate.adopt_space = adopt
    monkeypatch.setattr(BundleStore, "prepare", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(service, "_new_encoder", lambda profile: candidate)
    service.prepare(offline=True)
    service.preparation.join(timeout=5)
    assert not service.preparation.is_alive()
    assert service.status()["preparation_state"] == "ready"
    assert service.encoder is candidate and candidate.space_id == old.space_id
    preparation_calls = len(candidate.calls)
    with db.connect() as conn:
        assert snapshot == [
            tuple(row) for row in conn.execute("SELECT id,generation FROM chunks ORDER BY id")
        ]
        assert (
            conn.execute("SELECT chunks_done FROM index_work WHERE id=?", (work,)).fetchone()[0]
            == partial["chunks_done"]
        )
    service.control("resume")
    resumed = SegmentWorker(db, candidate, worker.vectors, importer.lifecycle_lock).run(work)
    assert resumed["state"] == "done"
    assert (
        sum(candidate.calls[preparation_calls:]) == partial["chunks_total"] - partial["chunks_done"]
    )


def test_cached_update_health_is_fast_and_cannot_confirm_before_scan(tmp_path, monkeypatch):
    import threading
    from contextlib import contextmanager

    from fastapi.testclient import TestClient

    from telegram_search.backend.api import create_app
    from telegram_search.updates import installer

    nonce = "a" * 32
    monkeypatch.setenv("BTS_UPDATE_HEALTHCHECK", nonce)
    app = create_app(tmp_path / "health-workspace")
    started, release = threading.Event(), threading.Event()
    original = app.state.db.connect
    scans, commits = [], []

    @contextmanager
    def connect():
        with original() as conn:

            class Connection:
                def __getattr__(self, name):
                    return getattr(conn, name)

                def execute(self, sql, *args):
                    if sql == "PRAGMA quick_check":
                        scans.append(sql)
                        started.set()
                        assert release.wait(5)
                    return conn.execute(sql, *args)

            yield Connection()

    monkeypatch.setattr(app.state.db, "connect", connect)
    monkeypatch.setattr(installer, "commit_update", lambda *args: commits.append(args))
    with TestClient(app, base_url="http://localhost") as client:
        assert started.wait(2)
        headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
        began = time.monotonic()
        for _ in range(4):
            assert client.get("/api/doctor").json()["database_check"] == "checking"
        assert time.monotonic() - began < 1
        assert (
            client.post("/api/updates/confirm", headers=headers, json={"nonce": nonce}).status_code
            == 400
        )
        assert not commits and app.state.semantic.background is None
        release.set()
        deadline = time.monotonic() + 3
        while app.state.update_database_check == "checking" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert app.state.update_database_check == "ok" and len(scans) == 1
        assert client.post(
            "/api/updates/confirm", headers=headers, json={"nonce": nonce}
        ).json() == {"confirmed": True}
        assert len(commits) == 1 and app.state.media.background.is_alive()


@pytest.mark.parametrize("kind", ["text", "images"])
def test_rejected_model_preparation_keeps_other_chat_pause_flags(
    db, importer, tmp_path, monkeypatch, kind
):
    chat, service, _, _ = setup_index(db, importer, tmp_path)
    other = load(importer, export(tmp_path / "other", [message()], chat_id=200))["chat_id"]
    indexing, media = controls(db, importer, service)
    target = service if kind == "text" else media
    column = "text_paused" if kind == "text" else "media_paused"
    if kind == "text":
        with db.connect() as conn:
            conn.execute("UPDATE semantic_state SET enabled=0")
    monkeypatch.setattr(target, "prepare", lambda *args, **kwargs: None)
    indexing.prepare(chat, kind)
    with db.connect() as conn:
        before = [tuple(row) for row in conn.execute(f"SELECT id,{column} FROM chats ORDER BY id")]

    def rejected(*args, **kwargs):
        raise UserError("Подготовка уже выполняется")

    monkeypatch.setattr(target, "prepare", rejected)
    with pytest.raises(UserError):
        indexing.prepare(other, kind)
    with db.connect() as conn:
        assert before == [
            tuple(row) for row in conn.execute(f"SELECT id,{column} FROM chats ORDER BY id")
        ]


def test_shared_model_initialization_does_not_start_all_chats(db, importer, tmp_path, monkeypatch):
    chat, service, _, _ = setup_index(db, importer, tmp_path)
    indexing, _ = controls(db, importer, service)
    with db.connect() as conn:
        conn.execute("UPDATE semantic_state SET enabled=0")
    monkeypatch.setattr(service, "prepare", lambda **kwargs: None)
    indexing.initialize("text")
    assert indexing.status(chat)["semantic"]["paused"]


def test_scoped_pause_inside_inference_keeps_ids_and_does_not_publish(db, importer, tmp_path):
    chat, service, worker, work = setup_index(db, importer, tmp_path)
    indexing, _ = controls(db, importer, service)
    worker.encoder.on_encode = lambda texts: indexing.control(chat, "text", "pause")
    result = worker.run(work)
    assert result["state"] == "pending" and result["chunks_done"] == 0
    with db.connect() as conn:
        ids = [row[0] for row in conn.execute("SELECT id FROM chunks ORDER BY id")]
    worker.encoder.on_encode = None
    indexing.control(chat, "text", "resume")
    assert worker.run(work)["state"] == "done"
    with db.connect() as conn:
        assert ids == [row[0] for row in conn.execute("SELECT id FROM chunks ORDER BY id")]


def test_author_exclusion_supersedes_inflight_text_and_rebuilds_without_bot(db, importer, tmp_path):
    from telegram_search.search.hybrid import HybridSearch

    chat, service, worker, work = setup_index(
        db, importer, tmp_path, ["поезд человека", "рассылка робота", "поезд человека"]
    )
    indexing, media = controls(db, importer, service)
    try:
        worker.encoder.on_encode = lambda texts: indexing.settings(
            chat, {"excluded_author_ids": ["user2"]}
        )
        worker.run(work)
        with db.connect() as conn:
            row = conn.execute(
                "SELECT state,chunks_done FROM index_work WHERE id=?", (work,)
            ).fetchone()
            assert row["state"] == "superseded" and row["chunks_done"] == 0
            next_work = conn.execute("SELECT id FROM index_work WHERE state='pending'").fetchone()[
                0
            ]
        worker.encoder.on_encode = None
        assert worker.run(next_work)["state"] == "done"
        with db.connect() as conn:
            assert all("рассылка" not in row[0] for row in conn.execute("SELECT text FROM chunks"))
        search = HybridSearch(db, service, importer.lifecycle_lock)
        for mode in ("meaning", "hybrid"):
            hits = search.search("поезд", mode=mode)["results"]
            assert hits and all(m["message_id"] != 2 for hit in hits for m in hit["messages"])
        indexing.settings(chat, {"excluded_author_ids": []})
        with db.connect() as conn:
            next_work = conn.execute("SELECT id FROM index_work WHERE state='pending'").fetchone()[
                0
            ]
        assert worker.run(next_work)["state"] == "done"
        with db.connect() as conn:
            assert any("рассылка" in row[0] for row in conn.execute("SELECT text FROM chunks"))
    finally:
        media.shutdown()
        service.shutdown()


def test_inflight_clip_exclusion_rejects_vectors_and_reinclude_reuses_shared_cache(
    db, importer, tmp_path, monkeypatch
):
    import numpy as np
    from test_index_exclusions import seed

    chat, service, _, _ = setup_index(db, importer, tmp_path)
    bot_chat = seed(importer, tmp_path / "bots", 200)
    indexing, media = controls(db, importer, service)
    calls = []

    def encode(data):
        calls.append(len(data))
        indexing.settings(bot_chat, {"excluded_author_ids": ["bot"]})
        return np.ones((len(data), 512), dtype=np.float32)

    media.clip = SimpleNamespace(
        space_id="clip-test",
        encode_images=encode,
        unload=lambda: None,
        execution=SimpleNamespace(info=lambda: {"device": "cpu", "provider": "CPU"}),
    )
    monkeypatch.setattr(media, "_read_photo", lambda sha: sha.encode())
    try:
        assert media._image_batch()
        with db.connect() as conn:
            assert not conn.execute("SELECT 1 FROM media_embeddings").fetchone()
        assert not media._image_batch()
        indexing.settings(bot_chat, {"excluded_author_ids": []})
        media.clip.encode_images = lambda data: np.ones((len(data), 512), dtype=np.float32)
        assert media._image_batch() and media.status(bot_chat)["images_ready"] == 1
        indexing.settings(bot_chat, {"excluded_author_ids": ["bot"]})
        assert media.status(bot_chat)["total_photos"] == 0
        indexing.settings(bot_chat, {"excluded_author_ids": []})
        assert media.status(bot_chat)["images_ready"] == 1 and not media._image_batch()
        assert indexing.status(chat)["excluded_author_ids"] == []
    finally:
        media.shutdown()
        service.shutdown()
