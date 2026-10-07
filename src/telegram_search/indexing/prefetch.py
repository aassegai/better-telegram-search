"""Exactly one lookahead batch. Never holds a model/device reservation."""

from concurrent.futures import ThreadPoolExecutor


class BatchPrefetch:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="index-prepare")
        self.pending = None

    def take(self, key, build):
        if self.pending is not None:
            previous, future = self.pending
            self.pending = None
            if key == previous:
                return future.result()
            # Batch reduction/pause/settings changes discard stale preparation.
            if not future.cancel():
                try:
                    future.result()
                except Exception:
                    pass
        return build()

    def submit(self, key, build):
        if self.pending is not None:
            raise RuntimeError("Only one prepared batch may be queued")
        self.pending = (key, self.pool.submit(build))

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.pending = None
