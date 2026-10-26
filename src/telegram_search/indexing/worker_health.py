"""Small process-local health state readable even when SQLite is unavailable."""

import threading
import time


class WorkerHealth:
    def __init__(self):
        self.lock = threading.Lock()
        self.phase = "not_started"
        self.updated_at = time.monotonic()
        self.busy_since = None
        self.retry_at = 0
        self.error = None

    def update(self, phase, *, error=None, delay=0, keep_wait=False):
        with self.lock:
            self.phase, self.error = phase, error
            self.updated_at = now = time.monotonic()
            if phase == "waiting_database":
                self.busy_since = self.busy_since if self.busy_since is not None else now
            elif not keep_wait:
                self.busy_since = None
            self.retry_at = now + delay

    def snapshot(self, *, enabled, thread, stopped):
        with self.lock:
            now = time.monotonic()
            return {
                "enabled": enabled,
                "alive": bool(thread and thread.is_alive()),
                "stopped": stopped,
                "phase": self.phase,
                "waiting_reason": "database" if self.busy_since is not None else None,
                "database_wait_seconds": round(now - self.busy_since, 3)
                if self.busy_since is not None
                else 0,
                "retry_in_seconds": round(max(0, self.retry_at - now), 3),
                "heartbeat_age_seconds": round(now - self.updated_at, 3),
                "error": self.error,
            }
