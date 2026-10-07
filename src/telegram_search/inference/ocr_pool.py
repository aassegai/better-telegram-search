"""Optional CPU parallelism inside one scheduler reservation and one thread budget."""

from concurrent.futures import ThreadPoolExecutor
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

    def recognize(self, data):
        return self.engines[0].recognize(data)

    def recognize_many(self, images):
        with self.lock:
            generation = self.generation

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
        with ThreadPoolExecutor(
            max_workers=len(self.engines), thread_name_prefix="ocr-cpu"
        ) as pool:
            for values in pool.map(lane, range(len(self.engines))):
                for position, value in values:
                    results[position] = value
        return results

    def unload_worker(self):
        with self.lock:
            self.generation += 1
            for engine in self.engines:
                engine.unload()

    def unload(self):
        self.unload_worker()
