"""Cancellation and memory recovery with synthetic children, without model downloads."""

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_media import services
from test_ocr_queue import photos

from telegram_search.inference.ocr_pool import CpuOcrPool
from telegram_search.inference.ocr_process import OcrProcess


class ChildEngine:
    version = "synthetic-v1"
    threads = 1

    def __init__(self, marker, completed_first=False):
        script = """
import sys, time
from pathlib import Path
marker, completed_first = sys.argv[1], sys.argv[2] == '1'
completed = 0
while header := sys.stdin.buffer.readline():
    sys.stdin.buffer.read(int(header))
    if completed_first and completed == 0:
        print('{"text":"finished","confidence":99}', flush=True)
        completed += 1
    else:
        Path(marker).touch()
        time.sleep(30)
"""
        self.worker = OcrProcess(
            [sys.executable, "-c", script, str(marker), str(int(completed_first))],
            threads=1,
            timeout=20,
        )

    def recognize(self, data, *, _generation=None):
        return json.loads(self.worker.recognize(data, generation=_generation))

    def unload(self):
        self.worker.unload()


def wait_markers(markers):
    deadline = time.monotonic() + 5
    while not all(marker.exists() for marker in markers):
        assert time.monotonic() < deadline, "Synthetic children did not start"
        time.sleep(0.01)


@pytest.mark.parametrize("completed_first", [False, True])
def test_pool_shutdown_preserves_finished_work_and_requeues_cancelled_images(
    db, importer, tmp_path, completed_first
):
    photos(importer, tmp_path / "source", 4)
    _, media = services(db, importer)
    db.settings = replace(db.settings, cpu_threads=2, ocr_cpu_workers=2, ocr_batch_size=4)
    markers = [tmp_path / f"lane-{i}" for i in range(2)]
    engines = [ChildEngine(marker, completed_first and i == 0) for i, marker in enumerate(markers)]
    media.ocr = CpuOcrPool(engines, 2)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(media._ocr_run)
            wait_markers(markers)
            media.stop.set()
            media.ocr.unload()
            assert pending.result(timeout=3)
        with db.connect() as conn:
            ready = conn.execute("SELECT COUNT(*) FROM ocr_cache WHERE state='ready'").fetchone()[0]
            failed = conn.execute("SELECT COUNT(*) FROM ocr_cache WHERE state='failed'").fetchone()[
                0
            ]
            pending = conn.execute(
                "SELECT COUNT(*) FROM ocr_work WHERE state='pending'"
            ).fetchone()[0]
        assert (ready, failed, pending) == (int(completed_first), 0, 4 - int(completed_first))
        assert not media.ocr_claims and not media.active_stages
        assert [engine.worker.starts for engine in engines] == [1, 1]
        assert all(engine.worker.process is None for engine in engines)
    finally:
        media.shutdown()


def test_single_shutdown_does_not_turn_interrupted_photo_into_failure(db, importer, tmp_path):
    photos(importer, tmp_path / "source", 2)
    _, media = services(db, importer)
    marker = tmp_path / "entered"
    media.ocr = ChildEngine(marker)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(media._ocr_run)
            wait_markers([marker])
            media.stop.set()
            media.ocr.unload()
            assert pending.result(timeout=3)
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM ocr_cache").fetchone()[0] == 0
            assert (
                conn.execute("SELECT COUNT(*) FROM ocr_work WHERE state='pending'").fetchone()[0]
                == 2
            )
        assert media.ocr.worker.starts == 1
    finally:
        media.shutdown()


def test_memory_throttle_can_release_idle_model_then_resume(db, importer):
    _, media = services(db, importer)
    released = threading.Event()
    media.ocr = SimpleNamespace(unload=lambda: released.set())
    media.resources = SimpleNamespace(
        snapshot=lambda: {
            "rss_bytes": 0 if released.is_set() else (db.settings.memory_limit_mib + 1) * 1024**2
        }
    )

    def operation():
        assert released.is_set()
        media.stop.set()
        return False

    media._stage_loop("ocr", operation)
    assert released.is_set() and media.resource_error is None


def test_clip_idle_eviction_preserves_recent_or_busy_query_session():
    pytest.importorskip("numpy")
    from telegram_search.inference.clip import ClipEncoder

    encoder = ClipEncoder.__new__(ClipEncoder)
    encoder.lock = threading.RLock()
    encoder.sessions = {"query": object(), "image": object()}
    encoder.last_used = time.monotonic()
    assert not encoder.unload_idle(idle_seconds=10)
    entered, release = threading.Event(), threading.Event()

    def query():
        with encoder.lock:
            entered.set()
            release.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(query)
        assert entered.wait(timeout=3)
        try:
            assert not encoder.unload_idle(idle_seconds=0)
        finally:
            release.set()
        future.result(timeout=3)
    assert encoder.unload_idle(idle_seconds=0) and not encoder.sessions
