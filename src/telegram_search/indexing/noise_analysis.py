"""Cancellable CPU statistics with short canonical reads and private reports."""

import json
import secrets
import sqlite3
import threading
import time
from collections import deque
from functools import lru_cache
from types import SimpleNamespace

import psutil

from telegram_search.indexing.cpu_publication import publish_result
from telegram_search.search.chunking.noise.candidates import CandidateGenerator
from telegram_search.search.chunking.noise.contracts import ANALYSIS_VERSION, AnalysisOptions
from telegram_search.search.chunking.noise.coverage import application_ready, coverage
from telegram_search.search.chunking.noise.lexicon import load_lexicon
from telegram_search.search.chunking.noise.scoring import SCORE_VERSION, evaluate
from telegram_search.search.chunking.noise.statistics import (
    MAX_METADATA_CHARS,
    MAX_TEXT_CHARS,
    collect_batch,
)
from telegram_search.shared.errors import UserError
from telegram_search.storage.contention import database_busy
from telegram_search.storage.noise import TERMINAL, NoiseReport, NoiseStore, fence_id, source_fence


class AnalysisCancelled(BaseException):
    pass


class AnalysisStale(Exception):
    pass


class AnalysisLimited(Exception):
    pass


class NoiseAnalysisService:
    def __init__(self, db):
        self.db, self.store = db, NoiseStore(db)
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.queue = deque()
        self.jobs = {}
        self.thread = None
        self.persistence_error = False
        self.comparison_active = lambda _job_id: False
        self.comparison_ids = lambda: ()
        self.store.recover()

    def create(self, chat_id, options=None):
        options = options or AnalysisOptions()
        with self.lock:
            if self.stop.is_set():
                raise UserError("Анализатор останавливается.")
            if self.persistence_error:
                raise UserError("Не удалось сохранить анализ. Перезапустите приложение.")
            if len(self.jobs) >= 8:
                raise UserError("В очереди уже 8 анализов. Дождитесь завершения.")
            try:
                job_id, fence = self.store.create(
                    chat_id, options, active_ids=(*self.jobs, *self.comparison_ids())
                )
            except sqlite3.Error as exc:
                if database_busy(exc):
                    raise UserError(
                        "База занята. Повторите запуск анализа через несколько секунд."
                    ) from None
                raise UserError("Не удалось создать анализ.") from None
            job = SimpleNamespace(
                id=job_id,
                chat_id=chat_id,
                fence=fence,
                options=options,
                cancelled=threading.Event(),
                state="queued",
                error=None,
                summary=None,
                started=None,
                cpu_started=None,
                deadline=None,
                rss_peak=0,
                rss_sample=0,
                last_publish=0,
                publish_failed=False,
                progress={
                    "phase": "queued",
                    "completed": 0,
                    "total": None,
                    "scanned_messages": 0,
                    "whole_messages": 0,
                    "forms": 0,
                    "elapsed_seconds": 0,
                    "version": ANALYSIS_VERSION,
                },
            )
            self.jobs[job_id] = job
            self.queue.append(job)
            if self.thread is None:
                self.thread = threading.Thread(
                    target=self._loop, name="noise-analysis", daemon=True
                )
                self.thread.start()
            self.wake.set()
        return self.status(chat_id, job_id)

    def _checkpoint(self, job):
        if self.stop.is_set() or job.cancelled.is_set():
            raise AnalysisCancelled()
        now = time.monotonic()
        if job.deadline is not None and now > job.deadline:
            raise AnalysisLimited()
        if now - job.rss_sample >= 0.1:
            job.rss_sample = now
            job.rss_peak = max(job.rss_peak, psutil.Process().memory_info().rss)

    def _retry_read(self, job, action):
        while True:
            self._checkpoint(job)
            try:
                result = action()
                with self.lock:
                    job.progress["waiting_reason"] = None
                return result
            except sqlite3.Error as exc:
                self._checkpoint(job)
                if not database_busy(exc):
                    raise
                with self.lock:
                    job.progress["waiting_reason"] = "database"
                self.stop.wait(0.1)

    def _current(self, job, conn=None):
        self._checkpoint(job)
        if conn is not None:
            if source_fence(conn, job.chat_id) != job.fence:
                raise AnalysisStale()
        else:

            def read():
                with self.db.connection_timeout(0.2), self.db.connect() as reader:
                    reader.execute("BEGIN")
                    self._current(job, reader)

            self._retry_read(job, read)

    def _progress(self, job, **values):
        with self.lock:
            job.progress.update(values)
            if job.started is not None:
                job.progress["elapsed_seconds"] = round(time.monotonic() - job.started, 3)
        if time.monotonic() - job.last_publish >= 0.5:
            try:
                self.store.update(job.id, job.state, job.progress)
                job.last_publish = time.monotonic()
            except sqlite3.Error:
                # Progress is retained in memory. A transient writer cannot kill
                # this worker, and terminal persistence is handled separately.
                pass

    def _page(self, job, cursor, limit):
        return self._retry_read(job, lambda: self._source_page(job, cursor, limit))

    def _source_page(self, job, cursor, limit):
        with self.db.connection_timeout(0.2), self.db.connect() as conn:
            conn.execute("BEGIN")
            conn.set_progress_handler(
                lambda: int(
                    job.cancelled.is_set() or self.stop.is_set() or time.monotonic() > job.deadline
                ),
                1000,
            )
            self._current(job, conn)
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT m.message_id,m.timestamp,substr(m.author_id,1,1025) AS author_id,"
                    "m.has_photo,"
                    "substr(m.text,1,?) AS text,"
                    "(length(m.raw_json)>? OR length(m.author_id)>1024) AS metadata_limited,"
                    "strftime('%Y-%m-%d',m.timestamp,'unixepoch') AS day,"
                    "CASE WHEN length(m.raw_json)>? THEN 1 ELSE "
                    "EXISTS(SELECT 1 FROM media_refs r WHERE r.chat_id=m.chat_id "
                    "AND r.message_id=m.message_id) "
                    "OR json_extract(m.raw_json,'$.media_type') IS NOT NULL "
                    "OR json_extract(m.raw_json,'$.file') IS NOT NULL END AS has_attachment,"
                    "CASE WHEN length(m.raw_json)>? THEN 1 ELSE "
                    "EXISTS(SELECT 1 FROM json_each(m.raw_json,'$.text_entities') e WHERE "
                    "json_extract(CASE WHEN e.type='object' THEN e.value ELSE '{}' END,'$.type') "
                    "IN ('mention','text_mention','text_link','code','pre','link')) "
                    "END AS protected,"
                    "CASE WHEN length(p.text)>? THEN 1 "
                    "ELSE COALESCE(instr(p.text,'?')>0,0) END AS parent_question "
                    "FROM indexable_messages m LEFT JOIN indexable_messages p "
                    "ON p.chat_id=m.chat_id "
                    "AND p.message_id=m.reply_to AND p.remote_deleted=0 "
                    "WHERE m.chat_id=? AND m.remote_deleted=0 AND m.kind='message' "
                    "AND (m.timestamp,m.message_id)>(?,?) "
                    "ORDER BY m.timestamp,m.message_id LIMIT ?",
                    (
                        MAX_TEXT_CHARS + 1,
                        MAX_METADATA_CHARS,
                        MAX_METADATA_CHARS,
                        MAX_METADATA_CHARS,
                        MAX_TEXT_CHARS,
                        job.chat_id,
                        *cursor,
                        limit,
                    ),
                )
            ]

    def _analyze(self, job):
        report = NoiseReport(self.db, job.id, job.fence, create=True)
        cursor, scanned, whole, limited_messages = (-(2**63), -(2**63)), 0, 0, 0
        salt = secrets.token_hex(16)
        self._progress(job, phase="scan", completed=0, total=None)
        while scanned < job.options.max_messages:
            self._checkpoint(job)
            rows = self._page(job, cursor, min(128, job.options.max_messages - scanned))
            if not rows:
                break
            # No archive connection or write transaction is held during text work.
            batch = collect_batch(rows, salt, lambda: self._checkpoint(job))
            self._checkpoint(job)
            report.add_batch(batch, job.options.max_forms)
            scanned += len(rows)
            whole += len(batch.observations)
            limited_messages += batch.limited_messages
            cursor = rows[-1]["timestamp"], rows[-1]["message_id"]
            self._progress(
                job,
                completed=scanned,
                scanned_messages=scanned,
                whole_messages=whole,
                forms=report.forms_count,
            )
        if scanned == job.options.max_messages and self._page(job, cursor, 1):
            report.limits.add("message_limit")
        self._current(job)
        self._progress(job, phase="lexicon", completed=0, total=None)
        lexicon = load_lexicon(lambda: self._checkpoint(job))
        generator = CandidateGenerator(
            useful_words=lexicon.forms, check=lambda: self._checkpoint(job)
        )
        self._current(job)
        self._progress(job, phase="candidates", completed=0, total=report.forms_count)
        after, completed, comparisons, postings = "", 0, 0, 0
        decisions = {"keep": 0, "review": 0, "omit": 0}
        limited_evidence_forms = 0
        with report.connect() as frequency_reader:

            @lru_cache(maxsize=4096)
            def frequency(form):
                return report.frequency(frequency_reader, form)

            while counts := report.forms(after):
                self._current(job)
                candidates = []
                for count in counts:
                    self._checkpoint(job)
                    evidence = generator.analyze(count["form"], check=lambda: self._checkpoint(job))
                    comparisons += evidence.comparisons
                    postings += evidence.postings_read
                    payload = evaluate(evidence, count, frequency)
                    if not payload["complete"]:
                        report.limits.add("candidate_limit")
                    decisions[payload["decision"]] += 1
                    candidates.append((count, payload))
                limited_evidence_forms += report.add_candidates(candidates)
                completed += len(counts)
                after = counts[-1]["form"]
                self._progress(job, completed=completed)
        self._current(job)
        return {
            "version": ANALYSIS_VERSION,
            "score_version": SCORE_VERSION,
            "lexicon": {"id": lexicon.id, "sha256": lexicon.sha256, "license": lexicon.license},
            "complete": not report.limits,
            "limits": sorted(report.limits),
            **coverage(report.limits),
            "limited_evidence_forms": limited_evidence_forms,
            "scanned_messages": scanned,
            "whole_messages": whole,
            "forms": report.forms_count,
            "limited_messages": limited_messages,
            "decisions": decisions,
            "candidate_comparisons": comparisons,
            "index_postings_read": postings,
            "new_auto_omit_enabled": False,
            "report_sha256": report.digest(lambda: self._checkpoint(job)),
            "dry_run": {
                "state": "not_available",
                "reason": "policy_not_compiled",
                "chunks": None,
                "tokens": None,
                "padded_token_slots": None,
            },
            "cpu_seconds": round(time.thread_time() - job.cpu_started, 3),
            "process_rss_mib": round(psutil.Process().memory_info().rss / 1024**2, 1),
            "sampled_process_peak_rss_mib": round(job.rss_peak / 1024**2, 1),
        }

    def _run(self, job):
        job.started, job.cpu_started = time.monotonic(), time.thread_time()
        job.deadline = job.started + job.options.time_limit_seconds
        job.state = "running"
        try:
            self._current(job)
            job.summary = self._analyze(job)
            job.state = "ready" if application_ready(job.summary) else "limited"
        except AnalysisCancelled:
            job.state, job.error = "cancelled", None
        except (AnalysisStale, KeyError):
            job.state, job.error = "stale", "source_changed"
        except AnalysisLimited:
            job.state, job.error = "limited", "time_limit"
        except Exception as exc:
            if job.cancelled.is_set() or self.stop.is_set():
                job.state, job.error = "cancelled", None
            else:
                job.state, job.error = (
                    "failed",
                    (
                        "report_size_limit"
                        if isinstance(exc, sqlite3.Error)
                        and getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL
                        else "analysis_failed"
                    ),
                )
        job.progress["elapsed_seconds"] = round(time.monotonic() - job.started, 3)

        def write(conn):
            if job.state in {"ready", "limited"} and source_fence(conn, job.chat_id) != job.fence:
                job.state, job.error = "stale", "source_changed"
            conn.execute(
                "UPDATE noise_analyses SET state=?,progress_json=?,summary_json=?,error=? "
                "WHERE id=?",
                (job.state, json.dumps(job.progress), json.dumps(job.summary), job.error, job.id),
            )

        publish_result(self, job, write)

    def _loop(self):
        while not self.stop.is_set() and not self.persistence_error:
            with self.lock:
                job = self.queue.popleft() if self.queue else None
            if job is None:
                self.wake.wait(0.5)
                self.wake.clear()
                continue
            self._run(job)

    def status(self, chat_id, job_id):
        with self.lock:
            # Read durable state and the in-memory owner in the same publication
            # order. Otherwise a just-finished job can appear 'running' again.
            row = self.store.get(chat_id, job_id)
            job = self.jobs.get(job_id)
            state = job.state if job else row["state"]
            if job is not None and state in TERMINAL and not job.publish_failed:
                # A completed computation still owns its job until terminal
                # persistence succeeds. Do not expose ready with a missing
                # durable summary/report during a concurrent writer's retry.
                state = "cancelling" if job.cancelled.is_set() else "running"
            progress = dict(job.progress) if job else json.loads(row["progress_json"])
            if job is not None and job.started is not None:
                progress["elapsed_seconds"] = round(time.monotonic() - job.started, 3)
            error = job.error if job else row["error"]
            active = job is not None and not job.publish_failed
        stale = row["stale"]
        return {
            "id": job_id,
            "state": "stale" if stale else state,
            "stale": stale,
            "active": active,
            "created_at": row["created_at"],
            "revision": fence_id(json.loads(row["fence_json"])),
            "progress": progress,
            "error": "source_changed" if stale else error,
            "summary": None if stale else json.loads(row["summary_json"] or "null"),
            "options": json.loads(row["options_json"]),
            "new_auto_omit_enabled": False,
        }

    def list(self, chat_id):
        return {"analyses": [self.status(chat_id, job_id) for job_id in self.store.list(chat_id)]}

    def cancel(self, chat_id, job_id):
        self.store.get(chat_id, job_id)
        with self.lock:
            job = self.jobs.get(job_id)
            if job is not None and not job.publish_failed:
                job.cancelled.set()
                job.state = "cancelling"
                self.wake.set()
        return self.status(chat_id, job_id)

    def report(self, chat_id, job_id, *, kind="forms", **page):
        row = self.store.get(chat_id, job_id)
        if row["stale"]:
            raise UserError("Диалог или исключения изменились. Запустите анализ заново.")
        if row["state"] not in {"ready", "limited"}:
            raise UserError("Отчёт анализа ещё не готов.")
        report = NoiseReport(self.db, job_id, json.loads(row["fence_json"]))
        try:
            result = report.families(**page) if kind == "families" else report.page(**page)
        except (OSError, ValueError, TypeError, RecursionError, sqlite3.Error):
            raise UserError("Отчёт анализа несовместим или повреждён.") from None
        if self.store.get(chat_id, job_id)["stale"]:
            raise UserError("Диалог или исключения изменились. Запустите анализ заново.")
        return result

    def delete(self, chat_id, job_id):
        with self.lock:
            self.store.get(chat_id, job_id)
            if self.comparison_active(job_id):
                raise UserError("Сначала остановите сравнение чанкинга.")
            if job_id in self.jobs:
                if self.persistence_error:
                    raise UserError("Не удалось сохранить анализ. Перезапустите приложение.")
                raise UserError("Сначала остановите анализ.")
            self.store.delete(chat_id, job_id)

    def close(self):
        self.stop.set()
        with self.lock:
            for job in self.jobs.values():
                job.cancelled.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join()
