"""Periodic, bounded file maintenance; independent from SQLite and GPU models."""

import threading
import time
from dataclasses import asdict, is_dataclass

from telegram_search.search.ann_process import AnnProcess
from telegram_search.search.execution import SearchCancelled, SearchExecution, execution_context
from telegram_search.shared.errors import UserError


class VectorOptimization:
    reader_tag = "bts-maintenance-reader-v1"
    check_seconds = 60
    check_writes = 64
    small_fragments = 128
    retry_seconds = 300

    def __init__(self):
        self.lock = threading.RLock()
        self.process = None
        self.execution = None
        self.closed = False
        self.writes = {}
        self.checked = {}
        self.compacted_writes = {}
        self.retry_at = {}
        self.info = {"state": "idle", "error": None}

    def status(self):
        with self.lock:
            return dict(self.info)

    def changed(self, space):
        with self.lock:
            self.writes[space] = self.writes.get(space, 0) + 1

    def writer_ready(self):
        # A failed kill is not proof that the native writer has stopped.
        with self.lock:
            if self.process is not None:
                if not self.process.close():
                    raise UserError("Оптимизация индекса останавливается. Повторите позже.")
                self.process = None

    @property
    def writer_active(self):
        with self.lock:
            return self.process is not None

    def close(self, *, wait=False):
        with self.lock:
            self.closed = True
            if self.execution is not None:
                self.execution.cancelled.set()
        while True:
            with self.lock:
                child = self.process
            if child is None:
                return
            if child.close():
                with self.lock:
                    if self.process is child:
                        self.process = None
                return
            if not wait:
                return
            time.sleep(0.1)

    def run(self, store, spaces, *, threads, memory_limit_mib):
        """Caller holds the writer gate, never reader/lifecycle/SQLite locks."""
        self.writer_ready()
        for space in spaces:
            key = space["id"]
            tagged = None
            with self.lock:
                if self.closed:
                    return False
                now = time.monotonic()
                writes = self.writes.get(key, 0)
                previous = self.checked.get(key)
                if now < self.retry_at.get(key, 0) or (
                    previous
                    and now - previous[0] < self.check_seconds
                    and writes - previous[1] < self.check_writes
                ):
                    continue
                self.checked[key] = (now, writes)
            try:
                with store.lock:
                    table = store.table(key, space["dimension"])
                    if table is None:
                        continue
                    # Recover our single pin after a previous process exit.
                    # The writer is stopped and all prior app readers drained.
                    if self.reader_tag in table.tags.list():
                        table.tags.delete(self.reader_tag)
                    stats = table.stats() if hasattr(table, "stats") else None
                    stats = asdict(stats) if is_dataclass(stats) else stats
                    fragments = (stats or {}).get("fragment_stats", {})
                    small = fragments.get("num_small_fragments")
                    if small is not None and small < self.small_fragments:
                        continue
                    if (
                        small is None
                        and previous
                        and writes - self.compacted_writes.get(key, 0) < self.small_fragments
                    ):
                        continue
                    store._ann_for_maintenance(key)
                    # The old latest may be years old even for a newly started
                    # reader. TTL alone cannot retain it during native GC.
                    # All earlier app readers have drained under store.lock;
                    # concurrent readers will use this pinned version or newer.
                    tags = table.tags
                    if self.reader_tag in tags.list():
                        tags.update(self.reader_tag, table.version)
                    else:
                        tags.create(self.reader_tag, table.version)
                    tagged = table
                    with self.lock:
                        if self.closed:
                            return False
                        child = AnnProcess(
                            threads=threads,
                            memory_limit_mib=memory_limit_mib,
                            timeout=120,
                            background=True,
                        )
                        self.process = child
                        execution = SearchExecution()
                        self.execution = execution
                        self.info = {"state": "running", "error": None, "small_fragments": small}
                began = time.monotonic()
                with execution_context(execution):
                    outcome = child.optimize(location=store.location, table="text_" + key) or {}
                with self.lock:
                    self.info = {
                        "state": "done",
                        "error": None,
                        "duration_seconds": round(time.monotonic() - began, 3),
                        "small_fragments_before": small,
                        "cleanup_deferred": outcome.get("cleanup_deferred", False),
                    }
                    self.compacted_writes[key] = writes
                store._ann_dirty(key, space["dimension"])
                return True  # At most one table before returning to indexing.
            except SearchCancelled:
                return False
            except Exception:
                with self.lock:
                    self.retry_at[key] = time.monotonic() + self.retry_seconds
                    self.info = {
                        "state": "deferred",
                        "error": "Оптимизация отложена; повторим автоматически.",
                    }
            finally:
                self.writer_ready()
                if tagged is not None:
                    # New readers of the pre-compaction version have drained;
                    # the child is proven stopped before releasing its pin.
                    with store.lock:
                        try:
                            tagged.tags.delete(self.reader_tag)
                        except Exception:
                            with self.lock:
                                self.retry_at[key] = time.monotonic() + self.retry_seconds
                                self.info = {
                                    "state": "deferred",
                                    "error": "Оптимизация отложена; повторим автоматически.",
                                }
                with self.lock:
                    self.execution = None
        return False
