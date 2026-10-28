"""Optional CPU parallelism inside one scheduler reservation and one thread budget."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import RLock

from telegram_search.shared.errors import UserError


class CpuOcrPool:
    def __init__(self, engines, threads):
        self.engines = engines
        self.threads = threads
        self.batch_limit = 4
        self.last_timings = {}
        self.generation = 0
        self.lock = RLock()

    def __getattr__(self, name):
        return getattr(self.engines[0], name)

    def backend_info(self):
        first = self.engines[0]
        info = (
            first.backend_info()
            if hasattr(first, "backend_info")
            else {
                "device": "cpu",
                "provider": "tesseract",
            }
        )
        return {**info, "cpu_workers": len(self.engines), "cpu_thread_budget": self.threads}

    def recognize(self, data, *, _generation=None):
        if getattr(self.engines[0], "accepts_generation", False):
            return self.recognize_many([data], _generation=_generation)[0]
        return self.engines[0].recognize(data)

    def recognize_many(self, images, *, _generation=None):
        with self.lock:
            generation = self.generation
            if _generation is not None and generation != _generation:
                return [{"cancelled": True} for _ in images]

        # Each lane receives a disjoint subset. Per-image timeout/kill is retained.
        def lane(index):
            values = []
            for position in range(index, len(images), len(self.engines)):
                engine = self.engines[index]
                with self.lock:
                    if generation != self.generation:
                        values.append((position, {"cancelled": True}))
                        continue
                    worker = getattr(engine, "worker", None)
                    kwargs = {"_generation": worker.generation} if worker else {}
                    if getattr(engine, "accepts_generation", False):
                        kwargs = {"_generation": engine.generation, "_reserved": True}
                try:
                    value = engine.recognize(images[position], **kwargs)
                    timings = {
                        **getattr(engine, "last_timings", {}),
                        **getattr(worker, "last_stats", {}),
                    }
                    values.append((position, {**value, "timings": timings}))
                except UserError:
                    with self.lock:
                        cancelled = generation != self.generation
                    values.append((position, {"cancelled": True} if cancelled else {"error": True}))
            return values

        results = [None] * len(images)
        slot = getattr(self.engines[0], "compute_slot", None)
        reservation = slot() if slot else nullcontext()
        try:
            with (
                reservation,
                ThreadPoolExecutor(
                    max_workers=len(self.engines), thread_name_prefix="ocr-cpu"
                ) as pool,
            ):
                for values in pool.map(lane, range(len(self.engines))):
                    for position, value in values:
                        results[position] = value
        except InterruptedError:
            stop = getattr(self.engines[0], "cancelled", None)
            if generation != self.generation or stop and stop.is_set():
                return [{"cancelled": True} for _ in images]
            raise
        return results

    def unload_worker(self):
        with self.lock:
            self.generation += 1
            errors = []
            for engine in self.engines:
                try:
                    engine.unload()
                except BaseException as exc:
                    errors.append(exc)
            if errors:
                raise errors[0]

    def retire(self):
        with self.lock:
            self.generation += 1
            self.retirement_error = None
            for engine in self.engines:
                try:
                    if hasattr(engine, "retire"):
                        engine.retire()
                        if getattr(engine, "retirement_error", None):
                            self.retirement_error = "Previous OCR worker cleanup is incomplete."
                    else:
                        engine.unload()
                except (UserError, InterruptedError):
                    self.retirement_error = "Previous OCR worker cleanup is incomplete."

    def unload(self):
        self.unload_worker()
