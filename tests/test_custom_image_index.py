"""Public RGB fixtures exercise custom workers, real Lance and scoped search."""

import hashlib
import io
import json
import sys

import pytest
from conftest import load
from test_custom_adapters import package

pytest.importorskip("numpy")
pytest.importorskip("lancedb")
from PIL import Image  # noqa: E402
from test_media import photo_export, services  # noqa: E402

from telegram_search.config.model_registry import visual_specs  # noqa: E402
from telegram_search.indexing.exclusions import update_authors  # noqa: E402
from telegram_search.model_adapters.store import AdapterStore  # noqa: E402
from telegram_search.model_adapters.validation import validate_adapter  # noqa: E402
from telegram_search.search.cache import SearchCache  # noqa: E402
from telegram_search.search.history import SearchHistory  # noqa: E402
from telegram_search.search.hybrid import HybridSearch  # noqa: E402
from telegram_search.search.jobs import SearchJobs  # noqa: E402
from telegram_search.search.lexical import Filters  # noqa: E402
from telegram_search.search.media import MediaSearch, UnifiedSearch  # noqa: E402
from telegram_search.search.spec import SearchSpec  # noqa: E402
from telegram_search.shared.errors import UserError  # noqa: E402
from telegram_search.sources.service import WorkspaceService  # noqa: E402


def image_package(tmp_path, *, max_batch=2):
    path, data = package(tmp_path, "image_embedding")
    code = (path.parent / "adapter.py").read_text()
    code = code.replace(
        "'vectors':[[1,0,0,0] for i in images]",
        "'vectors':[[1,0,0,0] if i['pixels'][0]>i['pixels'][2] else [0,1,0,0] for i in images]",
    ).replace(
        "def encode_image_queries(self, items, context): "
        "return self.encode_text(items, 'query', context)",
        "def encode_image_queries(self, items, context):\n"
        "        if items[0]['text'] == 'HANG': time.sleep(60)\n"
        "        return {'ids':[i['id'] for i in items],'dtype':'float32',"
        "'vectors':[[0,1,0,0] if 'blue' in i['text'] else [1,0,0,0] for i in items]}",
    )
    (path.parent / "adapter.py").write_text(code, encoding="utf-8")
    data["input_contract"]["max_batch"] = max_batch
    data["files"][0].update(
        bytes=len(code.encode()), sha256=hashlib.sha256(code.encode()).hexdigest()
    )
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, data


def register(db, path):
    report = validate_adapter(AdapterStore(db.workspace), path, sys.executable, register=True)
    return "custom:" + report["identity"]


def activate(media, profile, *, reindex=False):
    media.prepare("images", profile=profile, reindex=reindex)
    media.preparation.join(30)
    assert not media.preparation.is_alive()
    with media.db.connect() as conn:
        state = dict(conn.execute("SELECT * FROM media_state").fetchone())
    assert state["preparation_state"] == "ready", state["error"]


def rgb(color):
    stream = io.BytesIO()
    Image.new("RGB", (24, 16), color).save(stream, format="PNG")
    return stream.getvalue()


def test_image_adapter_indexes_searches_filters_and_restarts(db, importer, tmp_path):
    path, data = image_package(tmp_path)
    profile = register(db, path)
    red = load(importer, photo_export(tmp_path / "red", chat_id=100, photo_data=rgb("red")))
    blue = load(importer, photo_export(tmp_path / "blue", chat_id=200, photo_data=rgb("blue")))
    semantic, media = services(db, importer)
    jobs = None
    try:
        activate(media, profile)
        space = media.clip.space_id
        assert media._image_batch() and media._image_batch()
        assert not media._image_batch()
        engine = UnifiedSearch(
            HybridSearch(db, semantic, importer.lifecycle_lock),
            MediaSearch(db, media, semantic, importer.lifecycle_lock),
        )
        jobs = SearchJobs(db, engine, importer.lifecycle_lock)
        job = jobs.create(SearchSpec(query="blue photo", modalities=["images"], mode="meaning"))
        jobs.jobs[job["job_id"]].future.result(timeout=30)
        assert jobs.status(job["job_id"])["state"] == "completed"
        result = jobs.result(job["job_id"])
        assert result["results"][0]["chat_id"] == blue["chat_id"]
        assert result["applied_models"]["images"]["profile"] == profile
        scoped = engine.search(
            "blue photo",
            Filters(chat_ids=[red["chat_id"]]),
            exact=False,
            limit=20,
            mode="meaning",
            modalities=["images"],
        )
        assert scoped["results"] and all(r["chat_id"] == red["chat_id"] for r in scoped["results"])
        with db.connect() as conn:
            update_authors(conn, blue["chat_id"], {"user1"})
        filtered = engine.search(
            "blue photo",
            Filters(chat_ids=[blue["chat_id"]]),
            exact=False,
            limit=20,
            mode="meaning",
            modalities=["images"],
        )
        assert not filtered["results"]
        data["revision"] = "new-colors-v2"
        path.write_text(json.dumps(data), encoding="utf-8")
        next_profile = register(db, path)
        with pytest.raises(UserError, match="переиндексацию"):
            media.prepare("images", profile=next_profile)
        assert media.clip.space_id == space
        activate(media, next_profile, reindex=True)
        assert media.clip.space_id != space
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    finally:
        if jobs:
            jobs.close()
        media.shutdown()
        semantic.shutdown()
    semantic, media = services(db, importer)
    try:
        assert media.clip.profile == next_profile
        assert media.clip.sessions == {}
    finally:
        media.shutdown()
        semantic.shutdown()


def test_image_package_damage_keeps_settings_history_and_keywords_usable(db, importer, tmp_path):
    path, _ = image_package(tmp_path)
    profile = register(db, path)
    load(importer, photo_export(tmp_path / "red", photo_data=rgb("red")))
    semantic, media = services(db, importer)
    try:
        activate(media, profile)
        media.clip.unload()
        package_path = AdapterStore(db.workspace).root / profile.removeprefix("custom:")
        (package_path / "manifest.json").write_text("broken")
        model_settings = WorkspaceService(db, importer, semantic, media).models()
        assert model_settings["clip"]["paths"] == [str(package_path)]
        assert visual_specs(profile, db.workspace, allow_unavailable=True)[profile].manifest[
            "unavailable"
        ]
        assert isinstance(SearchCache.revision(db), str)
        with db.connect() as conn:
            captured = SearchHistory._models(conn, db, {})
        assert captured["images"]["manifests"][0]["unavailable"]
    finally:
        media.shutdown()
        semantic.shutdown()


def test_decoded_budget_does_not_drop_pending_photos(db, importer, tmp_path):
    path, _ = image_package(tmp_path, max_batch=2)
    profile = register(db, path)
    # One chat, two distinct blobs; byte budget admits exactly one per batch.
    root = tmp_path / "source"
    root.mkdir()
    (root / "red.png").write_bytes(rgb("red"))
    (root / "blue.png").write_bytes(rgb("blue"))
    from conftest import export, message

    load(importer, export(root, [message(1, photo="red.png"), message(2, photo="blue.png")]))
    semantic, media = services(db, importer)
    try:
        activate(media, profile)
        media.clip.prepared_byte_limit = 24 * 16 * 3
        assert media._image_batch()
        assert media._image_batch()
        assert not media._image_batch()
        with db.connect() as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM media_embeddings WHERE kind='image'").fetchone()[
                    0
                ]
                == 2
            )
            assert conn.execute("SELECT COUNT(*) FROM media_failures").fetchone()[0] == 0
        assert media.clip.last_timings["batch_size"] == 1
    finally:
        media.shutdown()
        semantic.shutdown()


def test_custom_image_device_uses_its_own_runtime_and_query_cancellation(
    db, importer, tmp_path, monkeypatch
):
    import threading

    from telegram_search.search.execution import SearchCancelled, SearchExecution, execution_context

    path, _ = image_package(tmp_path)
    profile = register(db, path)
    semantic, media = services(db, importer)
    thread = None
    execution = SearchExecution()
    try:
        activate(media, profile)
        space = media.clip.space_id

        def builtin_probe_forbidden(*args, **kwargs):
            raise AssertionError("Custom devices must not depend on the bundled ORT provider")

        monkeypatch.setattr(
            "telegram_search.inference.providers.Execution", builtin_probe_forbidden
        )
        changed = WorkspaceService(db, importer, semantic, media).change_device(
            "cpu", model="clip", search_device="cpu"
        )
        assert changed["execution"]["provider"] == "custom:cpu"
        assert media.clip.space_id == space
        errors = []

        def query():
            try:
                with execution_context(execution):
                    media.clip.encode_text(["HANG"])
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=query)
        thread.start()
        import time

        deadline = time.monotonic() + 5
        while "text" not in media.clip.sessions and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "text" in media.clip.sessions
        execution.cancelled.set()
        thread.join(5)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], SearchCancelled)
        assert not media.clip.backend_info()["query_loaded"]
        assert media.clip.encode_text(["blue"])[0].tolist() == [0, 1, 0, 0]
    finally:
        execution.cancelled.set()
        if thread:
            thread.join(5)
        media.shutdown()
        semantic.shutdown()


@pytest.mark.parametrize("stage", ["load", "image"])
def test_shutdown_cancels_unpublished_preparation_and_image_worker(
    db, importer, tmp_path, monkeypatch, stage
):
    import threading
    from dataclasses import replace

    from telegram_search.model_adapters.client import AdapterWorker

    path, data = image_package(tmp_path)
    code = (path.parent / "adapter.py").read_text()
    if stage == "load":
        code = code.replace(
            "def load(self, context): pass",
            "def load(self, context):\n        if context.cpu_threads == 3: time.sleep(60)",
        )
    else:
        code = code.replace(
            "def encode_images(self, images, context):\n",
            "def encode_images(self, images, context):\n"
            "        if images[0]['width'] > 2: time.sleep(60)\n",
        )
    (path.parent / "adapter.py").write_text(code, encoding="utf-8")
    data["files"][0].update(
        bytes=len(code.encode()), sha256=hashlib.sha256(code.encode()).hexdigest()
    )
    path.write_text(json.dumps(data), encoding="utf-8")
    profile = register(db, path)
    load(importer, photo_export(tmp_path / "source", photo_data=rgb("red")))
    if stage == "load":
        db.settings = replace(db.settings, cpu_threads=3)
    semantic, media = services(db, importer)
    entered = threading.Event()
    workers = []
    request = AdapterWorker.request

    def observed(worker, operation, payload=None, **kwargs):
        if (
            stage == "load"
            and operation == "load"
            or (
                stage == "image"
                and operation == "encode_images"
                and payload["images"][0]["width"] > 2
            )
        ):
            workers.append(worker)
            entered.set()
        return request(worker, operation, payload, **kwargs)

    monkeypatch.setattr(AdapterWorker, "request", observed)
    shutdown = None
    try:
        if stage == "load":
            media.prepare("images", profile=profile)
        else:
            activate(media, profile)
            media.image_background = threading.Thread(target=media._image_batch)
            media.image_background.start()
        assert entered.wait(5)
        shutdown = threading.Thread(target=media.shutdown, daemon=True)
        shutdown.start()
        shutdown.join(5)
        assert not shutdown.is_alive()
        assert all(worker.process.poll() is not None for worker in workers)
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM media_failures").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM media_embeddings").fetchone()[0] == 0
            if stage == "load":
                assert conn.execute("SELECT images_enabled FROM media_state").fetchone()[0] == 0
    finally:
        media.stop.set()
        for worker in workers:
            worker.close()
        if shutdown:
            shutdown.join(5)
        media.shutdown()
        semantic.shutdown()


def test_direct_image_preparation_checks_total_before_next_decode(
    db, importer, tmp_path, monkeypatch
):
    from PIL import ImageOps

    path, _ = image_package(tmp_path)
    profile = register(db, path)
    semantic, media = services(db, importer)
    try:
        activate(media, profile)
        media.clip.prepared_byte_limit = 2 * 24 * 16 * 3
        decoded = []
        original = ImageOps.exif_transpose

        def tracked(image):
            decoded.append(True)
            return original(image)

        monkeypatch.setattr(ImageOps, "exif_transpose", tracked)
        with pytest.raises(UserError, match="memory budget"):
            media.clip.prepare_images([rgb("red"), rgb("blue"), rgb("green")])
        assert len(decoded) == 2
    finally:
        media.shutdown()
        semantic.shutdown()


@pytest.mark.parametrize("operation", ["images", "query"])
def test_replaced_image_encoder_cannot_restart_captured_worker(
    db, importer, tmp_path, monkeypatch, operation
):
    path, data = image_package(tmp_path)
    profile = register(db, path)
    load(importer, photo_export(tmp_path / "source", text="red caption", photo_data=rgb("red")))
    semantic, media = services(db, importer)
    try:
        activate(media, profile)
        old = media.clip
        data["revision"] = "replacement-v2"
        path.write_text(json.dumps(data), encoding="utf-8")
        next_profile = register(db, path)
        method = old.encode_images if operation == "images" else old.encode_text

        def replace_before_inference(*args, **kwargs):
            # The old model was captured and passed the queue/search guard.
            media._prepare("images", False, profile=next_profile, reindex=True)
            return method(*args, **kwargs)

        if operation == "images":
            monkeypatch.setattr(old, "encode_images", replace_before_inference)
            assert media._image_batch()
        else:
            monkeypatch.setattr(old, "encode_text", replace_before_inference)
            search = UnifiedSearch(
                HybridSearch(db, semantic, importer.lifecycle_lock),
                MediaSearch(db, media, semantic, importer.lifecycle_lock),
            )
            result = search.search(
                "red", Filters(), False, 20, mode="hybrid", modalities=["text", "images"]
            )
            assert result["results"]
            assert any("model changed" in warning for warning in result["warnings"])
        assert media.clip is not old
        assert old.sessions == {} and old.cancelled.retired.is_set()
        assert not media.stop.is_set() and not media.clip.cancelled.is_set()
        with pytest.raises(UserError, match="model changed"):
            type(old).encode_text(old, ["blue"])
        assert old.sessions == {}
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM media_failures").fetchone()[0] == 0
        assert media._image_batch()
    finally:
        media.shutdown()
        semantic.shutdown()


def test_late_retirement_cleanup_failure_does_not_rollback_committed_model(
    db, importer, tmp_path, monkeypatch
):
    path, data = image_package(tmp_path)
    profile = register(db, path)
    semantic, media = services(db, importer)
    try:
        activate(media, profile)
        old = media.clip
        data["revision"] = "replacement-v2"
        path.write_text(json.dumps(data), encoding="utf-8")
        next_profile = register(db, path)
        unload = old.unload
        calls = []

        def cleanup():
            calls.append(True)
            if len(calls) == 2:
                raise UserError("Synthetic cleanup failure")
            unload()

        monkeypatch.setattr(old, "unload", cleanup)
        activate(media, next_profile, reindex=True)
        assert media.clip.profile == next_profile
        assert old.cancelled.retired.is_set()
        assert media.resource_error == "Previous custom image worker cleanup is incomplete."
        with db.connect() as conn:
            row = conn.execute("SELECT visual_profile,visual_space_id FROM media_state").fetchone()
            assert tuple(row) == (next_profile, media.clip.space_id)
    finally:
        media.shutdown()
        semantic.shutdown()


def test_retirement_attempts_all_owned_workers_after_a_cleanup_failure(db, importer, tmp_path):
    from types import SimpleNamespace

    path, _ = image_package(tmp_path)
    profile = register(db, path)
    semantic, media = services(db, importer)
    try:
        activate(media, profile)
        encoder = media.clip
        attempted = []

        def fail():
            attempted.append("image")
            raise UserError("Synthetic process cleanup failure")

        def close_text():
            attempted.append("text")

        encoder.sessions = {
            "image": SimpleNamespace(close=fail),
            "text": SimpleNamespace(close=close_text),
        }
        encoder.retire()
        assert attempted == ["image", "text"]
        assert list(encoder.sessions) == ["image"]
        assert encoder.retirement_error
        # The synthetic handle has no process; actual failures are client-fenced.
        encoder.sessions.clear()
    finally:
        media.shutdown()
        semantic.shutdown()
