import threading
from types import SimpleNamespace

import pytest

from telegram_search.config.settings import Settings
from telegram_search.inference.ocr_pool import CpuOcrPool
from telegram_search.shared.errors import UserError


def test_cpu_lanes_overlap_return_original_order_and_isolate_failures():
    entered = threading.Barrier(2)

    def recognize(data):
        if data in (b"first", b"second"):
            entered.wait(timeout=5)
        if data == b"bad":
            raise UserError("synthetic")
        return {"text": data.decode(), "confidence": 90}

    engines = [SimpleNamespace(recognize=recognize, version="same", threads=1) for _ in range(2)]
    pool = CpuOcrPool(engines, 2)
    values = pool.recognize_many([b"first", b"second", b"bad", b"last"])
    assert [value.get("text") for value in values] == ["first", "second", None, "last"]
    assert pool.version == "same" and pool.threads == sum(engine.threads for engine in engines)


def test_cpu_pool_keeps_each_child_cold_start_statistics():
    engines = [
        SimpleNamespace(
            recognize=lambda data, **kwargs: {"text": "ready", "confidence": 90},
            worker=SimpleNamespace(generation=0, last_stats={"cold_start": 1}),
            last_timings={"regions": 2},
            version="same",
            threads=1,
        )
        for _ in range(2)
    ]
    values = CpuOcrPool(engines, 2).recognize_many([b"first", b"second"])
    assert sum(value["timings"]["cold_start"] for value in values) == 2
    assert sum(value["timings"]["regions"] for value in values) == 4


@pytest.mark.parametrize("threads,workers,device", [(1, 2, "cpu"), (4, 5, "cpu"), (4, 2, "gpu")])
def test_worker_settings_cannot_exceed_cpu_budget_or_spawn_competing_gpu_jobs(
    threads, workers, device
):
    with pytest.raises(UserError):
        Settings(
            cpu_threads=threads, ocr_cpu_workers=workers, ocr_engine="paddle", ocr_device=device
        ).validate()
