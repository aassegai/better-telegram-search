"""Observable scheduling order: interactive work first, FIFO background stages."""

import time
from concurrent.futures import ThreadPoolExecutor

from telegram_search.inference.resources import CpuGate


def test_interactive_search_precedes_fifo_background_stages_and_gate_is_reentrant():
    gate = CpuGate()
    completed = []

    def run(name, interactive=False):
        with gate.slot(interactive=interactive):
            with gate:  # Reentrant calls inside one stage must not rejoin the queue.
                completed.append(name)

    def wait_queue(size):
        deadline = time.monotonic() + 3
        while True:
            with gate.condition:
                if len(gate.waiters) == size:
                    return
            assert time.monotonic() < deadline
            time.sleep(0.001)

    with ThreadPoolExecutor(max_workers=4) as executor:
        with gate:
            futures = []
            for index, stage in enumerate(("ocr", "clip", "ocr_dense"), 1):
                futures.append(executor.submit(run, stage))
                wait_queue(index)
            futures.append(executor.submit(run, "search", True))
            wait_queue(4)
        for future in futures:
            future.result(timeout=3)
    assert completed == ["search", "ocr", "clip", "ocr_dense"]
    assert gate.owner is None and not gate.waiters and not gate.interactive_waiters
