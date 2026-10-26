"""One CPU-bounded native ANN builder; metadata/status never require a SQL writer."""

import secrets
import threading
import time
from types import SimpleNamespace

from telegram_search.search.ann import AnnOptions
from telegram_search.search.ann_process import AnnProcess, AnnUnavailable
from telegram_search.search.execution import SearchCancelled, SearchExecution, execution_context
from telegram_search.shared.errors import UserError


class AnnIndexService:
    def __init__(self, db, semantic, media, lifecycle_lock, *, start_background=True):
        self.db, self.semantic, self.media = db, semantic, media
        self.lifecycle = lifecycle_lock
        self.condition = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.entries = {}
        self.forced = set()
        self.force_all = False
        self.failed = set()
        self.suppressed = False
        self.control_generation = 0
        self.current = None
        self.thread = None
        self.initializing = True
        self.error = None
        semantic.ann_service = self
        if semantic.vectors is not None:
            semantic.vectors.ann_observer = self
        if start_background:
            self.start()

    @property
    def available(self):
        return self.semantic.available

    def start(self):
        with self.condition:
            if self.thread is None and not self.stop.is_set():
                self.thread = threading.Thread(target=self._loop, name="ann-index", daemon=True)
                self.thread.start()

    def dirty(self, space_id, dimension):
        self.wake.set()

    def status(self):
        with self.condition:
            now = time.monotonic()
            entries = []
            for entry in self.entries.values():
                value = dict(entry)
                if entry["space_id"] in self.forced and (
                    self.current is None or self.current.space != entry["space_id"]
                ):
                    value.update(state="queued", error=None)
                value["elapsed_seconds"] = round(
                    now - self.current.started
                    if self.current and self.current.space == entry["space_id"]
                    else value.get("duration_seconds", 0),
                    3,
                )
                value["total"] = None
                entries.append(value)
            settings = self.db.settings
            return {
                "available": self.available,
                "enabled": settings.ann_indexing_enabled,
                "initializing": self.initializing,
                "running": self.current.id if self.current else None,
                "error": self.error,
                "entries": entries,
                "resources": {
                    "threads": min(settings.ann_build_threads, settings.cpu_threads),
                    "memory_limit_mib": min(
                        settings.ann_memory_limit_mib, settings.memory_limit_mib
                    ),
                },
            }

    def prepare(self, space_id=None):
        with self.condition:
            if self.stop.is_set() or not self.available:
                raise UserError("Среда векторного поиска недоступна.")
            if space_id is not None and space_id not in self.entries:
                raise UserError("Пространство векторного поиска не найдено.")
            self.suppressed = False
            keys = set(self.entries) if space_id is None else {space_id}
            if not self.entries and space_id is None:
                self.force_all = True
            for key in keys:
                self.failed.discard(key)
                if not self.current or self.current.space != key:
                    self.forced.add(key)
            self.wake.set()
        return self.status()

    def _stop_process(self, job):
        try:
            stopped = bool(job.process.close())
        except Exception:
            stopped = False
        if not stopped:
            with self.condition:
                entry = self.entries.get(job.space)
                if self.current is job and entry:
                    entry.update(state="cancelling", error="ann_stop_failed")
        return stopped

    def _interrupt(self, *, space=None, state="cancelled", requeue=False):
        with self.condition:
            self.control_generation += 1
            job = self.current
            if job is None or (space is not None and space != job.space):
                return True
            job.invalidated = True
            job.cancel_state = state
            job.execution.cancelled.set()
            if requeue:
                self.forced.add(job.space)
            entry = self.entries.get(job.space)
            if entry:
                entry.update(state="cancelling", error=None)
                if state == "checking":
                    entry["ready"] = False
        # No join on the scheduler while a lifecycle/vector lock is held.
        stopped = self._stop_process(job)
        self.wake.set()
        return stopped

    def invalidate(self, space_id):
        with self.condition:
            self.forced.discard(space_id)
            self.failed.discard(space_id)
            if space_id in self.entries:
                self.entries[space_id].update(state="checking", ready=False)
        if not self._interrupt(space=space_id, state="checking"):
            raise UserError("Индекс ANN останавливается. Повторите очистку через несколько секунд.")
        self.wake.set()

    def models_changed(self, previous_space=None):
        if previous_space is not None:
            self._interrupt(space=previous_space, state="checking")
        else:
            with self.condition:
                self.control_generation += 1
        self.wake.set()

    def cancel(self):
        with self.condition:
            self.suppressed = True
            self.forced.clear()
            self.force_all = False
        self._interrupt()
        return self.status()

    def settings_changed(self, keys):
        if not set(keys) & {
            "ann_indexing_enabled",
            "ann_build_threads",
            "ann_memory_limit_mib",
            "ann_partitions",
            "ann_probes",
            "ann_rebuild_percent",
            "cpu_threads",
            "memory_limit_mib",
        }:
            return
        if "ann_indexing_enabled" in keys and not self.db.settings.ann_indexing_enabled:
            self.cancel()
            return
        with self.condition:
            self.suppressed = False
            self.failed.clear()
        if set(keys) & {
            "ann_build_threads",
            "ann_memory_limit_mib",
            "ann_partitions",
            "cpu_threads",
            "memory_limit_mib",
        }:
            self._interrupt(state="checking", requeue=True)
        self.wake.set()

    def _active(self):
        if not self.lifecycle.acquire(timeout=0.1):
            return None
        try:
            spaces = {}
            encoder, visual = self.semantic.encoder, self.media.clip
            if encoder:
                spaces[encoder.space_id] = (encoder.spec.dimension, ["text", "ocr"])
            if visual:
                spaces[visual.space_id] = (getattr(visual, "dimension", 512), ["images"])
            store = self.semantic._vector_store() if spaces else None
            return store, spaces
        finally:
            self.lifecycle.release()

    def _step(self):
        with self.condition:
            if self.current is not None:
                return  # An unresolved writer lease never permits a second builder.
            generation = self.control_generation
        active = self._active()
        if active is None:
            return
        store, spaces = active
        settings = self.db.settings
        with self.condition:
            self.initializing = False
            self.entries = {key: entry for key, entry in self.entries.items() if key in spaces}
            self.forced.intersection_update(spaces)
            self.failed.intersection_update(spaces)
            if self.force_all:
                self.forced.update(spaces)
                self.force_all = False
        if store is None:
            return
        options = AnnOptions(partitions=settings.ann_partitions, probes=settings.ann_probes)
        for space, (dimension, roles) in spaces.items():
            with store.lock:
                table = store.table(space, dimension)
                info = store.ann_status(space, dimension)
                rows = table.count_rows() if table is not None else 0
                if table is not None:
                    store._validate_table(table, dimension)
                # Partition capacity is based on the last training population.
                # A few fresh rows must not cause a rebuild every 32 appends.
                desired = min(options.partitions, max(1, info.get("indexed_rows", rows) // 32))
                needs_build = (
                    not info["ready"]
                    or info.get("partitions") != desired
                    or (
                        info.get("unindexed_rows", 0)
                        >= max(1024, rows * settings.ann_rebuild_percent / 100)
                    )
                )
                with self.condition:
                    if (
                        generation != self.control_generation
                        or settings is not self.db.settings
                        or self.stop.is_set()
                    ):
                        return
                    previous = self.entries.get(space, {})
                    state = "ready" if info["ready"] else "not_prepared"
                    if rows < options.minimum_rows:
                        state = "small"
                        self.forced.discard(space)
                    if space in self.failed:
                        state = "failed"
                    entry = {
                        **info,
                        "space_id": space,
                        "dimension": dimension,
                        "roles": roles,
                        "rows": rows,
                        "state": state,
                        "needs_update": needs_build,
                        "error": previous.get("error") if state == "failed" else None,
                        "duration_seconds": previous.get("duration_seconds", 0),
                    }
                    self.entries[space] = entry
                    requested = space in self.forced or (
                        settings.ann_indexing_enabled and needs_build and space not in self.failed
                    )
                    if self.suppressed or not requested or rows < options.minimum_rows:
                        continue
                    self.forced.discard(space)
                    execution = SearchExecution()
                    process = AnnProcess(
                        threads=min(settings.ann_build_threads, settings.cpu_threads),
                        memory_limit_mib=min(
                            settings.ann_memory_limit_mib, settings.memory_limit_mib
                        ),
                        timeout=3600,
                        background=True,
                    )
                    job = SimpleNamespace(
                        id=secrets.token_urlsafe(16),
                        space=space,
                        dimension=dimension,
                        process=process,
                        execution=execution,
                        started=time.monotonic(),
                        invalidated=False,
                        cancel_state="cancelled",
                    )
                    self.current = job
                    entry.update(state="building", error=None)
            self._perform(store, job, options)
            return

    def _perform(self, store, job, options):
        final = None
        try:
            with execution_context(job.execution):
                result = job.process.build(
                    location=store.location,
                    table="text_" + job.space,
                    dimension=job.dimension,
                    options=options,
                )
                with store.lock:
                    job.execution.check()
                    if result["ready"]:
                        result = store.ann_status(job.space, job.dimension)
                        table = store.table(job.space, job.dimension)
                        rows = table.count_rows()
                        result.update(
                            rows=rows,
                            needs_update=(
                                result.get("unindexed_rows", 0)
                                >= max(1024, rows * self.db.settings.ann_rebuild_percent / 100)
                            ),
                        )
                    final = {
                        **result,
                        "state": "ready" if result["ready"] else "failed",
                        "error": None,
                    }
        except SearchCancelled:
            pass
        except Exception as exc:
            reason = exc.reason if isinstance(exc, AnnUnavailable) else "ann_build_failed"
            final = {"state": "failed", "error": reason}
        finally:
            while not self._stop_process(job):
                # Do not release a writer lease or authorize table deletion if
                # the OS has not actually terminated the native process yet.
                time.sleep(0.1)
            with self.condition:
                entry = self.entries.get(job.space)
                if entry:
                    if job.invalidated:
                        entry.update(state=job.cancel_state, error=None)
                    elif final is not None:
                        entry.update(final)
                        if final.get("ready"):
                            entry.pop("reason", None)
                        if final["state"] == "failed":
                            self.failed.add(job.space)
                    entry["duration_seconds"] = time.monotonic() - job.started
                if self.current is job:
                    self.current = None
            self.wake.set()

    def _loop(self):
        while not self.stop.is_set():
            self.wake.clear()
            started = time.monotonic()
            try:
                if self.available:
                    self._step()
                    with self.condition:
                        self.error = None
                else:
                    with self.condition:
                        self.initializing = False
            except Exception:
                # No native paths/SQL/source details in the background error.
                with self.condition:
                    self.initializing = False
                    self.error = "ann_status_failed"
            # Frequent indexing batches coalesce; they cannot turn metadata
            # polling into a busy loop, even when automatic builds are disabled.
            self.stop.wait(max(0, 0.25 - (time.monotonic() - started)))
            self.wake.wait(1)

    def close(self):
        self.stop.set()
        self.cancel()
        self.wake.set()
        if self.thread:
            # ImportService owns the workspace writer lock and shuts down next.
            # Never release it merely because observing a live writer timed out.
            self.thread.join()
        # Also cover a scheduler that exited unexpectedly while retaining a
        # writer lease. Joining the thread alone does not prove the child died.
        with self.condition:
            job = self.current
        if job is not None:
            while not self._stop_process(job):
                time.sleep(0.1)
            with self.condition:
                if self.current is job:
                    self.current = None
                    entry = self.entries.get(job.space)
                    if entry:
                        entry.update(state=job.cancel_state, error=None)
