import importlib.util
import json
import logging
import threading
import time
from collections import OrderedDict

from telegram_search.config.model_registry import model_spec, registry
from telegram_search.indexing.estimates import Estimates
from telegram_search.indexing.forecast import paused_forecast
from telegram_search.indexing.worker import SegmentWorker
from telegram_search.indexing.worker_health import WorkerHealth
from telegram_search.inference.resources import configure_gpu_load
from telegram_search.search.execution import checkpoint, search_lock
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize
from telegram_search.storage.chunking import reset_for_rebuild
from telegram_search.storage.contention import database_busy
from telegram_search.storage.generations import invalidate_segments

logger = logging.getLogger(__name__)


class SemanticService:
    """One model space with independently selected indexing and query sessions."""

    def __init__(self, db, lifecycle_lock, *, start_background=True):
        self.db = db
        configure_gpu_load(db.settings.gpu_index_load_percent)
        self.lock = lifecycle_lock
        self.encoder = None
        self.vectors = None
        self.ann_service = None
        self.stop = threading.Event()
        self.preparing_encoder = threading.Event()
        self.wake = threading.Event()
        self.preparation = None
        self.background = None
        self.background_enabled = start_background
        self.health = WorkerHealth()
        self._current_work = None
        self._busy_recovery = None
        self._failed_result = None
        self.last_used = time.monotonic()
        self.estimates = Estimates(db)
        self.query_cache = OrderedDict()
        self.available = all(
            importlib.util.find_spec(name)
            for name in ("onnxruntime", "tokenizers", "lancedb", "numpy")
        )
        with self.db.connect() as conn:
            conn.execute("UPDATE index_work SET state='pending' WHERE state='running'")
            conn.execute(
                "UPDATE semantic_state SET preparation_state='interrupted' "
                "WHERE preparation_state IN ('downloading','preparing')"
            )
        if self.available:
            try:
                self._load_existing()
            except UserError as exc:
                with self.db.connect() as conn:
                    conn.execute("UPDATE semantic_state SET error=? WHERE id=1", (str(exc),))
            if start_background:
                self.start_background()

    def start_background(self):
        with self.lock:
            self.background_enabled = True
            if (
                self.available
                and not self.stop.is_set()
                and (self.background is None or not self.background.is_alive())
            ):
                # An explicitly restarted dead thread may have left its own
                # claimed attempt running. Recover only that attempt, not work
                # invalidated/retried by another operation in the meantime.
                self._busy_recovery = self._current_work or self._busy_recovery
                self.health.update("starting")
                self.background = threading.Thread(
                    target=self._loop, name="semantic-index", daemon=True
                )
                self.background.start()

    def worker_status(self):
        return self.health.snapshot(
            enabled=self.background_enabled and self.available,
            thread=self.background,
            stopped=self.stop.is_set(),
        )

    def _new_encoder(self, profile, settings=None):
        from telegram_search.inference.bundles import BundleStore
        from telegram_search.inference.e5 import E5Encoder

        spec = model_spec(profile)
        settings = settings or self.db.settings
        return E5Encoder(
            spec,
            BundleStore(self.db.workspace).verify(spec),
            threads=settings.cpu_threads,
            device=settings.model_device("e5"),
            search_device=settings.model_device("e5", query=True),
            gpu_device_id=settings.gpu_device_id,
            gpu_memory_limit_mib=settings.gpu_memory_limit_mib,
        )

    def _load_existing(self):
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT e.* FROM embedding_spaces e JOIN semantic_state s "
                "ON s.active_space_id=e.id WHERE s.enabled=1"
            ).fetchone()
        if row:
            encoder = self._new_encoder(row["profile"])
            encoder.adopt_space(row["manifest_json"])
            if (
                encoder.space_id != row["id"]
                or serialize(encoder.space_manifest) != row["manifest_json"]
            ):
                raise UserError(
                    "Модель или runtime отличается от закреплённого индекса. "
                    "Подготовьте явную переиндексацию."
                )
            self.encoder = encoder

    def _vector_store(self):
        from telegram_search.search.ann import AnnOptions
        from telegram_search.search.vectors import VectorStore

        with self.lock:
            if self.vectors is None:
                self.vectors = VectorStore(self.db.workspace)
            self.vectors.ann_observer = self.ann_service
            self.vectors.ann_options = AnnOptions(
                partitions=self.db.settings.ann_partitions, probes=self.db.settings.ann_probes
            )
            self.vectors.ann_threads = self.db.settings.cpu_threads
            self.vectors.ann_memory_limit_mib = min(
                self.db.settings.ann_memory_limit_mib, self.db.settings.memory_limit_mib
            )
            return self.vectors

    def activate(self, encoder, *, reindex=False, before_commit=None):
        with self.lock:
            old = self.encoder
            if self.ann_service is not None and (old is None or old.space_id != encoder.space_id):
                self.ann_service.models_changed(old.space_id if old else None)
            if old and old is not encoder:
                # Cleanup can fail; do it before committing or publishing a new
                # space. The old encoder lazily reloads if activation aborts.
                old.unload()
            with self.db.connect() as conn:
                current = conn.execute(
                    "SELECT active_space_id FROM semantic_state WHERE id=1"
                ).fetchone()[0]
                if current and current != encoder.space_id and not reindex:
                    from telegram_search.inference.compatibility import compatible_manifest

                    reference = conn.execute(
                        "SELECT manifest_json FROM embedding_spaces WHERE id=?", (current,)
                    ).fetchone()
                    if not reference or not compatible_manifest(
                        encoder.space_manifest, json.loads(reference[0])
                    ):
                        raise UserError("Смена embedding space требует явной переиндексации.")
                    encoder.adopt_space(reference[0])
                    if encoder.space_id != current:
                        raise UserError("Manifest embedding space не совпадает с базой.")
                existing = conn.execute(
                    "SELECT manifest_json FROM embedding_spaces WHERE id=?", (encoder.space_id,)
                ).fetchone()
                if existing and existing[0] != serialize(encoder.space_manifest):
                    raise UserError("Manifest embedding space не совпадает с базой.")
                conn.execute(
                    "INSERT OR IGNORE INTO embedding_spaces VALUES(?,?,?,?,?)",
                    (
                        encoder.space_id,
                        encoder.spec.profile,
                        serialize(encoder.space_manifest),
                        encoder.spec.dimension,
                        int(time.time()),
                    ),
                )
                if reindex:
                    reset_for_rebuild(conn)
                if current != encoder.space_id or reindex:
                    for row in conn.execute("SELECT chat_id,utc_day FROM index_segments"):
                        invalidate_segments(conn, row["chat_id"], {row["utc_day"]}, "model_reindex")
                conn.execute(
                    "UPDATE semantic_state SET enabled=1,active_space_id=?,error=NULL,"
                    "preparation_state='ready' WHERE id=1",
                    (encoder.space_id,),
                )
                if before_commit:
                    before_commit(conn)
            # The durable space must commit before queries can see a new encoder.
            self.encoder = encoder
            self.query_cache.clear()
            self.last_used = time.monotonic()
        self.wake.set()

    def prepare(
        self, profile=None, *, reindex=False, offline=False, repair=False, local_bundle=None
    ):
        if not self.available:
            raise UserError(
                "Установите ONNX runtime: uv sync --locked --extra semantic или --extra gpu."
            )
        with self.lock:
            if self.preparation and self.preparation.is_alive():
                raise UserError("Подготовка модели уже выполняется.")
            with self.db.connect() as conn:
                current = conn.execute(
                    "SELECT e.profile FROM embedding_spaces e JOIN semantic_state s "
                    "ON s.active_space_id=e.id"
                ).fetchone()
                profile = profile or (current[0] if current else "berta")
                spec = model_spec(profile)
                if current and current[0] != profile and not reindex:
                    raise UserError("Подтвердите переиндексацию при смене модели.")
                conn.execute(
                    "UPDATE semantic_state SET preparation_state='downloading',"
                    "requested_profile=?,download_completed_bytes=0,download_total_bytes=?,"
                    "error=NULL WHERE id=1",
                    (profile, spec.download_bytes),
                )
            self.preparation = threading.Thread(
                target=self._prepare,
                args=(spec, reindex, offline, repair, local_bundle),
                name="model-prepare",
                daemon=True,
            )
            self.preparation.start()
        return self.status()

    def _prepare(self, spec, reindex, offline, repair, local_bundle):
        from telegram_search.inference.bundles import BundleStore

        def progress(event):
            if self.stop.is_set():
                raise UserError("Подготовка модели остановлена.")
            with self.lock, self.db.connect() as conn:
                conn.execute(
                    "UPDATE semantic_state SET download_completed_bytes=? WHERE id=1",
                    (event["completed_bytes"],),
                )

        previous = None
        encoder = None
        try:
            BundleStore(self.db.workspace).prepare(
                spec, offline=offline, progress=progress, repair=repair, local_bundle=local_bundle
            )
            if self.stop.is_set():
                raise UserError("Подготовка модели остановлена.")
            with self.lock, self.db.connect() as conn:
                conn.execute("UPDATE semantic_state SET preparation_state='preparing' WHERE id=1")
                self.preparing_encoder.set()
                previous = self.encoder
                if previous:
                    previous.suspend()
                self.query_cache.clear()
            encoder = self._new_encoder(spec.profile)
            encoder.encode_text(["Проверка локальной индексации"], "passage")
            encoder.encode_text(["Проверка локального поиска"], "query", interactive=True)
            encoder.unload_index()
            if self.stop.is_set():
                raise UserError("Подготовка модели остановлена.")
            self.activate(encoder, reindex=reindex)
        except Exception as exc:
            if encoder and encoder is not self.encoder:
                encoder.unload()
            error = (
                str(exc)
                if isinstance(exc, UserError)
                else "Не удалось подготовить модель. Повторите попытку."
            )
            with self.lock, self.db.connect() as conn:
                conn.execute(
                    "UPDATE semantic_state SET preparation_state=?,error=? WHERE id=1",
                    ("interrupted" if self.stop.is_set() else "failed", error),
                )
        finally:
            with self.lock:
                if previous and self.encoder is previous:
                    previous.resume()
                self.preparing_encoder.clear()
            self.wake.set()

    def control(self, action):
        with self.lock, self.db.connect() as conn:
            if action == "pause":
                conn.execute("UPDATE semantic_state SET paused=1 WHERE id=1")
            elif action in {"resume", "retry"}:
                conn.execute("UPDATE semantic_state SET paused=0,error=NULL WHERE id=1")
                if action == "retry":
                    conn.execute(
                        "UPDATE index_work SET state='pending',error=NULL WHERE state='failed'"
                    )
            else:
                raise UserError("Неизвестное действие semantic index.")
        self.wake.set()
        if action in {"resume", "retry"} and self.background_enabled:
            self.start_background()
        return self.status()

    def encode_query(self, query):
        if self.encoder is None:
            raise UserError("Сначала подготовьте модель смыслового поиска.")
        self.last_used = time.monotonic()
        with search_lock(self.lock):
            if self.preparing_encoder.is_set():
                raise UserError("Модель поиска готовится. Поиск по словам доступен.")
            encoder = self.encoder
            cached = self.query_cache.get(query)
            if cached is not None:
                self.query_cache.move_to_end(query)
                return encoder, cached
        value = encoder.encode_text([query], "query", interactive=True)[0]
        checkpoint()
        with search_lock(self.lock):
            if self.encoder is encoder:
                self.query_cache[query] = value
                while len(self.query_cache) > self.db.settings.query_cache_entries:
                    self.query_cache.popitem(last=False)
        return encoder, value

    def status(self, chat_id=None):
        with search_lock(self.lock):
            return self._status_locked(chat_id)

    def _status_locked(self, chat_id=None):
        with self.db.connect() as conn:
            state = dict(conn.execute("SELECT * FROM semantic_state WHERE id=1").fetchone())
            clause = " AND chat_id=?" if chat_id is not None else ""
            args = (chat_id,) if chat_id is not None else ()
            total = conn.execute(
                "SELECT COUNT(*) FROM index_segments WHERE 1=1" + clause, args
            ).fetchone()[0]
            ready = conn.execute(
                "SELECT COUNT(*) FROM index_segments WHERE dense_generation=target_generation "
                "AND chunk_generation=target_generation AND embedding_space_id=?" + clause,
                (state["active_space_id"], *args),
            ).fetchone()[0]
            active = conn.execute(
                "SELECT profile,dimension FROM embedding_spaces WHERE id=?",
                (state["active_space_id"],),
            ).fetchone()
            works = [
                dict(row)
                for row in conn.execute(
                    "SELECT state,COUNT(*) AS count,"
                    "SUM(chunks_total) AS chunks_total,SUM(chunks_done) AS chunks_done "
                    "FROM index_work WHERE state IN ('pending','running','failed')"
                    + clause
                    + " GROUP BY state",
                    args,
                )
            ]
            failed = conn.execute(
                "SELECT error FROM index_work WHERE state='failed' AND error IS NOT NULL"
                + clause
                + " ORDER BY created_at DESC,id DESC LIMIT 1",
                args,
            ).fetchone()
            if chat_id is not None:
                chat = conn.execute("SELECT text_paused FROM chats WHERE id=?", args).fetchone()
                if chat is None:
                    raise UserError("Диалог не найден.")
                state["paused"] = bool(state["paused"] or chat[0])
            blocked = bool(state["paused"]) or (
                chat_id is None
                and conn.execute(
                    "SELECT 1 FROM index_work w JOIN chats c ON c.id=w.chat_id "
                    "JOIN index_segments s ON s.chat_id=w.chat_id AND s.utc_day=w.utc_day "
                    "AND s.target_generation=w.generation WHERE c.text_paused=1 "
                    "AND w.state IN ('pending','running') LIMIT 1"
                ).fetchone()
                is not None
            )
        worker = self.worker_status()
        worker_blocked = worker["waiting_reason"] == "database" or (
            worker["enabled"] and not worker["alive"] and not worker["stopped"]
        )
        estimate = (
            self.estimates.text_forecast(self.encoder, chat_id)
            if ready != total
            and not blocked
            and not worker_blocked
            and any(work["state"] in {"pending", "running"} for work in works)
            else None
        )
        if blocked and ready != total:
            estimate = paused_forecast({"eta_samples": 0, "projected": False})
        return {
            **state,
            "runtime_installed": self.available,
            "dense_available": self.encoder is not None
            and bool(state["enabled"])
            and not self.preparing_encoder.is_set(),
            "profile": active["profile"] if active else None,
            "total_segments": total,
            "ready_segments": ready,
            "pending_segments": total - ready,
            "works": works,
            "worker": worker,
            "index_error": failed[0] if failed else None,
            "estimate": estimate,
            "estimated_remaining_seconds": 0.0
            if ready == total
            else estimate["estimated_remaining_seconds"]
            if estimate
            else None,
            "backend": self.encoder.backend_info() if self.encoder else None,
            "profiles": [
                {
                    "profile": spec.profile,
                    "model_id": spec.model_id,
                    "download_bytes": spec.download_bytes,
                    "dimension": spec.dimension,
                }
                for spec in registry().values()
            ],
        }

    def cleanup(self, *, compact=False):
        if not self.available:
            return
        with self.lock:
            with self.db.connect() as conn:
                spaces = [
                    dict(row) for row in conn.execute("SELECT id,dimension FROM embedding_spaces")
                ]
                deletions = conn.execute(
                    "SELECT chat_id,created_at FROM vector_deletions LIMIT 64"
                ).fetchall()
                segments = conn.execute("SELECT * FROM vector_segment_cleanup LIMIT 64").fetchall()
            if not spaces:
                return
            vectors = self._vector_store()
            # Native Lance work can be slow. Acknowledge each item in a separate
            # short transaction so no SQLite writer is held during the next IO.
            for row in deletions:
                vectors.delete_chat(spaces, row["chat_id"])
                with self.db.connect() as conn:
                    conn.execute(
                        "DELETE FROM vector_deletions WHERE chat_id=? AND created_at=?",
                        (row["chat_id"], row["created_at"]),
                    )
            for row in segments:
                vectors.prune_segment(
                    spaces, row["chat_id"], row["utc_day"], row["keep_space_id"], row["generation"]
                )
                with self.db.connect() as conn:
                    conn.execute(
                        "DELETE FROM vector_segment_cleanup WHERE chat_id=? AND utc_day=? "
                        "AND keep_space_id=? AND generation=?",
                        (row["chat_id"], row["utc_day"], row["keep_space_id"], row["generation"]),
                    )
            if compact:
                vectors.compact(spaces)

    def _step(self):
        self.health.update("cleanup", keep_wait=True)
        self.cleanup()
        worker = None
        with self.lock:
            encoder = self.encoder
            if encoder is not None:
                with self.db.connect() as conn:
                    state = conn.execute(
                        "SELECT enabled,paused FROM semantic_state WHERE id=1"
                    ).fetchone()
                    work = conn.execute(
                        "SELECT w.id,w.generation,w.attempts FROM index_work w "
                        "JOIN chats c ON c.id=w.chat_id "
                        "WHERE w.state='pending' AND c.text_paused=0 "
                        "AND w.available_at<=CAST(strftime('%s','now') AS INTEGER) "
                        "ORDER BY w.created_at,w.id LIMIT 1"
                    ).fetchone()
                if state[0] and not state[1] and work and not self.preparing_encoder.is_set():
                    worker = self._worker(encoder)
        if encoder:
            if worker:
                self._current_work = (work["id"], work["generation"], work["attempts"] + 1)
                self.health.update("indexing", keep_wait=True)
                result = worker.run(work[0])
                self.health.update("indexing")
                if result["state"] == "failed":
                    self._failed_result = (encoder, result)
                    self._pause_failed_result()
                return True
            with self.lock:
                if self.encoder is encoder and encoder.unload_idle(
                    query_last_used=self.last_used,
                    idle_seconds=self.db.settings.idle_unload_seconds,
                    index_paused=bool(state[1])
                    and not getattr(getattr(self, "media", None), "ocr_dense_waiting", False),
                ):
                    self.query_cache.clear()
        self.health.update("idle")
        return False

    def _pause_failed_result(self):
        encoder, result = self._failed_result
        with self.lock, self.db.connect() as conn:
            current = conn.execute(
                "SELECT 1 FROM index_work w JOIN index_segments s "
                "ON s.chat_id=w.chat_id AND s.utc_day=w.utc_day "
                "JOIN chats c ON c.id=w.chat_id WHERE w.id=? "
                "AND w.state='failed' AND w.attempts=? "
                "AND w.generation=? AND s.target_generation=w.generation "
                "AND w.embedding_space_id=? AND c.text_paused=0",
                (
                    result["id"],
                    result["attempts"],
                    result["generation"],
                    encoder.space_id,
                ),
            ).fetchone()
            if self.encoder is encoder and current:
                conn.execute(
                    "UPDATE semantic_state SET paused=1,error=? WHERE id=1",
                    (result["error"],),
                )
        self._failed_result = None

    def _loop(self):
        failures = 0
        try:
            while not self.stop.is_set():
                self._current_work = None
                delay = 1
                with self.db.connection_timeout(0.25):
                    try:
                        if self._failed_result:
                            self._pause_failed_result()
                        if self._busy_recovery:
                            with self.lock, self.db.connect() as conn:
                                # Only our interrupted attempt; preserve its stage,
                                # chunk checkpoint, generation and embedding space.
                                conn.execute(
                                    "UPDATE index_work SET state='pending',error=NULL "
                                    "WHERE id=? AND generation=? AND attempts=? "
                                    "AND state='running'",
                                    self._busy_recovery,
                                )
                            self._busy_recovery = None
                        worked = self._step()
                        failures = 0
                        if worked:
                            continue
                    except Exception as exc:
                        if database_busy(exc):
                            self._busy_recovery = self._current_work or self._busy_recovery
                            delay = min(2, 0.25 * 2 ** min(failures, 3))
                            failures += 1
                            self.health.update(
                                "waiting_database",
                                delay=delay,
                                error="База данных занята. Индексация продолжится автоматически.",
                            )
                            if failures == 1:
                                logger.warning("Semantic indexing is waiting for SQLite")
                        else:
                            self.health.update(
                                "error", error="Не удалось обновить индекс. Повторите задачу."
                            )
                            # Error reporting cannot require the failed resource to
                            # be writable. Keep in-memory health if SQLite also fails.
                            try:
                                with self.db.connect() as conn:
                                    conn.execute(
                                        "UPDATE semantic_state SET error=? WHERE id=1",
                                        ("Не удалось обновить индекс. Повторите задачу.",),
                                    )
                            except Exception:
                                logger.error("Semantic indexing error could not be saved")
                self.wake.wait(delay)
                self.wake.clear()
        finally:
            self.health.update("stopped")

    def _worker(self, encoder):
        return SegmentWorker(
            self.db,
            encoder,
            self._vector_store(),
            self.lock,
            batch_size=self.db.settings.embedding_batch,
            on_claim=lambda: self.health.update("indexing"),
            should_stop=lambda: (
                self.stop.is_set() or self.preparing_encoder.is_set() or self.encoder is not encoder
            ),
        )

    def shutdown(self):
        self.stop.set()
        self.wake.set()
        if self.background:
            self.background.join()
        if self.preparation:
            self.preparation.join()
        if self.encoder:
            self.encoder.unload()
        if self.vectors is not None:
            self.vectors.close()
        self.query_cache.clear()
