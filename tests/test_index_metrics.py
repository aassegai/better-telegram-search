from telegram_search.indexing.metrics import StageMetrics


def test_parallel_throughput_idle_exclusion_bounded_window_and_identity():
    metrics = StageMetrics(window=3)
    assert metrics.snapshot("ocr", "cpu", 100)["measuring"]
    metrics.record("ocr", "cpu", 0, 2, phases={"regions": 4, "text": "never retained"})
    metrics.record("ocr", "cpu", 0, 2, errors=1, phases={"regions": 2})
    status = metrics.snapshot("ocr", "cpu", 100)
    assert status["units_per_minute"] == 60 and status["estimated_remaining_seconds"] == 100
    assert status["regions_per_second"] == 3 and "text" not in status["phase_means"]
    metrics.record("ocr", "cpu", 50, 52)
    assert metrics.snapshot("ocr", "cpu", 1)["units_per_minute"] == 45
    metrics.record("ocr", "cpu", 53, 55)
    assert metrics.snapshot("ocr", "cpu", 1)["samples"] == 3
    assert metrics.snapshot("ocr", "gpu", 1)["estimated_remaining_seconds"] is None
    metrics.record("ocr", "gpu", 60, 61)
    assert metrics.snapshot("ocr", "gpu", 1)["samples"] == 1
