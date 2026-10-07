"""Synthetic queues only: prove two lanes overlap and never claim the same image."""

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import load
from PIL import Image
from test_media import photo_export, services
from test_model_devices import ExecutionStub

from telegram_search.indexing.chats import ChatIndexing
from telegram_search.inference.providers import CPU, CUDA
from telegram_search.shared.errors import UserError
from telegram_search.sources.service import WorkspaceService


def fixture_queue(db, importer, tmp_path, count=2):
    chats = []
    for i, color in enumerate(("red", "blue", "green")[:count]):
        stream = io.BytesIO()
        Image.new("RGB", (64, 32), color).save(stream, format="PNG")
        chats.append(
            load(
                importer,
                photo_export(tmp_path / color, chat_id=100 + i, photo_data=stream.getvalue()),
            )["chat_id"]
        )
    semantic, media = services(db, importer)
    entered = threading.Barrier(3)
    release = threading.Event()
    calls = []

    def recognize(lane, data):
        calls.append((lane, data))
        entered.wait(timeout=10)
        assert release.wait(10)
        return {"text": "синтетический текст", "confidence": 90}

    def engine(lane, provider):
        return SimpleNamespace(
            version="shared-ocr",
            execution=SimpleNamespace(
                provider=provider, info=lambda: {"device": lane, "provider": provider}
            ),
            recognize=lambda data: recognize(lane, data),
            unload=lambda: None,
        )

    media.ocr = engine("gpu", CUDA)
    media.ocr.cpu_peer = engine("cpu", CPU)
    return semantic, media, chats, entered, release, calls


@pytest.mark.parametrize("mutation", [None, "pause", "replace", "delete"])
def test_hybrid_ocr_overlaps_without_duplicate_claims_and_guards_publication(
    db, importer, tmp_path, mutation
):
    semantic, media, chats, entered, release, calls = fixture_queue(db, importer, tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        gpu = pool.submit(media._ocr_one)
        cpu = pool.submit(media._ocr_one, media.ocr.cpu_peer)
        try:
            entered.wait(timeout=10)  # Fails if CPU and GPU are serialized by one gate.
            assert len(media.ocr_claims) == 2
            assert len({data for _, data in calls}) == 2
            if mutation == "pause":
                for chat in chats:
                    ChatIndexing(db, semantic, media, importer.lifecycle_lock).control(
                        chat, "ocr", "pause"
                    )
            elif mutation == "replace":
                media.ocr = SimpleNamespace(version="other-ocr")
            elif mutation == "delete":
                with importer.lifecycle_lock, db.connect() as conn:
                    conn.execute("DELETE FROM chats")
        finally:
            release.set()
        assert gpu.result(timeout=10) and cpu.result(timeout=10)
    assert not media.ocr_claims
    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM ocr_cache").fetchone()[0]
    assert count == (2 if mutation is None else 0)
    assert media.ocr_completed == count
    if mutation is None:
        assert not media._ocr_one() and not media._ocr_one(media.ocr.cpu_peer)
        assert len(calls) == 2
        assert set(media.ocr_timings) == {
            "read_seconds",
            "compute_wait_seconds",
            "inference_seconds",
            "publish_seconds",
            "pipeline",
        }


def test_claim_is_released_after_unexpected_inference_failure(db, importer, tmp_path):
    _, media, _, _, _, _ = fixture_queue(db, importer, tmp_path, count=1)
    media.ocr.recognize = lambda data: (_ for _ in ()).throw(RuntimeError("synthetic failure"))
    with pytest.raises(RuntimeError):
        media._ocr_one()
    assert not media.ocr_claims
    media.ocr.cpu_peer.recognize = lambda data: {"text": "recovered", "confidence": 90}
    assert media._ocr_one(media.ocr.cpu_peer)
    assert media.ocr_completed == 1


def test_hybrid_device_applies_only_to_ocr_and_preserves_cache(db, importer, tmp_path, monkeypatch):
    from test_media import fake_ocr

    load(importer, photo_export(tmp_path / "source"))
    semantic, media = services(db, importer)
    fake_ocr(media)
    media.ocr.unload = lambda: None
    assert media._ocr_one()
    db.settings = replace(db.settings, ocr_engine="paddle")
    monkeypatch.setattr("telegram_search.inference.providers.Execution", ExecutionStub)
    modes = []

    def candidate(settings):
        modes.append(settings.ocr_device)
        return SimpleNamespace(
            version=media.ocr.version,
            verify=lambda: None,
            check_contract=lambda: None,
            unload=lambda: None,
        )

    monkeypatch.setattr(media, "_new_ocr", candidate)
    workspace = WorkspaceService(db, importer, semantic, media)
    with db.connect() as conn:
        before = [tuple(row) for row in conn.execute("SELECT * FROM ocr_cache")]
    result = workspace.change_device("hybrid", model="ocr")
    assert result["model"]["device"] == "hybrid" and modes == ["hybrid"]
    with db.connect() as conn:
        assert before == [tuple(row) for row in conn.execute("SELECT * FROM ocr_cache")]
    for model in ("e5", "clip", None):
        with pytest.raises(UserError):
            workspace.change_device("hybrid", model=model)
    media.ocr_cpu_running = True
    with pytest.raises(UserError, match="Приостановите"):
        workspace.change_device("cpu", model="ocr")


def test_background_cpu_lane_starts_and_shutdown_joins_both_workers(db, importer, tmp_path):
    _, media, _, _, _, _ = fixture_queue(db, importer, tmp_path, count=1)
    done = threading.Event()
    media.ocr.recognize = lambda data: {"text": "gpu", "confidence": 90}
    media.ocr.cpu_peer.recognize = lambda data: {"text": "cpu", "confidence": 90}
    original = media._ocr_one

    def finish(engine=None):
        result = original(engine)
        if result:
            done.set()
        return result

    media._ocr_one = finish
    media.start_background()
    try:
        assert done.wait(10)
    finally:
        media.shutdown()
    assert not media.background.is_alive() and not media.ocr_background.is_alive()
    assert not media.ocr_claims


def test_failed_commit_does_not_advance_completed_counter_or_keep_claim(
    db, importer, tmp_path, monkeypatch
):
    from contextlib import contextmanager

    _, media, _, _, _, _ = fixture_queue(db, importer, tmp_path, count=1)
    media.ocr.recognize = lambda data: {"text": "synthetic", "confidence": 90}
    original = db.connect

    @contextmanager
    def fail_commit():
        with original() as conn:
            yield conn
            if conn.execute("SELECT 1 FROM ocr_cache").fetchone():
                raise RuntimeError("synthetic commit failure")

    monkeypatch.setattr(db, "connect", fail_commit)
    with pytest.raises(RuntimeError, match="commit failure"):
        media._ocr_one()
    monkeypatch.setattr(db, "connect", original)
    with db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM ocr_cache").fetchone()
    assert media.ocr_completed == 0 and not media.ocr_claims


def test_replaced_owner_cannot_leave_restarted_child_alive(db, importer, tmp_path, monkeypatch):
    _, media, _, _, _, _ = fixture_queue(db, importer, tmp_path, count=1)
    old = media.ocr
    live = []
    old.unload_worker = lambda: live.clear()

    def recognize(data):
        # Simulate prepare swapping the owner after admission but before old
        # recognize launches its persistent child.
        media.ocr = SimpleNamespace(version="replacement")
        old.unload_worker()
        live.append("restarted child")
        return {"text": "discarded", "confidence": 90}

    old.recognize = recognize
    assert media._ocr_one()
    assert not live and not media.ocr_claims and media.ocr_completed == 0


def test_hybrid_requires_two_cpu_threads():
    from telegram_search.config.settings import Settings

    with pytest.raises(UserError, match="не менее двух"):
        Settings(ocr_engine="paddle", ocr_device="hybrid", cpu_threads=1).validate()
