"""No GPU/model downloads: scheduling, validation and session configuration."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from telegram_search.backend.api import create_app
from telegram_search.config.settings import Settings
from telegram_search.inference.resources import (
    CombinedGate,
    CpuGate,
    GpuGate,
    compute_gate,
    compute_lock,
    configure_gpu_load,
)
from telegram_search.shared.errors import UserError
from telegram_search.sources.service import WorkspaceService


def test_gpu_load_settings_validate_and_roundtrip(tmp_path):
    assert Settings().gpu_index_load_percent == 100
    for value in (10, 25, 60, 100):
        settings = replace(Settings(), gpu_index_load_percent=value)
        settings.save(tmp_path)
        assert Settings.load(tmp_path).gpu_index_load_percent == value
    for value in (True, 0, 101, 50.5, "50", None):
        with pytest.raises(UserError):
            replace(Settings(), gpu_index_load_percent=value).validate()


def test_gpu_cooldown_is_shared_but_does_not_delay_queries_or_cpu():
    now = [0.0]
    gate = GpuGate(25, clock=lambda: now[0])
    # A completed long batch has a bounded two-second cooldown.
    gate.released(time.monotonic() - 5, False)
    assert gate.background_delay() == 2.0
    background = threading.Event()
    with ThreadPoolExecutor(max_workers=2) as executor:

        def run():
            with gate:
                background.set()

        future = executor.submit(run)
        try:
            deadline = time.monotonic() + 3
            while not gate.waiters:
                assert time.monotonic() < deadline
                time.sleep(0.001)
            with gate.slot(interactive=True):
                assert not background.is_set()
            with compute_lock:
                assert not background.is_set()
        finally:
            gate.configure(100)
        future.result(timeout=3)
        assert background.is_set()


def test_limit_applies_to_existing_and_future_device_gates_only():
    try:
        first = compute_gate(SimpleNamespace(provider="CUDAExecutionProvider", device_id=12))
        configure_gpu_load(25)
        second = compute_gate(SimpleNamespace(provider="CoreMLExecutionProvider", device_id=13))
        assert first.percent == second.percent == 25
        assert compute_gate() is compute_lock
        assert (
            compute_gate(SimpleNamespace(provider="CUDAExecutionProvider", device_id=12)) is first
        )
    finally:
        configure_gpu_load(100)


def test_gpu_settings_and_clear_endpoint_security(db):
    with TestClient(create_app(db.workspace), base_url="http://127.0.0.1") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"x-session-token": token}
        body = {"gpu_index_load_percent": 25, "gpu_memory_limit_mib": 6144}
        assert client.patch("/api/settings", json=body).status_code == 403
        assert client.patch("/api/settings", json=body, headers=headers).status_code == 200
        settings = client.get("/api/settings").json()
        assert settings["gpu_index_load_percent"] == 25 and settings["gpu_memory_limit_mib"] == 6144
        assert settings["e5_device"] is None and settings["ocr_device"] == "cpu"
        assert (
            client.patch(
                "/api/settings", json={"gpu_index_load_percent": True}, headers=headers
            ).status_code
            == 400
        )
        path = "/api/storage/vectors/text/clear"
        assert client.post(path, json={"confirm": True}).status_code == 403
        assert client.post(path, json={"confirm": False}, headers=headers).status_code == 400
        assert client.post(path, json={"confirm": 1}, headers=headers).status_code == 422
        assert (
            client.post(
                path, json={"confirm": True}, headers={**headers, "origin": "https://evil.invalid"}
            ).status_code
            == 403
        )
        assert client.get("/api/storage/vectors/clear").json()["state"] == "idle"


def test_auto_does_not_reserve_cpu_during_cooldown_and_keeps_gpu_fifo():
    cpu, gpu = CpuGate(), GpuGate(25, clock=lambda: 0.0)
    gpu.next_background = 2.0
    automatic = CombinedGate(cpu, gpu)
    completed = []

    def waiters(count):
        deadline = time.monotonic() + 3
        while True:
            with gpu.condition:
                if len(gpu.waiters) == count:
                    return
            assert time.monotonic() < deadline
            time.sleep(0.001)

    def auto():
        with automatic, automatic:
            completed.append("auto")

    def explicit():
        with gpu:
            completed.append("gpu")

    def query():
        with cpu.slot(interactive=True):
            completed.append("cpu query")

    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(auto)
        try:
            waiters(1)
            second = executor.submit(explicit)
            waiters(2)
            executor.submit(query).result(timeout=1)
            assert completed == ["cpu query"]
        finally:
            gpu.configure(100)
        first.result(timeout=3)
        second.result(timeout=3)
    assert completed == ["cpu query", "auto", "gpu"]


def test_memory_limit_reloads_all_sessions_and_preserves_independent_devices(
    db, importer, monkeypatch
):
    from test_media import services

    semantic, media = services(db, importer)
    db.settings = replace(
        db.settings,
        e5_device="gpu",
        clip_search_device="gpu",
        ocr_engine="paddle",
        ocr_device="gpu",
    )

    def limits():
        return SimpleNamespace(memory_limit_mib=4096, threads=4)

    unloaded = []
    encoder = SimpleNamespace(
        space_id="unchanged",
        execution=limits(),
        query_execution=limits(),
        suspend=lambda: unloaded.append("text"),
        resume=lambda: None,
    )
    semantic.encoder = encoder
    media.clip = SimpleNamespace(
        execution=limits(), query_execution=limits(), unload=lambda: unloaded.append("images")
    )
    media.ocr = SimpleNamespace(unload=lambda: unloaded.append("ocr"))
    candidates = []

    def new_ocr(settings):
        candidates.append(settings)
        return SimpleNamespace(verify=lambda: None)

    monkeypatch.setattr(media, "_new_ocr", new_ocr)
    before = db.settings
    result = WorkspaceService(db, importer, semantic, media).update_settings(
        {"gpu_memory_limit_mib": 6144}
    )
    assert result["gpu_memory_limit_mib"] == 6144
    assert semantic.encoder is encoder and encoder.space_id == "unchanged"
    for model in (semantic.encoder, media.clip):
        assert model.execution.memory_limit_mib == model.query_execution.memory_limit_mib == 6144
    assert candidates[0].gpu_memory_limit_mib == 6144
    assert unloaded == ["text", "images", "ocr"]
    assert db.settings.model_device("e5") == before.model_device("e5") == "gpu"
    assert db.settings.model_device("clip", query=True) == "gpu"
    assert db.settings.ocr_device == before.ocr_device == "gpu"


def test_clear_job_status_and_reads_remain_available_while_mutations_are_fenced(db, monkeypatch):
    app = create_app(db.workspace)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-session-token": client.get("/api/session").json()["token"]}
        app.state.semantic.available = True
        fake = SimpleNamespace(
            table_path=lambda space: db.workspace / "absent", clear=lambda *args, **kwargs: 0
        )
        monkeypatch.setattr(app.state.semantic, "_vector_store", lambda: fake)
        app.state.media._set_active("clip", True)
        try:
            response = client.post(
                "/api/storage/vectors/images/clear", json={"confirm": True}, headers=headers
            )
            assert response.status_code == 200 and response.json()["state"] == "waiting"
            assert client.get("/api/settings").status_code == 200
            assert client.get("/api/storage/vectors/clear").json()["state"] == "waiting"
            assert (
                client.patch(
                    "/api/settings", json={"gpu_index_load_percent": 60}, headers=headers
                ).status_code
                == 409
            )
            app.state.media._set_active("clip", False)
            deadline = time.monotonic() + 5
            while client.get("/api/storage/vectors/clear").json()["state"] == "waiting":
                assert time.monotonic() < deadline
                time.sleep(0.01)
            app.state.vector_maintenance.worker.join(timeout=5)
            assert client.get("/api/storage/vectors/clear").json()["state"] == "done"
        finally:
            app.state.media._set_active("clip", False)
