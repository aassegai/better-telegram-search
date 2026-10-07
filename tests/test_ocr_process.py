import json
import sys

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
