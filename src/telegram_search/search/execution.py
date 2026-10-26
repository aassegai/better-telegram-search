"""Cooperative cancellation and measured progress, with no search payload logging."""

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from itertools import islice

_execution = ContextVar("search_execution", default=None)
COUNTERS = (
    "scope_messages",
    "eligible_vectors",
    "vectors_read",
    "vectors_scored",
    "candidates_before_rerank",
    "returned",
)


class SearchCancelled(BaseException):
    """Like asyncio cancellation, do not convert a stop into a model fallback."""


class SearchExecution:
    def __init__(self):
        self.cancelled = threading.Event()
        self.lock = threading.RLock()
        self.started = time.monotonic()
        self.phase_started = self.started
        self.phase = "queued"
        self.completed = 0
        self.total = None
        self.unit = "items"
        self.sequence = 0
        self.metrics = dict.fromkeys(COUNTERS, 0)
        self.timings = {}
        self.requested_strategy = "exact"
        self.effective_strategy = "exact"
        self.fallback_reasons = []
        self.finished = None

    def check(self):
        if self.cancelled.is_set():
            raise SearchCancelled()

    def begin(self, phase, total=None, unit="items"):
        self.check()
        with self.lock:
            now = time.monotonic()
            if self.phase != "queued":
                self.timings[self.phase] = (
                    self.timings.get(self.phase, 0) + now - self.phase_started
                )
            self.phase, self.total, self.unit = phase, total, unit
            self.completed, self.phase_started = 0, now
            self.sequence += 1

    def advance(self, count=1):
        self.check()
        with self.lock:
            self.completed += count
            if self.total is not None:
                self.completed = min(self.completed, self.total)
            self.sequence += 1

    def count(self, name, count):
        with self.lock:
            self.metrics[name] += count

    def snapshot(self):
        with self.lock:
            now = self.finished or time.monotonic()
            elapsed = now - self.phase_started
            rate = self.completed / elapsed if self.completed >= 3 and elapsed >= 0.5 else None
            timings = dict(self.timings)
            timings[self.phase] = timings.get(self.phase, 0) + elapsed
            return {
                "phase": self.phase,
                "completed": self.completed,
                "total": self.total,
                "unit": self.unit,
                "sequence": self.sequence,
                "elapsed_seconds": round(now - self.started, 3),
                "phase_elapsed_seconds": round(elapsed, 3),
                "rate": rate,
                "eta_seconds": (self.total - self.completed) / rate
                if rate and self.total is not None
                else None,
                "requested_strategy": self.requested_strategy,
                "effective_strategy": self.effective_strategy,
                "fallback_reasons": list(self.fallback_reasons),
                "diagnostics": {**self.metrics, "phase_seconds": timings},
            }

    def finish(self):
        with self.lock:
            self.finished = time.monotonic()


def current_execution():
    return _execution.get()


@contextmanager
def execution_context(execution):
    token = _execution.set(execution)
    try:
        execution.check()
        yield execution
    finally:
        _execution.reset(token)


def checkpoint():
    execution = current_execution()
    if execution:
        execution.check()


def phase(name, total=None, unit="items"):
    execution = current_execution()
    if execution:
        execution.begin(name, total, unit)


def advance(count=1):
    execution = current_execution()
    if execution:
        execution.advance(count)


def count(name, value):
    execution = current_execution()
    if execution:
        execution.count(name, value)


def id_batches(rows, size=512):
    """Stream a canonical SQL cursor into bounded vector predicates."""
    rows = iter(rows)
    while batch := list(islice(rows, size)):
        checkpoint()
        yield [row[0] for row in batch]


@contextmanager
def search_lock(lock):
    """A cancelled reader must not wait behind an unrelated long indexing job."""
    if current_execution() is None:
        with lock:
            yield
        return
    while not lock.acquire(timeout=0.05):
        checkpoint()
    try:
        checkpoint()
        yield
    finally:
        lock.release()
