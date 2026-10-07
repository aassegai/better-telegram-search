import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from telegram_search.inference.ocr_process import OcrProcess
from telegram_search.shared.errors import UserError

CHILD = """
import json, os, sys, time
while header := sys.stdin.buffer.readline():
    data = sys.stdin.buffer.read(int(header))
    if data == b'hang':
        time.sleep(30)
    if data == b'crash':
        os._exit(1)
    if data == b'oversized':
        sys.stdout.write('x' * (1024 * 1024 + 2))
        sys.stdout.flush()
        continue
    print(json.dumps({'text': data.decode(), 'confidence': 99}), flush=True)
"""


def worker(**kwargs):
    return OcrProcess([sys.executable, "-u", "-c", CHILD], threads=1, timeout=10, **kwargs)


def test_worker_reuses_process_preserves_frame_boundaries_and_recycles():
    process = worker(recycle_after=2)
    try:
        assert json.loads(process.recognize(b"first\nimage"))["text"] == "first\nimage"
        first = process.process
        assert json.loads(process.recognize(b"second image"))["text"] == "second image"
        assert process.process is first
        assert json.loads(process.recognize(b"third image"))["text"] == "third image"
        assert process.process is not first and first.poll() is not None
        last = process.process
    finally:
        process.unload()
    assert last.poll() is not None and process.process is None


@pytest.mark.parametrize("data", [b"hang", b"crash", b"oversized"])
def test_timeout_crash_and_response_budget_stop_child_and_allow_next_image(data):
    process = worker()
    try:
        process.recognize(b"warmup")
        first = process.process
        if data == b"hang":
            process.timeout = 0.05
        with pytest.raises(UserError):
            process.recognize(data)
        assert first.poll() is not None and process.process is None
        process.timeout = 10
        assert json.loads(process.recognize(b"recovered"))["text"] == "recovered"
    finally:
        process.unload()


def test_empty_and_oversized_input_do_not_start_child():
    process = worker()
    for data in (b"", b"x" * (32 * 1024**2 + 1)):
        with pytest.raises(UserError):
            process.recognize(data)
        assert process.process is None


def test_shutdown_interrupts_a_request_without_waiting_for_request_mutex(tmp_path):
    marker = tmp_path / "entered"
    child = """
import pathlib,sys,time
header=sys.stdin.buffer.readline()
sys.stdin.buffer.read(int(header))
pathlib.Path(sys.argv[1]).touch()
time.sleep(30)
"""
    process = OcrProcess([sys.executable, "-u", "-c", child, str(marker)], threads=1, timeout=20)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(process.recognize, b"image")
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        began = time.monotonic()
        process.unload()
        with pytest.raises(UserError):
            future.result(timeout=3)
        assert time.monotonic() - began < 3 and process.process is None


def test_memory_recycling_keeps_stable_child_beyond_old_128_image_interval():
    process = worker()
    try:
        for _ in range(130):
            process.recognize(b"synthetic")
        assert process.starts == 1 and process.completed == 130
    finally:
        process.unload()


@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
def test_readiness_handshake_accepts_unix_and_windows_newlines(ending):
    ready = b'{"ready":true}' + ending
    script = f"import sys; sys.stdout.buffer.write({ready!r}); sys.stdout.flush()\n" + CHILD
    process = OcrProcess(
        [sys.executable, "-u", "-c", script], threads=1, timeout=10, ready_handshake=True
    )
    try:
        assert json.loads(process.recognize(b"first"))["text"] == "first"
        assert process.last_stats["cold_start_seconds"] > 0
        assert json.loads(process.recognize(b"second"))["text"] == "second"
        assert process.last_stats["cold_start_seconds"] == 0 and process.starts == 1
    finally:
        process.unload()
