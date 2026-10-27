"""Bounded, durable and cancellable CPU comparison jobs for the local UI/API."""

import json
import sqlite3
import threading
import time
from collections import deque
from types import SimpleNamespace

from telegram_search.indexing.cpu_publication import publish_result
from telegram_search.indexing.noise_dry_run import NoiseDryRun, TokenizerUnavailable
from telegram_search.search.chunking.noise.contracts import DryRunOptions
from telegram_search.shared.errors import NoiseDraftConflict, UserError
from telegram_search.storage.contention import database_busy
from telegram_search.storage.noise import TERMINAL
from telegram_search.storage.noise_dry_runs import NoiseDryRunStore


class NoiseComparisons:
    def __init__(self, policies):
        self.policies, self.db = policies, policies.db
        # One order for comparison admission, parent deletion and publication.
        self.lock = policies.analyses.lock
        self.store = NoiseDryRunStore(self.db)
        self.store.recover()
        self.queue, self.jobs = deque(), {}
        self.stop, self.wake = threading.Event(), threading.Event()
        self.thread = None
        self.persistence_error = False
        policies.analyses.comparison_active = self.owns_parent
        policies.analyses.comparison_ids = self.parent_ids

    def parent_ids(self):
        with self.lock:
            return tuple(job.analysis_id for job in self.jobs.values())

    def owns_parent(self, analysis_id, *, chat_id=None):
        with self.lock:
            owned = any(
                job.analysis_id == analysis_id and (chat_id is None or job.chat_id == chat_id)
                for job in self.jobs.values()
            )
            if owned and self.persistence_error:
                raise UserError("Не удалось сохранить сравнение. Перезапустите приложение.")
            return owned

    def create(self, chat_id, analysis_id, revision, options=None):
        self.policies._revision(revision)
        options = options or DryRunOptions()
        with self.lock:
            if self.stop.is_set():
                raise UserError("Сравнение чанкинга останавливается.")
            if self.persistence_error:
                raise UserError("Не удалось сохранить сравнение. Перезапустите приложение.")
            if len(self.jobs) >= 8:
                raise UserError("В очереди уже 8 сравнений. Дождитесь завершения.")
            if self.owns_parent(analysis_id, chat_id=chat_id):
                raise UserError("Сравнение этого отчёта уже выполняется.")
            row = self.store.create(chat_id, analysis_id, revision, options)
            job = SimpleNamespace(
                id=row["id"],
                chat_id=chat_id,
                analysis_id=analysis_id,
                revision=revision,
                options=options,
                cancelled=threading.Event(),
                state="queued",
                error=None,
                summary=None,
                started=None,
                progress=json.loads(row["progress_json"]),
                last_publish=0,
                publish_failed=False,
            )
            self.jobs[job.id] = job
            self.queue.append(job)
            if self.thread is None:
                self.thread = threading.Thread(
                    target=self._loop, name="noise-comparison", daemon=True
                )
                self.thread.start()
            self.wake.set()
        return self.status(chat_id, analysis_id, job.id)

    def _progress(self, job, value):
        with self.lock:
            job.progress.update(value)
        if time.monotonic() - job.last_publish >= 0.5:
            try:
                self.store.progress(job.id, job.progress)
                job.last_publish = time.monotonic()
            except sqlite3.Error:
                pass  # In-memory progress survives a busy writer.

    def _current(self, job):
        row = self.store.get(job.chat_id, job.analysis_id, job.id)
        if row["stale"]:
            raise NoiseDraftConflict("Сравнение устарело. Запустите его заново.")

    def _run(self, job):
        job.started, job.state = time.monotonic(), "running"
        self._progress(job, {"phase": "preparing"})
        try:
            if self.stop.is_set() or job.cancelled.is_set():
                job.state = "cancelled"
                return
            self._current(job)
            job.summary = NoiseDryRun(self.policies).compare(
                job.chat_id,
                job.analysis_id,
                job.revision,
                options=job.options,
                cancelled=lambda: self.stop.is_set() or job.cancelled.is_set(),
                progress=lambda value: self._progress(job, value),
                admission_check=lambda: self._current(job),
            )
            job.state = job.summary["state"]
        except (NoiseDraftConflict, KeyError):
            job.state, job.error = "stale", "source_changed"
        except TokenizerUnavailable:
            job.state, job.error = "failed", "tokenizer_unavailable"
        except Exception as exc:
            job.state, job.error = (
                "failed",
                (
                    "database_busy"
                    if isinstance(exc, sqlite3.Error) and database_busy(exc)
                    else "comparison_failed"
                ),
            )
        finally:
            job.progress["elapsed_seconds"] = round(time.monotonic() - job.started, 3)
            publish_result(self, job, lambda conn: self.store.publish(conn, job))

    def _loop(self):
        while not self.stop.is_set() and not self.persistence_error:
            with self.lock:
                job = self.queue.popleft() if self.queue else None
            if job is None:
                self.wake.wait(0.5)
                self.wake.clear()
            else:
                self._run(job)

    def status(self, chat_id, analysis_id, job_id):
        with self.lock:
            row = self.store.get(chat_id, analysis_id, job_id)
            job = self.jobs.get(job_id)
            state = job.state if job else row["state"]
            if job is not None and state in TERMINAL and not job.publish_failed:
                state = "cancelling" if job.cancelled.is_set() else "running"
            progress = dict(job.progress) if job else json.loads(row["progress_json"])
            if job is not None and job.started is not None:
                progress["elapsed_seconds"] = round(time.monotonic() - job.started, 3)
            active = job is not None and not job.publish_failed
            summary = None if job is not None else json.loads(row["summary_json"] or "null")
            error = job.error if job else row["error"]
        stale = row["stale"]
        return {
            "id": job_id,
            "analysis_id": analysis_id,
            "draft_revision": row["draft_revision"],
            "state": "stale" if stale else state,
            "stale": stale,
            "active": active,
            "progress": progress,
            "summary": None if stale else summary,
            "error": "source_changed" if stale else error,
            "created_at": row["created_at"],
            "options": json.loads(row["options_json"]),
        }

    def latest(self, chat_id, analysis_id):
        with self.lock:
            job_id = self.store.latest(chat_id, analysis_id)
            return {"comparison": self.status(chat_id, analysis_id, job_id) if job_id else None}

    def cancel(self, chat_id, analysis_id, job_id):
        with self.lock:
            self.store.get(chat_id, analysis_id, job_id)
            job = self.jobs.get(job_id)
            if job is not None and not job.publish_failed:
                job.cancelled.set()
                job.state = "cancelling"
                self.wake.set()
        return self.status(chat_id, analysis_id, job_id)

    def close(self):
        self.stop.set()
        with self.lock:
            for job in self.jobs.values():
                job.cancelled.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join()
