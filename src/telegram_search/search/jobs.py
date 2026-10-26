"""Bounded, cooperative search jobs. Native calls finish before cancellation settles."""

import json
import logging
import secrets
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import Future
from dataclasses import dataclass, field

from telegram_search.search.browse import BrowseService
from telegram_search.search.cache import SearchCache
from telegram_search.search.execution import (
    SearchCancelled,
    SearchExecution,
    count,
    execution_context,
    phase,
    search_lock,
)
from telegram_search.search.history import SearchHistory
from telegram_search.search.spec import SearchSpec
from telegram_search.shared.errors import UserError

logger = logging.getLogger(__name__)


class SearchQueueFull(UserError):
    pass


class SearchNotReady(UserError):
    pass


class SearchSnapshotExpired(UserError):
    pass


@dataclass
class SearchJob:
    id: str
    spec: SearchSpec
    tab_id: str | None = None
    execution: SearchExecution = field(default_factory=SearchExecution)
    state: str = "queued"
    error: str | None = None
    warnings: list = field(default_factory=list)
    revision: str | None = None
    result: bytes | None = None
    finished_at: float | None = None
    future: object = None


class SearchJobs:
    """One running inference pipeline, a bounded queue and bounded result snapshots.

    A job owns its immutable request and cancellation event. Cancelling never changes
    indexing state or another job. The final event check and publication share a lock
    with cancel(), so late native results cannot become completed snapshots.
    """

    def __init__(
        self,
        db,
        engine,
        lifecycle_lock,
        *,
        max_pending=16,
        max_results=8,
        max_bytes=32 * 1024**2,
        ttl=600,
        history=None,
    ):
        self.db, self.engine, self.lifecycle_lock = db, engine, lifecycle_lock
        self.history = history or SearchHistory(db, lifecycle_lock)
        self.tabs = None
        self.browser = BrowseService(db)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.queue = deque()
        self.running = None
        self.jobs = OrderedDict()
        self.max_pending, self.max_results = max_pending, max_results
        self.max_bytes, self.ttl = max_bytes, ttl
        self.closed = False
        self.worker = threading.Thread(target=self._worker, name="search", daemon=True)
        self.worker.start()

    def _worker(self):
        while True:
            with self.condition:
                while not self.queue and not self.closed:
                    self.condition.wait()
                if not self.queue:
                    return
                job = self.queue.popleft()
                if not job.future.set_running_or_notify_cancel():
                    continue
                self.running = job
            try:
                self._run(job)
            except Exception:
                # Persistence/native cleanup must never strand the scheduler or
                # its Future. Keep private exception details out of logs/status.
                logger.error("Search worker cleanup failed")
                job.execution.finish()
                with self.lock:
                    if job.state not in {"completed", "cancelled"}:
                        job.state = "failed"
                        job.error = "Не удалось выполнить поиск. Повторите запрос."
                    job.finished_at = time.monotonic()
            finally:
                with self.condition:
                    self.running = None
                    job.future.set_result(None)
                    self.condition.notify_all()

    def _expire(self):
        now = time.monotonic()
        terminal = [job for job in self.jobs.values() if job.finished_at is not None]
        size = sum(len(job.result or b"") for job in terminal)
        results = sum(job.result is not None for job in terminal)
        for job in terminal:
            if now - job.finished_at >= self.ttl or len(self.jobs) > self.max_pending + 32:
                self.jobs.pop(job.id)
                self.history.release("job:" + job.id)
                size -= len(job.result or b"")
                results -= job.result is not None
            elif job.result is not None and (size > self.max_bytes or results > self.max_results):
                size -= len(job.result)
                results -= 1
                job.result = None
                self.history.release("job:" + job.id)

    def create(self, spec, *, tab_id=None):
        inherited = {}
        if "page_size" not in spec.model_fields_set:
            inherited["page_size"] = self.db.settings.search_result_limit
        if "chunk_size" not in spec.model_fields_set:
            inherited["chunk_size"] = self.db.settings.display_chunk_size
        spec = spec.model_copy(update=inherited)
        # Pydantic's frozen models can still contain lists: detach the whole input.
        spec = SearchSpec.model_validate(spec.model_dump(mode="json"))
        with self.lock:
            if self.closed:
                raise UserError("Приложение завершает работу.")
            self._expire()
            if len(self.queue) + int(self.running is not None) >= self.max_pending:
                raise SearchQueueFull("Очередь поиска заполнена. Остановите лишние поиски.")
            job = SearchJob(secrets.token_urlsafe(24), spec, tab_id=tab_id)
            job.execution.requested_strategy = spec.strategy
            self.jobs[job.id] = job
            job.future = Future()
            self.queue.append(job)
            self.condition.notify()
            return self._status(job)

    def _run(self, job):
        execution = job.execution
        try:
            with self.lock:
                execution.check()
                job.state = "running"
            with execution_context(execution):
                phase("scope", unit="messages")
                with search_lock(self.lifecycle_lock):
                    job.revision = SearchCache.revision(self.db)
                    where, params = job.spec.filters().sql()
                    with self.db.connect() as conn:
                        count(
                            "scope_messages",
                            conn.execute(
                                "SELECT count(*) FROM indexable_messages m WHERE " + where, params
                            ).fetchone()[0],
                        )
                if job.spec.scope.mode == "selected" and not job.spec.scope.chat_ids:
                    execution.effective_strategy = "none"
                    result = {
                        "results": [],
                        "has_more": False,
                        "effective_mode": "browse" if job.spec.browsing else job.spec.mode,
                        "warnings": ["Не выбрано ни одного диалога."],
                    }
                elif job.spec.browsing:
                    execution.effective_strategy = "none"
                    result = self.browser.search(job.spec)
                else:
                    dense = not job.spec.exact_phrase and (
                        "images" in job.spec.modalities or job.spec.mode != "words"
                    )
                    execution.effective_strategy = "exact" if dense else "none"
                    if dense and job.spec.strategy == "ann":
                        execution.fallback_reasons.append("ann_index_unavailable")
                    phase("query")
                    result = self.engine.search(
                        job.spec.query,
                        job.spec.filters(),
                        job.spec.exact_phrase,
                        SearchCache.capacity,
                        job.spec.mode,
                        chunk_size=job.spec.chunk_size,
                        modalities=job.spec.modalities,
                    )
                phase("render", total=len(result["results"]), unit="messages")
                execution.advance(len(result["results"]))
                count("returned", len(result["results"]))
                result = {
                    **result,
                    "spec": job.spec.canonical(),
                    "job_id": job.id,
                    "limit": job.spec.page_size,
                    "chunk_size": job.spec.chunk_size,
                    "requested_strategy": execution.requested_strategy,
                    "effective_strategy": execution.effective_strategy,
                    "fallback_reasons": list(execution.fallback_reasons),
                    "diagnostics": execution.snapshot()["diagnostics"],
                }
                with search_lock(self.lifecycle_lock):
                    if SearchCache.revision(self.db) != job.revision:
                        raise UserError("Архив или настройки изменились. Повторите поиск.")
                    with self.lock:
                        execution.check()
                        raw = self.history.remember(
                            job.spec,
                            result,
                            job.revision,
                            "job:" + job.id,
                            max_bytes=self.max_bytes,
                            on_publish=(
                                lambda conn, snapshot_id: self.tabs.finish(
                                    job, snapshot_id, connection=conn
                                )
                            )
                            if self.tabs is not None and job.tab_id
                            else None,
                        )
                        job.result, job.warnings, job.state = (
                            raw,
                            result.get("warnings", []),
                            "completed",
                        )
        except SearchCancelled:
            with self.lock:
                job.state, job.result = "cancelled", None
        except Exception as exc:
            with self.lock:
                job.state = "cancelled" if execution.cancelled.is_set() else "failed"
                # Native exceptions may contain private source text or local paths.
                job.error = (
                    str(exc)
                    if isinstance(exc, UserError)
                    else "Не удалось выполнить поиск. Повторите запрос."
                )
                if job.state == "cancelled":
                    job.error = None
        finally:
            execution.finish()
            if self.tabs is not None and job.tab_id:
                try:
                    with self.lifecycle_lock:
                        self.tabs.finish(job)
                except Exception:
                    logger.error("Search tab cleanup could not be saved")
            with self.lock:
                job.finished_at = time.monotonic()
                self._expire()

    def _get(self, job_id):
        self._expire()
        try:
            return self.jobs[job_id]
        except KeyError:
            raise KeyError("Search job not found") from None

    def _status(self, job):
        queued = [key for key, item in self.jobs.items() if item.state == "queued"]
        return {
            **job.execution.snapshot(),
            "job_id": job.id,
            "state": job.state,
            "queue_position": queued.index(job.id) + 1 if job.id in queued else None,
            "warnings": list(job.warnings),
            "error": job.error,
        }

    def status(self, job_id):
        with self.lock:
            return self._status(self._get(job_id))

    def cancel(self, job_id):
        with self.lock:
            job = self._get(job_id)
            if job.state in {"queued", "running", "cancelling"}:
                job.execution.cancelled.set()
                if job.future.cancel():
                    self.queue.remove(job)
                    job.state, job.finished_at = "cancelled", time.monotonic()
                    job.execution.finish()
                    if self.tabs is not None and job.tab_id:
                        # No lifecycle acquisition here: close() can already own
                        # the job lock. Finishing a cancelled tab only updates SQLite.
                        try:
                            self.tabs.finish(job)
                        except Exception:
                            logger.error("Cancelled search tab cleanup could not be saved")
                else:
                    job.state = "cancelling"
            return self._status(job)

    def result(self, job_id, *, offset=0, limit=None, cursor=None):
        with self.lock:
            job = self._get(job_id)
            if job.state != "completed":
                raise SearchNotReady("Поиск ещё не завершён или был остановлен.")
            raw = job.result
            if raw is None:
                raise SearchSnapshotExpired("Снимок поиска истёк. Повторите поиск.")
        limit = job.spec.page_size if limit is None else limit
        if (
            type(offset) is not int
            or not 0 <= offset <= 100
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise UserError("Недопустимые параметры страницы.")
        with search_lock(self.lifecycle_lock):
            if SearchCache.revision(self.db) != job.revision:
                raise SearchSnapshotExpired("Архив или настройки изменились. Повторите поиск.")
            result = json.loads(raw)
            if job.spec.browsing:
                if offset:
                    raise UserError("Для просмотра сообщений используйте курсор страницы.")
                if cursor or limit != job.spec.page_size:
                    result = {**result, **self.browser.search(job.spec, cursor=cursor, limit=limit)}
                    if cursor and result.get("snapshot_id"):
                        self.history.append_browse(
                            result["snapshot_id"], cursor, result, max_bytes=self.max_bytes
                        )
                return {**result, "offset": 0, "next_offset": None, "limit": limit}
            if cursor:
                raise UserError("Курсор доступен только для просмотра сообщений.")
            hits = result["results"]
            end = min(offset + limit, len(hits))
            if result.get("snapshot_id"):
                self.history.note_view(result["snapshot_id"], end)
            return {
                **result,
                "results": hits[offset:end],
                "offset": offset,
                "cached_results": len(hits),
                "next_offset": end if end < len(hits) else None,
                "has_more": end < len(hits) or result.get("has_more", False),
            }

    def clear_results(self):
        with self.lock:
            for job in self.jobs.values():
                if job.result is not None:
                    job.result = None
                    self.history.release("job:" + job.id)

    def close(self):
        with self.condition:
            self.closed = True
            for job in list(self.jobs.values()):
                if job.finished_at is None:
                    self.cancel(job.id)
            self.condition.notify_all()
        self.worker.join()
        with self.lock:
            job_ids = tuple(self.jobs)
        for job_id in job_ids:
            self.history.release("job:" + job_id)
