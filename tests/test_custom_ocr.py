"""Public images, real isolated OCR workers; no model downloads or private archive."""

import hashlib
import io
import json
import sys
import threading
from dataclasses import replace

import pytest
from PIL import Image
from test_custom_adapters import package

from telegram_search.config.settings import Settings
from telegram_search.model_adapters.client import AdapterWorker
from telegram_search.model_adapters.ocr import AdapterOcrEngine
from telegram_search.model_adapters.registry import custom_spec
from telegram_search.model_adapters.store import AdapterStore
from telegram_search.model_adapters.validation import validate_adapter


def ocr_package(tmp_path, *, hang=None):
    path, data = package(tmp_path, "ocr")
    code = (path.parent / "adapter.py").read_text()
    code = code.replace(
        "return [{'id':i['id'],'text':'','regions':[]} for i in images]",
        "return [{'id':i['id'],'text':('red' if i['pixels'][0]>i['pixels'][2] else 'blue')"
        "+' ticket 12345 '+str(i['width']),'confidence':0.9,'regions':[{'text':'ticket',"
        "'bbox':[0,0,min(2,i['width']),min(2,i['height'])],'confidence':0.8}]} for i in images]",
    )
    if hang == "image":
        code = code.replace(
            "def recognize(self, images, context):\n",
            "def recognize(self, images, context):\n"
            "        if images[0]['width']>2: time.sleep(60)\n",
        )
    if hang == "load":
        code = code.replace(
            "def load(self, context): pass",
            "def load(self, context):\n        if context.cpu_threads==3: time.sleep(60)",
        )
    raw = code.encode("utf-8")
    (path.parent / "adapter.py").write_bytes(raw)
    data["files"][0].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    data["input_contract"]["max_batch"] = 4
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def engine(tmp_path, *, hang=None, **options):
    workspace = tmp_path / "workspace"
    report = validate_adapter(
        AdapterStore(workspace), ocr_package(tmp_path, hang=hang), sys.executable, register=True
    )
    spec = custom_spec(workspace, "custom:" + report["identity"], role="ocr")
    return AdapterOcrEngine(spec, workspace, replace(Settings(), **options))


def photo(color="red", size=(24, 16)):
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, format="PNG")
    return stream.getvalue()


def test_custom_ocr_batches_resizes_and_maps_optional_regions(tmp_path, monkeypatch):
    model = engine(tmp_path, ocr_max_edge=128)
    calls = []
    request = AdapterWorker.request

    def observed(worker, operation, payload=None, **kwargs):
        if operation == "recognize":
            calls.append(len(payload["images"]))
        return request(worker, operation, payload, **kwargs)

    monkeypatch.setattr(AdapterWorker, "request", observed)
    try:
        model.prepared_byte_limit = 2 * 24 * 16 * 3
        values = model.recognize_many([photo("red"), photo("blue"), photo("red")])
        assert calls == [2, 1]
        assert values[0]["text"].startswith("red ticket")
        assert values[1]["text"].startswith("blue ticket")
        assert values[0]["confidence"] == 90
        assert values[0]["regions"][0]["bbox"] == [0, 0, 2, 2]
        model.prepared_byte_limit = 24 * 1024**2
        assert model.recognize(photo(size=(640, 32)))["text"].endswith("128")
        assert model.backend_info()["provider"] == "custom:cpu"
        before = model.version
        model.unload()
        assert model.recognize(photo())["text"].startswith("red")
        assert model.version == before
    finally:
        model.unload()


@pytest.mark.parametrize("stage", ["image", "load"])
def test_custom_ocr_unload_interrupts_rpc_and_leaves_cancellable_result(
    tmp_path, monkeypatch, stage
):
    model = engine(tmp_path, hang=stage, cpu_threads=3 if stage == "load" else 4)
    entered = threading.Event()
    workers = []
    result = []
    request = AdapterWorker.request

    def observed(worker, operation, payload=None, **kwargs):
        if (stage == "load" and operation == "load") or (
            stage == "image" and operation == "recognize"
        ):
            workers.append(worker)
            entered.set()
        return request(worker, operation, payload, **kwargs)

    monkeypatch.setattr(AdapterWorker, "request", observed)
    thread = threading.Thread(target=lambda: result.append(model.recognize(photo())))
    try:
        thread.start()
        assert entered.wait(5)
        model.unload_worker()
        thread.join(5)
        assert not thread.is_alive()
        assert result == [{"cancelled": True}]
        assert all(worker.process.poll() is not None for worker in workers)
    finally:
        model.unload()
        thread.join(5)


def test_custom_ocr_does_not_load_scientific_runtime_into_parent(tmp_path):
    import subprocess

    code = """
import builtins
original=builtins.__import__
def checked(name,*args,**kwargs):
 if name.split('.')[0] in {'numpy','onnxruntime','lancedb','safetensors'}:
  raise AssertionError('Scientific dependency imported by parent OCR')
 return original(name,*args,**kwargs)
builtins.__import__=checked
from telegram_search.model_adapters.ocr import AdapterOcrEngine
print('OCR parent imports stay lightweight')
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_registered_ocr_selection_is_data_only_and_real_queue_search_restarts(
    db, importer, tmp_path, monkeypatch
):
    from conftest import load
    from test_media import photo_export, services

    from telegram_search.indexing.exclusions import update_authors
    from telegram_search.search.lexical import Filters
    from telegram_search.search.media import MediaSearch
    from telegram_search.sources.service import WorkspaceService

    report = validate_adapter(
        AdapterStore(db.workspace), ocr_package(tmp_path), sys.executable, register=True
    )
    profile = "custom:" + report["identity"]
    a = load(importer, photo_export(tmp_path / "a", chat_id=100, photo_data=photo()))
    b = load(importer, photo_export(tmp_path / "b", chat_id=200, photo_data=photo()))
    semantic, media = services(db, importer)
    try:

        def forbidden(*args, **kwargs):
            raise AssertionError("Selection must not execute adapter code")

        with monkeypatch.context() as guard:
            guard.setattr(AdapterWorker, "__init__", forbidden)
            selected = WorkspaceService(db, importer, semantic, media).change_device(
                "cpu", model="ocr", ocr_engine=profile, defer_preparation=True
            )
        assert selected["model"]["engine"] == profile
        assert selected["model"]["profile_names"][profile].startswith("custom/")
        assert media.ocr is None
        assert media.status()["ocr_runtime_installed"]
        media.prepare("ocr", offline=True)
        media.preparation.join(10)
        assert not media.preparation.is_alive()
        assert media.status()["ocr_available"], media.status()["error"]
        version = media.ocr.version
        assert media._ocr_one()
        assert not media._ocr_one()
        with db.connect() as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM ocr_cache WHERE state='ready'").fetchone()[0]
                == 1
            )
        search = MediaSearch(db, media, semantic, importer.lifecycle_lock)
        hits, _ = search.search(
            "ticket 12345", Filters(chat_ids=[b["chat_id"]]), kind="ocr", mode="words"
        )
        assert hits and hits[0]["chat_id"] == b["chat_id"]
        with db.connect() as conn:
            update_authors(conn, a["chat_id"], {"user1"})
        assert not search.search("ticket", Filters(chat_ids=[a["chat_id"]]), kind="ocr")[0]
    finally:
        media.shutdown()
        semantic.shutdown()
    semantic, media = services(db, importer)
    try:
        assert media.ocr.version == version and media.ocr.sessions == {}
        assert MediaSearch(db, media, semantic, importer.lifecycle_lock).search(
            "ticket", Filters(chat_ids=[b["chat_id"]]), kind="ocr"
        )[0]
    finally:
        media.shutdown()
        semantic.shutdown()


def test_custom_ocr_cpu_pool_uses_thread_budget_without_resource_deadlock(db, importer, tmp_path):
    from conftest import export, load, message
    from test_media import services

    report = validate_adapter(
        AdapterStore(db.workspace), ocr_package(tmp_path), sys.executable, register=True
    )
    db.settings = replace(
        db.settings,
        ocr_engine="custom:" + report["identity"],
        ocr_cpu_workers=2,
        cpu_threads=4,
        ocr_batch_size=2,
    )
    db.settings.validate()
    db.settings.save(db.workspace)
    source = tmp_path / "photos"
    source.mkdir()
    (source / "red.png").write_bytes(photo("red"))
    (source / "blue.png").write_bytes(photo("blue"))
    load(importer, export(source, [message(1, photo="red.png"), message(2, photo="blue.png")]))
    semantic, media = services(db, importer)
    finished = threading.Event()
    errors = []
    thread = None
    try:
        media.prepare("ocr", offline=True)
        media.preparation.join(10)
        assert media.status()["ocr_available"], media.status()["error"]
        assert len(media.ocr.engines) == 2
        assert sum(e.threads for e in media.ocr.engines) == 4

        def work():
            try:
                media._ocr_run()
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=work)
        thread.start()
        assert finished.wait(10)
        assert not errors
        assert media.status()["ocr_ready"] == 2
    finally:
        media.shutdown()
        if thread:
            thread.join(5)
        semantic.shutdown()


def test_custom_ocr_revision_preserves_old_cache_and_retires_previous_worker(
    db, importer, tmp_path
):
    from conftest import load
    from test_media import photo_export, services

    from telegram_search.search.lexical import Filters
    from telegram_search.search.media import MediaSearch
    from telegram_search.sources.service import WorkspaceService

    path = ocr_package(tmp_path)
    report = validate_adapter(AdapterStore(db.workspace), path, sys.executable, register=True)
    load(importer, photo_export(tmp_path / "source", photo_data=photo()))
    semantic, media = services(db, importer)
    workspace = WorkspaceService(db, importer, semantic, media)
    try:
        workspace.change_device(
            "cpu", model="ocr", ocr_engine="custom:" + report["identity"], defer_preparation=True
        )
        media.prepare("ocr")
        media.preparation.join(10)
        assert media._ocr_one()
        old = media.ocr
        data = json.loads(path.read_text())
        raw = (path.parent / "adapter.py").read_bytes().replace(b"ticket 12345", b"invoice 77777")
        (path.parent / "adapter.py").write_bytes(raw)
        data["revision"] = "v2"
        data["files"][0].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        path.write_text(json.dumps(data), encoding="utf-8")
        revised = validate_adapter(AdapterStore(db.workspace), path, sys.executable, register=True)
        workspace.change_device(
            "cpu", model="ocr", ocr_engine="custom:" + revised["identity"], defer_preparation=True
        )
        assert old.cancelled.retired.is_set()
        assert old.recognize(photo()) == {"cancelled": True}
        media.prepare("ocr")
        media.preparation.join(10)
        assert media.ocr.version != old.version
        assert media._ocr_one()
        search = MediaSearch(db, media, semantic, importer.lifecycle_lock)
        assert not search.search("ticket", Filters(), kind="ocr")[0]
        assert search.search("invoice", Filters(), kind="ocr")[0]
        with db.connect() as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM ocr_cache WHERE state='ready'").fetchone()[0]
                == 2
            )
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    finally:
        media.shutdown()
        semantic.shutdown()


def test_custom_ocr_hybrid_mock_has_shared_version_and_disjoint_claims(
    db, importer, tmp_path, monkeypatch
):
    from conftest import export, load, message
    from test_media import services

    path = ocr_package(tmp_path)
    data = json.loads(path.read_text())
    raw = (
        (path.parent / "adapter.py")
        .read_bytes()
        .replace(b"'devices':['cpu']", b"'devices':['cpu','gpu']")
    )
    (path.parent / "adapter.py").write_bytes(raw)
    data["devices"] = ["cpu", "gpu"]
    data["files"][0].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    path.write_text(json.dumps(data), encoding="utf-8")
    report = validate_adapter(AdapterStore(db.workspace), path, sys.executable, register=True)
    db.settings = replace(
        db.settings,
        ocr_engine="custom:" + report["identity"],
        ocr_device="hybrid",
        cpu_threads=4,
        ocr_batch_size=1,
    )
    db.settings.validate()
    db.settings.save(db.workspace)
    root = tmp_path / "photos"
    root.mkdir()
    (root / "red.png").write_bytes(photo("red"))
    (root / "blue.png").write_bytes(photo("blue"))
    load(importer, export(root, [message(1, photo="red.png"), message(2, photo="blue.png")]))
    semantic, media = services(db, importer)
    threads = []
    errors = []
    try:
        media.prepare("ocr")
        media.preparation.join(10)
        assert media.status()["ocr_available"], media.status()["error"]
        assert media.ocr.cpu_peer.version == media.ocr.version
        assert media.ocr.threads + media.ocr.cpu_peer.threads == 4
        barrier = threading.Barrier(2)
        devices = []
        request = AdapterWorker.request

        def observed(worker, operation, payload=None, **kwargs):
            if operation == "recognize" and payload["images"][0]["width"] > 2:
                devices.append(worker.context.device)
                barrier.wait(timeout=10)
            return request(worker, operation, payload, **kwargs)

        monkeypatch.setattr(AdapterWorker, "request", observed)

        def work(engine):
            try:
                media._ocr_run(engine)
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=work, args=(engine,))
            for engine in (media.ocr, media.ocr.cpu_peer)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
        assert not any(thread.is_alive() for thread in threads)
        assert not errors
        assert sorted(devices) == ["cpu", "gpu"]
        assert media.status()["ocr_ready"] == 2
        with db.connect() as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM ocr_work WHERE state='done'").fetchone()[0] == 2
            )
    finally:
        media.shutdown()
        for thread in threads:
            thread.join(5)
        semantic.shutdown()


def test_custom_ocr_dense_uses_selected_text_encoder_without_recognition_again(
    db, importer, tmp_path
):
    pytest.importorskip("numpy")
    pytest.importorskip("lancedb")
    from conftest import load
    from test_media import photo_export, services

    from telegram_search.search.lexical import Filters
    from telegram_search.search.media import MediaSearch

    report = validate_adapter(
        AdapterStore(db.workspace), ocr_package(tmp_path), sys.executable, register=True
    )
    db.settings = replace(db.settings, ocr_engine="custom:" + report["identity"])
    db.settings.save(db.workspace)
    load(importer, photo_export(tmp_path / "source", photo_data=photo()))
    semantic, media = services(db, importer)
    try:
        media.prepare("ocr")
        media.preparation.join(10)
        assert media._ocr_one()
        text_path, _ = package(tmp_path, "text_embedding")
        text = validate_adapter(
            AdapterStore(db.workspace), text_path, sys.executable, register=True
        )
        semantic.prepare("custom:" + text["identity"])
        semantic.preparation.join(15)
        assert semantic.status()["dense_available"], semantic.status()["error"]
        assert media._ocr_embeddings()
        hits, _ = MediaSearch(db, media, semantic, importer.lifecycle_lock).search(
            "ticket", Filters(), kind="ocr", mode="meaning"
        )
        assert hits and "ocr_meaning" in hits[0]["matched_by"]
        assert not media._ocr_one()
    finally:
        media.shutdown()
        semantic.shutdown()


def test_global_gpu_limits_rebuild_custom_ocr_without_bundled_ort_probe(
    db, importer, tmp_path, monkeypatch
):
    from test_media import services

    from telegram_search.sources.service import WorkspaceService

    report = validate_adapter(
        AdapterStore(db.workspace), ocr_package(tmp_path), sys.executable, register=True
    )
    db.settings = replace(db.settings, ocr_engine="custom:" + report["identity"])
    db.settings.save(db.workspace)
    semantic, media = services(db, importer)
    try:
        media.prepare("ocr")
        media.preparation.join(10)
        old = media.ocr

        def forbidden(*args, **kwargs):
            raise AssertionError("An OCR-only custom runtime must not probe bundled ORT")

        monkeypatch.setattr("telegram_search.inference.providers.Execution", forbidden)
        WorkspaceService(db, importer, semantic, media).change_device(
            "cpu", gpu_device_id=1, gpu_memory_limit_mib=512
        )
        assert media.ocr is not old
        assert media.ocr.version == old.version
        assert media.ocr.execution.device_id == 1
        assert media.ocr.execution.memory_limit_mib == 512
        media.ocr.recognize(photo())
        context = media.ocr.sessions["image"].context
        assert context.gpu_device_id == 1 and context.gpu_memory_limit_mib == 512
    finally:
        media.shutdown()
        semantic.shutdown()


@pytest.mark.parametrize("failure", [SystemExit, OSError])
def test_deferred_ocr_choice_disables_before_config_and_rolls_back_safe_errors(
    db, importer, tmp_path, monkeypatch, failure
):
    from test_media import services

    from telegram_search.sources.service import WorkspaceService
    from telegram_search.storage.database import Database

    path = ocr_package(tmp_path)
    original = validate_adapter(AdapterStore(db.workspace), path, sys.executable, register=True)
    old_profile = "custom:" + original["identity"]
    db.settings = replace(db.settings, ocr_engine=old_profile)
    db.settings.save(db.workspace)
    data = json.loads(path.read_text())
    data["revision"] = "selected-but-not-prepared"
    path.write_text(json.dumps(data), encoding="utf-8")
    report = validate_adapter(AdapterStore(db.workspace), path, sys.executable, register=True)
    new_profile = "custom:" + report["identity"]
    semantic, media = services(db, importer)
    try:
        media.prepare("ocr")
        media.preparation.join(10)
        save = Settings.save

        def interrupted(settings, workspace):
            if settings.ocr_engine == new_profile:
                if failure is SystemExit:
                    save(settings, workspace)
                raise failure("Synthetic interrupted config publication")
            return save(settings, workspace)

        with monkeypatch.context() as guard:
            guard.setattr(Settings, "save", interrupted)
            with pytest.raises(failure):
                WorkspaceService(db, importer, semantic, media).change_device(
                    "cpu", model="ocr", ocr_engine=new_profile, defer_preparation=True
                )
        if failure is OSError:
            assert db.settings.ocr_engine == old_profile
            assert media.ocr is not None
            with db.connect() as conn:
                assert conn.execute("SELECT ocr_enabled FROM media_state").fetchone()[0] == 1
    finally:
        media.shutdown()
        semantic.shutdown()
    if failure is SystemExit:
        restarted = Database(db.workspace)
        restarted.initialize()
        semantic, media = services(restarted, importer)
        try:
            assert restarted.settings.ocr_engine == new_profile
            assert media.ocr is None
            assert not media._ocr_one()
            with restarted.connect() as conn:
                assert conn.execute("SELECT ocr_enabled FROM media_state").fetchone()[0] == 0
        finally:
            media.shutdown()
            semantic.shutdown()


def test_pause_preserves_completed_rpc_groups(tmp_path, monkeypatch):
    path = ocr_package(tmp_path)
    data = json.loads(path.read_text())
    raw = (
        (path.parent / "adapter.py")
        .read_bytes()
        .replace(
            b"def recognize(self, images, context):\n",
            b"def recognize(self, images, context):\n"
            b"        if images[0]['pixels'][2]>images[0]['pixels'][0]: time.sleep(60)\n",
        )
    )
    (path.parent / "adapter.py").write_bytes(raw)
    data["files"][0].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    path.write_text(json.dumps(data), encoding="utf-8")
    workspace = tmp_path / "workspace"
    report = validate_adapter(AdapterStore(workspace), path, sys.executable, register=True)
    model = AdapterOcrEngine(
        custom_spec(workspace, "custom:" + report["identity"], role="ocr"), workspace, Settings()
    )
    model.prepared_byte_limit = 24 * 16 * 3
    entered = threading.Event()
    results = []
    request = AdapterWorker.request

    def observed(worker, operation, payload=None, **kwargs):
        if operation == "recognize" and payload["images"][0]["pixels"].startswith("AAD/"):
            entered.set()
        return request(worker, operation, payload, **kwargs)

    monkeypatch.setattr(AdapterWorker, "request", observed)
    thread = threading.Thread(
        target=lambda: results.extend(model.recognize_many([photo("red"), photo("blue")]))
    )
    try:
        thread.start()
        assert entered.wait(5)
        model.unload_worker()
        thread.join(5)
        assert not thread.is_alive()
        assert results[0]["text"].startswith("red")
        assert results[1] == {"cancelled": True}
    finally:
        model.unload()
        thread.join(5)


def test_invalid_unicode_from_ocr_becomes_failed_photo_not_infinite_retry(db, importer, tmp_path):
    from conftest import export, load, message
    from test_media import services

    path = ocr_package(tmp_path)
    data = json.loads(path.read_text())
    raw = (
        (path.parent / "adapter.py")
        .read_bytes()
        .replace(
            b"def recognize(self, images, context):\n",
            b"def recognize(self, images, context):\n"
            b"        if images[0]['width']>2 and images[0]['pixels'][0]>images[0]['pixels'][2]:"
            b" return [{'id':i['id'],'text':'\\ud800'} for i in images]\n",
        )
    )
    (path.parent / "adapter.py").write_bytes(raw)
    data["files"][0].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    path.write_text(json.dumps(data), encoding="utf-8")
    report = validate_adapter(AdapterStore(db.workspace), path, sys.executable, register=True)
    db.settings = replace(db.settings, ocr_engine="custom:" + report["identity"])
    db.settings.save(db.workspace)
    source = tmp_path / "photos"
    source.mkdir()
    (source / "red.png").write_bytes(photo("red"))
    (source / "blue.png").write_bytes(photo("blue"))
    load(importer, export(source, [message(1, photo="red.png"), message(2, photo="blue.png")]))
    semantic, media = services(db, importer)
    try:
        media.prepare("ocr")
        media.preparation.join(10)
        for _ in range(5):
            if not media._ocr_run():
                break
        with db.connect() as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM ocr_work WHERE state='pending'").fetchone()[0]
                == 0
            )
            assert (
                conn.execute("SELECT COUNT(*) FROM ocr_cache WHERE state='failed'").fetchone()[0]
                == 1
            )
            assert (
                conn.execute("SELECT COUNT(*) FROM ocr_cache WHERE state='ready'").fetchone()[0]
                == 1
            )
    finally:
        media.shutdown()
        semantic.shutdown()
