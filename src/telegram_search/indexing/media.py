import hashlib
import importlib.util
import threading
import time
from contextlib import contextmanager
from dataclasses import replace

from telegram_search.indexing.estimates import record_rate
from telegram_search.indexing.media_images import MediaImages
from telegram_search.indexing.metrics import ResourceMetrics, StageMetrics
from telegram_search.indexing.ocr_queue import OcrQueue
from telegram_search.indexing.prefetch import BatchPrefetch
from telegram_search.inference.resources import (
    backoff_indexing_batch,
    compute_gate,
    indexing_batch_size,
    memory_exhausted,
)
from telegram_search.security.paths import safe_media_path
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import normalize_text, serialize


class MediaService(MediaImages):
    """Content-addressed OCR/image cache, published only while canonical refs still exist."""

    def __init__(self, db, lifecycle_lock, semantic, *, start_background=True):
        self.db, self.lock, self.semantic = db, lifecycle_lock, semantic
        self.ocr = None
        self.clip = None
        self.running = False
        self.ocr_cpu_running = False
        self.resource_error = None
        self.image_limits = {}
        self.ocr_staging_ids = set()
        self.ocr_claims = set()
        self.ocr_completed = 0
        self.ocr_timings = {}
        self.metrics = StageMetrics()
        self.resources = ResourceMetrics()
        self.image_prefetch = BatchPrefetch()
        self.ocr_queue = OcrQueue(db)
        self.active_stages = set()
        self.ocr_dense_waiting = False
        semantic.media = self
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.preparation = None
        self.last_used = time.monotonic()
        with db.connect() as conn:
            conn.execute(
                "UPDATE media_state SET preparation_state='interrupted' "
                "WHERE preparation_state='preparing'"
            )
        self._load_existing()
        self.background = None
        self.ocr_background = None
        self.image_background = None
        self.semantic_background = None
        if start_background:
            self.start_background()

    def start_background(self):
        if self.background is None:
            self.background = threading.Thread(target=self._loop, name="media-index", daemon=True)
            self.background.start()
            self.ocr_background = threading.Thread(
                target=self._cpu_ocr_loop, name="ocr-cpu-index", daemon=True
            )
            self.ocr_background.start()
            self.image_background = threading.Thread(
                target=lambda: self._stage_loop("clip", self._image_batch),
                name="clip-index",
                daemon=True,
            )
            self.semantic_background = threading.Thread(
                target=lambda: self._stage_loop("ocr_dense", self._ocr_embeddings),
                name="ocr-semantic-index",
                daemon=True,
            )
            self.image_background.start()
            self.semantic_background.start()

    def _new_ocr(self, settings=None):
        from telegram_search.inference.ocr import OcrEngine

        settings = settings or self.db.settings
        if settings.ocr_cpu_workers > 1:
            from telegram_search.inference.ocr_pool import CpuOcrPool

            count = settings.ocr_cpu_workers
            pool = CpuOcrPool(
                [
                    self._new_ocr(
                        replace(
                            settings,
                            ocr_cpu_workers=1,
                            memory_limit_mib=max(512, settings.memory_limit_mib // count),
                            cpu_threads=settings.cpu_threads // count
                            + int(index < settings.cpu_threads % count),
                        )
                    )
                    for index in range(count)
                ],
                settings.cpu_threads,
            )
            pool.rate_settings = (settings.ocr_batch_size, settings.ocr_region_batch_size, count)
            return pool
        if settings.ocr_engine == "paddle":
            from telegram_search.inference.ocr_onnx import OnnxOcrEngine

            return OnnxOcrEngine(self.db.workspace, settings)
        engine = OcrEngine(
            self.db.workspace,
            threads=settings.cpu_threads,
            max_edge=settings.ocr_max_edge,
            timeout=settings.ocr_timeout_seconds,
        )
        engine.rate_settings = (settings.ocr_batch_size, settings.ocr_region_batch_size, 1)
        return engine

    def _new_clip(self, settings=None):
        try:
            from telegram_search.inference.clip import ClipEncoder
        except ImportError as exc:
            raise UserError(
                "Установите ONNX runtime: uv sync --locked --extra semantic или --extra gpu."
            ) from exc

        settings = settings or self.db.settings
        encoder = ClipEncoder(
            self.db.workspace,
            threads=settings.cpu_threads,
            device=settings.model_device("clip"),
            search_device=settings.model_device("clip", query=True),
            gpu_device_id=settings.gpu_device_id,
            gpu_memory_limit_mib=settings.gpu_memory_limit_mib,
        )
        with self.db.connect() as conn:
            spaces = conn.execute(
                "SELECT space_id,COUNT(*) AS count FROM media_embeddings WHERE kind='image' "
                "GROUP BY space_id ORDER BY count DESC,space_id"
            ).fetchall()
        compatible = encoder.compatible_spaces()
        for row in spaces:
            if row["space_id"] in compatible:
                encoder.adopt_space(row["space_id"])
                break
        return encoder

    def _load_existing(self):
        with self.db.connect() as conn:
            state = dict(conn.execute("SELECT * FROM media_state WHERE id=1").fetchone())
        try:
            if state["ocr_enabled"]:
                engine = self._new_ocr()
                engine.verify()
                self.ocr = engine
            if state["images_enabled"]:
                self.clip = self._new_clip()
        except UserError as exc:
            with self.db.connect() as conn:
                conn.execute("UPDATE media_state SET error=? WHERE id=1", (str(exc),))

    def prepare(self, kind, *, offline=False):
        if kind not in {"ocr", "images"}:
            raise UserError("Неизвестная модель медиа.")
        with self.lock:
            if self.preparation and self.preparation.is_alive():
                raise UserError("Подготовка медиа уже выполняется.")
            if self.semantic.preparation and self.semantic.preparation.is_alive():
                raise UserError("Дождитесь подготовки текстовой модели.")
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE media_state SET preparation_state='preparing',error=NULL WHERE id=1"
                )
            self.preparation = threading.Thread(
                target=self._prepare, args=(kind, offline), name="media-model-prepare", daemon=True
            )
            self.preparation.start()
        return self.status()

    def _prepare(self, kind, offline):
        engine = None
        try:
            if kind == "ocr":
                engine = self._new_ocr()
                engine.prepare(offline=offline)
            else:
                from telegram_search.config.model_registry import media_registry
                from telegram_search.inference.bundles import BundleStore

                for spec in media_registry().values():
                    BundleStore(self.db.workspace).prepare(spec, offline=offline, repair=True)
                engine = self._new_clip()
                engine.check_contract()
            with self.lock:
                with self.db.connect() as conn:
                    if kind == "ocr":
                        conn.execute("UPDATE media_state SET ocr_enabled=1 WHERE id=1")
                    else:
                        conn.execute("UPDATE media_state SET images_enabled=1 WHERE id=1")
                    conn.execute(
                        "UPDATE media_state SET preparation_state='ready',error=NULL WHERE id=1"
                    )
                if kind == "ocr":
                    old = self.ocr
                    self.ocr = engine
                    if old and hasattr(old, "unload"):
                        old.unload()
                else:
                    old = self.clip
                    self.clip = engine
                    if old:
                        old.unload()
        except Exception:
            if (
                engine
                and engine is not self.clip
                and engine is not self.ocr
                and hasattr(engine, "unload")
            ):
                engine.unload()
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE media_state SET preparation_state='failed',error=? WHERE id=1",
                    (
                        "Не удалось подготовить медиа. Проверьте runtime, свободное место "
                        "и доступ к закреплённым моделям.",
                    ),
                )
        finally:
            self.wake.set()

    def control(self, action, *, kind="media"):
        columns = {
            "media": ("paused", "ocr_paused", "ocr_dense_paused"),
            "images": ("paused",),
            "ocr": ("ocr_paused",),
            "ocr_dense": ("ocr_dense_paused",),
        }[kind]
        with self.lock, self.db.connect() as conn:
            if action in {"pause", "resume"}:
                for column in columns:
                    conn.execute(
                        f"UPDATE media_state SET {column}=? WHERE id=1", (action == "pause",)
                    )
            elif action == "retry":
                if kind in {"ocr", "media"}:
                    conn.execute("DELETE FROM ocr_cache WHERE state='failed'")
                if kind == "media":
                    conn.execute("DELETE FROM media_failures")
                elif kind == "images" and self.clip:
                    conn.execute(
                        "DELETE FROM media_failures WHERE space_id=?", (self.clip.space_id,)
                    )
                elif kind == "ocr_dense" and self.ocr and self.semantic.encoder:
                    identity = hashlib.sha256(
                        serialize(
                            ["ocr", self.ocr.version, self.semantic.encoder.space_id]
                        ).encode()
                    ).hexdigest()
                    conn.execute("DELETE FROM media_failures WHERE space_id=?", (identity,))
                conn.execute("UPDATE media_state SET error=NULL WHERE id=1")
            else:
                raise UserError("Неизвестное действие медиа.")
        self.wake.set()
        return self.status()

    def status(self, chat_id=None):
        ocr, clip, encoder = self.ocr, self.clip, self.semantic.encoder
        with self.db.connect() as conn:
            conn.execute("BEGIN")
            args = (chat_id,) if chat_id is not None else ()
            refs = " AND r.chat_id=?" if chat_id is not None else ""
            clause = " AND chat_id=?" if chat_id is not None else ""
            state = dict(conn.execute("SELECT * FROM media_state WHERE id=1").fetchone())
            total = conn.execute(
                "SELECT COUNT(DISTINCT sha256) FROM indexable_media_refs "
                "WHERE kind='photo' AND status='ready'" + clause,
                args,
            ).fetchone()[0]
            attachments = conn.execute(
                "SELECT COUNT(*) FROM indexable_media_refs WHERE kind='photo'" + clause, args
            ).fetchone()[0]
            photo_messages = conn.execute(
                "SELECT COUNT(*) FROM (SELECT chat_id,message_id FROM indexable_media_refs "
                "WHERE kind='photo'" + clause + " GROUP BY chat_id,message_id)",
                args,
            ).fetchone()[0]
            retries = conn.execute(
                "SELECT COALESCE(SUM(MAX(w.attempts-1,0)),0) FROM ocr_work w WHERE w.version=? "
                "AND EXISTS (SELECT 1 FROM indexable_media_refs r WHERE r.sha256=w.sha256 "
                "AND r.kind='photo' AND r.status='ready'" + refs + ")",
                (ocr.version if ocr else "", *args),
            ).fetchone()[0]
            ready = failed = images = 0
            if ocr:
                counts = {
                    row[0]: row[1]
                    for row in conn.execute(
                        "SELECT o.state,COUNT(*) FROM ocr_cache o WHERE o.version=? AND EXISTS "
                        "(SELECT 1 FROM indexable_media_refs r WHERE "
                        "r.sha256=o.sha256 AND r.status='ready' "
                        "AND r.kind='photo'" + refs + ") GROUP BY o.state",
                        (ocr.version, *args),
                    )
                }
                ready, failed = counts.get("ready", 0), counts.get("failed", 0)
            nonempty = conn.execute(
                "SELECT COUNT(*) FROM ocr_cache o WHERE version=? AND state='ready' "
                "AND text<>'' AND EXISTS (SELECT 1 FROM "
                "indexable_media_refs r WHERE r.sha256=o.sha256 "
                "AND r.status='ready' AND r.kind='photo'" + refs + ")",
                (ocr.version if ocr else "", *args),
            ).fetchone()[0]
            if clip:
                images = conn.execute(
                    "SELECT COUNT(*) FROM media_embeddings e WHERE kind='image' "
                    "AND space_id=? AND EXISTS (SELECT 1 FROM indexable_media_refs r "
                    "WHERE r.sha256=e.sha256 AND r.status='ready' AND r.kind='photo'" + refs + ")",
                    (clip.space_id, *args),
                ).fetchone()[0]
            missing = conn.execute(
                "SELECT COUNT(*) FROM indexable_media_refs WHERE "
                "kind='photo' AND status<>'ready'" + clause,
                args,
            ).fetchone()[0]
            ocr_dense = conn.execute(
                "SELECT COUNT(DISTINCT e.sha256) FROM media_embeddings e "
                "WHERE kind='ocr' AND space_id=? AND ocr_version=? AND EXISTS "
                "(SELECT 1 FROM indexable_media_refs r WHERE r.sha256=e.sha256 "
                "AND r.status='ready' AND r.kind='photo'" + refs + ")",
                (
                    encoder.space_id if encoder else "",
                    ocr.version if ocr else "",
                    *args,
                ),
            ).fetchone()[0]
            images_failed = conn.execute(
                "SELECT COUNT(*) FROM media_failures f WHERE f.space_id=? AND EXISTS "
                "(SELECT 1 FROM indexable_media_refs r WHERE r.sha256=f.sha256 AND r.kind='photo' "
                "AND r.status='ready'" + refs + ")",
                (clip.space_id if clip else "", *args),
            ).fetchone()[0]
            ocr_failure_space = (
                hashlib.sha256(
                    serialize(["ocr", ocr.version, encoder.space_id]).encode()
                ).hexdigest()
                if ocr and encoder
                else None
            )
            ocr_dense_failed = conn.execute(
                "SELECT COUNT(*) FROM media_failures f WHERE f.space_id=? "
                "AND EXISTS (SELECT 1 FROM indexable_media_refs r WHERE r.sha256=f.sha256 "
                "AND r.kind='photo' AND r.status='ready'" + refs + ")",
                (ocr_failure_space, *args),
            ).fetchone()[0]
            if chat_id is not None:
                chat = conn.execute(
                    "SELECT media_paused,ocr_paused,ocr_dense_paused FROM chats WHERE id=?", args
                ).fetchone()
                if chat is None:
                    raise UserError("Диалог не найден.")
                state["paused"] = bool(state["paused"] or chat[0])
                state["ocr_paused"] = bool(state["ocr_paused"] or chat[1])
                state["ocr_dense_paused"] = bool(state["ocr_dense_paused"] or chat[2])
        queues = {
            stage: self.metrics.snapshot(stage, self._stage_identity(stage), pending)
            for stage, pending in (
                ("ocr", total - ready - failed),
                ("clip", total - images - images_failed),
                ("ocr_dense", nonempty - ocr_dense - ocr_dense_failed),
            )
        }
        ocr_eta = queues["ocr"]["estimated_remaining_seconds"]
        if ocr_eta is None:
            ocr_eta = self.semantic.estimates.ocr(ocr, total - ready - failed)
        image_eta = queues["clip"]["estimated_remaining_seconds"]
        if image_eta is None:
            image_eta = self.semantic.estimates.images(clip, total - images - images_failed)
        # Project future nonempty images from observed coverage, without treating
        # the already-discovered semantic queue as the whole archive.
        dense_eta = queues["ocr_dense"]["estimated_remaining_seconds"]
        future_dense = max(0, total - ready - failed) * nonempty / max(1, ready)
        if ocr and encoder and total > ready + failed:
            speed = queues["ocr_dense"]["units_per_minute"]
            dense_eta = (
                round((queues["ocr_dense"]["pending"] + future_dense) * 60 / speed, 1)
                if speed and ready >= 2
                else None
            )
        remaining = [ocr_eta, image_eta, dense_eta]
        enabled = [bool(ocr), bool(clip), bool(ocr and encoder)]
        known = [eta for eta, active in zip(remaining, enabled, strict=True) if active]
        preparation_eta = sum(known) if known and all(eta is not None for eta in known) else None
        return {
            **state,
            "total_photos": total,
            "photo_attachments": attachments,
            "photo_messages": photo_messages,
            "ocr_retries": retries,
            "resource_usage": self.resources.snapshot(),
            "ocr_ready": ready,
            "ocr_failed": failed,
            "ocr_dense_ready": ocr_dense,
            "ocr_dense_failed": ocr_dense_failed,
            "ocr_nonempty_ready": nonempty,
            "ocr_dense_available": encoder is not None,
            "ocr_estimated_remaining_seconds": ocr_eta,
            "ocr_dense_estimated_remaining_seconds": queues["ocr_dense"][
                "estimated_remaining_seconds"
            ],
            "preparation_estimated_remaining_seconds": preparation_eta,
            "projected_ocr_dense_photos": round(future_dense, 1),
            "preparation_estimate_kind": "serial_upper_bound",
            "queues": queues,
            "images_ready": images,
            "images_failed": images_failed,
            "images_estimated_remaining_seconds": image_eta,
            "missing_refs": missing,
            "ocr_available": ocr is not None,
            "images_available": clip is not None,
            "ocr_runtime_installed": all(
                importlib.util.find_spec(name)
                for name in (
                    ("onnxruntime", "cv2", "pyclipper")
                    if self.db.settings.ocr_engine == "paddle"
                    else ("tesserocr",)
                )
            ),
            "ocr_engine": self.db.settings.ocr_engine,
            "ocr_backend": ocr.backend_info()
            if ocr and hasattr(ocr, "backend_info")
            else ocr.execution.info()
            if ocr and hasattr(ocr, "execution")
            else {"device": "cpu", "provider": "tesseract"},
            "ocr_timings": self.ocr_timings,
            "running": self.running or self.ocr_cpu_running,
            "resource_error": self.resource_error,
            "device": clip.execution.info()["device"]
            if clip
            else self.db.settings.model_device("clip"),
            "backend": clip.execution.info() if clip else None,
            "query_backend": clip.query_execution.info()
            if clip and hasattr(clip, "query_execution")
            else None,
        }

    def _read_photo(self, sha):
        with self.db.connect() as conn:
            refs = conn.execute(
                "SELECT r.relative_path,s.relative_path AS root FROM indexable_media_refs r "
                "JOIN source_roots s ON s.id=r.source_root_id WHERE r.sha256=? "
                "AND r.kind='photo' AND r.status='ready' ORDER BY r.id",
                (sha,),
            ).fetchall()
        for ref in refs:
            try:
                path = safe_media_path(self.db.source_path(ref["root"]), ref["relative_path"])
                with path.open("rb") as stream:
                    data = stream.read(32 * 1024 * 1024 + 1)
                if len(data) <= 32 * 1024 * 1024 and hashlib.sha256(data).hexdigest() == sha:
                    return data
            except (OSError, UserError):
                continue
        raise UserError("Файл недоступен или изменился. Проверьте источник и повторите импорт.")

    def _current(self, conn, sha, *, kind="images"):
        global_column, chat_column = (
            ("ocr_dense_paused", "ocr_dense_paused")
            if kind == "ocr_dense"
            else ("ocr_paused", "ocr_paused")
            if kind == "ocr"
            else ("paused", "media_paused")
        )
        state = conn.execute(f"SELECT {global_column} FROM media_state WHERE id=1").fetchone()
        return (
            not self.stop.is_set()
            and not state[0]
            and conn.execute(
                "SELECT 1 FROM indexable_media_refs r JOIN chats c ON "
                "c.id=r.chat_id WHERE r.sha256=? "
                f"AND c.{chat_column}=0 AND r.status='ready' AND r.kind='photo' LIMIT 1",
                (sha,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _unload_ocr_lane(engine):
        if engine:
            unload = getattr(engine, "unload_worker", None) or getattr(engine, "unload", None)
            if unload:
                unload()

    def _set_active(self, stage, active):
        with self.lock:
            if active:
                self.active_stages.add(stage)
            else:
                self.active_stages.discard(stage)
            self.running = bool(self.active_stages)
            self.ocr_cpu_running = "ocr_cpu" in self.active_stages

    @contextmanager
    def _activity(self, stage):
        self._set_active(stage, True)
        try:
            yield
        finally:
            self._set_active(stage, False)

    def _stage_identity(self, stage):
        engine = (
            self.ocr if stage == "ocr" else self.clip if stage == "clip" else self.semantic.encoder
        )
        if engine is None:
            return None
        execution = getattr(engine, "execution", None)
        return (
            getattr(engine, "version", getattr(engine, "space_id", "")),
            getattr(execution, "provider", "tesseract"),
            getattr(engine, "threads", 1),
            self.db.settings.ocr_batch_size if stage == "ocr" else None,
            self.db.settings.ocr_region_batch_size if stage == "ocr" else None,
            self.db.settings.ocr_cpu_workers if stage == "ocr" else None,
            self.ocr.version if stage == "ocr_dense" and self.ocr else None,
        )

    def _ocr_one(self, engine=None):
        return self._ocr_batch(engine, limit=1)

    def _ocr_run(self, engine=None):
        engine = engine or self.ocr
        if not engine:
            return False
        if not hasattr(engine, "recognize_many"):
            return self._ocr_one(engine)
        return self._ocr_batch(engine, limit=getattr(engine, "batch_limit", 1), configured=True)

    def _ocr_batch(self, engine=None, *, limit=1, configured=False):
        began = time.monotonic()
        with self.lock:
            owner = self.ocr
            engine = engine or owner
            if (
                self.stop.is_set()
                or not owner
                or engine not in (owner, getattr(owner, "cpu_peer", None))
            ):
                return False
            claimed = self.ocr_queue.claim(
                engine.version,
                limit,
                configured=configured,
                with_tokens=True,
                exclude=tuple(self.ocr_claims),
            )
            assignments, region_batch = (
                claimed if configured else (claimed, self.db.settings.ocr_region_batch_size)
            )
            tokens = dict(assignments)
            shas = list(tokens)
            self.ocr_claims.update(shas)
            lane = "ocr_cpu" if engine is getattr(owner, "cpu_peer", None) else "ocr"
            if shas:
                self._set_active(lane, True)
        selected = time.monotonic()
        if not shas:
            self._unload_ocr_lane(engine)
            return False
        results, inputs, good = {}, [], []
        read_finished = compute_started = compute_finished = selected
        try:
            for sha in shas:
                try:
                    data = self._read_photo(sha)
                    # One bounded IPC frame; postpone excess input to a later claim.
                    if inputs and sum(map(len, inputs)) + len(data) > 32 * 1024**2:
                        continue
                    inputs.append(data)
                    good.append(sha)
                except UserError:
                    results[sha] = {"error": True}
            read_finished = time.monotonic()
            compute_started = compute_finished = read_finished
            if inputs:
                with compute_gate(getattr(engine, "execution", None)):
                    with self.db.connect() as conn:
                        if self.ocr is not owner or not all(
                            self._current(conn, sha, kind="ocr") for sha in good
                        ):
                            return False
                    compute_started = time.monotonic()
                    try:
                        if configured and hasattr(engine, "recognize_many"):
                            for worker_lane in getattr(engine, "engines", [engine]):
                                worker_lane.worker.region_batch = region_batch
                            values = engine.recognize_many(inputs)
                        elif len(inputs) == 1:
                            values = [engine.recognize(inputs[0])]
                        else:
                            values = engine.recognize_many(inputs)
                        for sha, value in zip(good, values, strict=True):
                            results[sha] = value
                    except UserError:
                        if len(inputs) > 1:
                            # Retry individually on the next iteration; a hanging image
                            # must not turn every other member of its batch into a failure.
                            engine.batch_limit = 1
                        else:
                            results[good[0]] = {"error": True}
                    compute_finished = time.monotonic()
            published, failed = 0, 0
            with self.lock:
                with self.db.connect() as conn:
                    for sha, result in results.items():
                        if result.get("cancelled") or (result.get("error") and self.stop.is_set()):
                            continue
                        # Pause/shutdown flush already computed results. Deletion and
                        # replacement still invalidate publication, including all vectors.
                        exists = conn.execute(
                            "SELECT 1 FROM indexable_media_refs WHERE sha256=? AND kind='photo' "
                            "AND status='ready' LIMIT 1",
                            (sha,),
                        ).fetchone()
                        assignment = conn.execute(
                            "SELECT 1 FROM ocr_work WHERE version=? AND sha256=? "
                            "AND claim_token=? AND state='running'",
                            (engine.version, sha, tokens[sha]),
                        ).fetchone()
                        if self.ocr is not owner or not exists or not assignment:
                            continue
                        error = bool(result.get("error"))
                        text = "" if error else result["text"]
                        conn.execute(
                            "INSERT INTO ocr_cache(sha256,version,state,text,"
                            "text_normalized,confidence,error) "
                            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(sha256,version) DO UPDATE SET "
                            "state=excluded.state,text=excluded.text,text_normalized=excluded.text_normalized,"
                            "confidence=excluded.confidence,error=excluded.error",
                            (
                                sha,
                                engine.version,
                                "failed" if error else "ready",
                                text,
                                normalize_text(text),
                                None if error else result["confidence"],
                                "Не удалось распознать изображение. Повторите обработку."
                                if error
                                else None,
                            ),
                        )
                        published += 1
                        failed += int(error)
                # Commit before updating progress/rates; failures never fabricate a checkpoint.
                if published:
                    self.ocr_completed += published
                    self.ocr_timings = {
                        "select_seconds": selected - began,
                        "read_seconds": read_finished - selected,
                        "compute_wait_seconds": compute_started - read_finished,
                        "inference_seconds": compute_finished - compute_started,
                        "publish_seconds": time.monotonic() - compute_finished,
                        "pipeline": dict(getattr(engine, "last_timings", {})),
                    }
                    phases = {
                        k: v for k, v in self.ocr_timings.items() if isinstance(v, (int, float))
                    }
                    for result in results.values():
                        for key, value in result.get("timings", {}).items():
                            phases[key] = phases.get(key, 0) + value
                    if len(inputs) == 1:
                        phases.update(getattr(engine, "last_timings", {}))
                    if not hasattr(engine, "engines"):
                        phases.update(getattr(getattr(engine, "worker", None), "last_stats", {}))
                    self.metrics.record(
                        "ocr",
                        self._stage_identity("ocr"),
                        began,
                        time.monotonic(),
                        units=published,
                        errors=failed,
                        phases=phases,
                    )
                    snapshot = self.metrics.snapshot("ocr", self._stage_identity("ocr"), 1)
                    rate = snapshot["units_per_minute"]
                    with self.db.connect() as conn:
                        record_rate(
                            conn,
                            "ocr",
                            owner,
                            published,
                            published * 60 / rate if rate else time.monotonic() - began,
                        )
            return True
        finally:
            with self.lock:
                self.ocr_claims.difference_update(shas)
                self._set_active(lane, False)
                self.ocr_queue.release(engine.version, assignments)
                abandoned = self.ocr is not owner or self.stop.is_set()
            if abandoned:
                self._unload_ocr_lane(engine)

    def _cpu_ocr_loop(self):
        def step():
            with self.lock:
                peer = getattr(self.ocr, "cpu_peer", None)
            return self._ocr_run(peer) if peer is not None else False

        self._stage_loop("ocr_cpu", step, peer=True)

    def _relieve_memory(self, stage):
        with self.lock:
            if stage in {"ocr", "ocr_cpu"} and stage not in self.active_stages:
                engine = getattr(self.ocr, "cpu_peer", None) if stage == "ocr_cpu" else self.ocr
                self._unload_ocr_lane(engine)
            elif stage == "clip" and self.clip and "clip" not in self.active_stages:
                if hasattr(self.clip, "unload_idle"):
                    self.clip.unload_idle(idle_seconds=0)
                elif hasattr(self.clip, "unload_index"):
                    self.clip.unload_index()
            elif stage == "ocr_dense" and self.semantic.encoder:
                release = getattr(self.semantic.encoder, "unload_index_if_idle", None)
                if release:
                    release()

    def _index_cycle(self):
        # Synchronous CLI callers keep deterministic completion. Background workers
        # have independent queues and share fair, interactive-priority device gates.
        worked = self._ocr_run()
        worked |= self._image_batch()
        worked |= self._ocr_embeddings()
        return worked

    def _loop(self):
        self._stage_loop("ocr", self._ocr_run)

    def _stage_loop(self, stage, operation, *, peer=False):
        last_cleanup = 0.0
        while not self.stop.is_set():
            try:
                if stage == "ocr" and time.monotonic() - last_cleanup > 30:
                    self.cleanup()
                    last_cleanup = time.monotonic()
                with self.lock:
                    busy = self.preparation and self.preparation.is_alive()
                    available = not busy and (
                        getattr(self.ocr, "cpu_peer", None)
                        if peer
                        else self.ocr
                        if stage == "ocr"
                        else self.clip
                        if stage == "clip"
                        else self.ocr and self.semantic.encoder
                    )
                if available:
                    if (
                        self.resources.snapshot()["rss_bytes"]
                        > self.db.settings.memory_limit_mib * 1024**2
                    ):
                        self.resource_error = (
                            "Достигнут предел RAM: медиа ожидает освобождения памяти."
                        )
                        self._relieve_memory(stage)
                    else:
                        self.resource_error = None
                        if operation():
                            continue
            except Exception:
                self.resource_error = (
                    "Медиа-индекс остановился на ошибке. Проверьте источник и повторите."
                )
            if stage == "clip" and self.clip and hasattr(self.clip, "unload_index"):
                self.clip.unload_index()
                if hasattr(self.clip, "unload_idle"):
                    self.clip.unload_idle(idle_seconds=self.db.settings.idle_unload_seconds)
            # A bounded wait also covers callers sharing the legacy wake Event.
            self.stop.wait(0.25)

    def _ocr_embeddings(self):
        began = time.monotonic()
        encoder = self.semantic.encoder
        engine = self.ocr
        if not encoder or not engine or self.semantic.preparing_encoder.is_set():
            self.ocr_dense_waiting = False
            return False
        failure_identity = hashlib.sha256(
            serialize(["ocr", engine.version, encoder.space_id]).encode()
        ).hexdigest()
        with self.db.connect() as conn:
            if conn.execute("SELECT ocr_dense_paused FROM media_state WHERE id=1").fetchone()[0]:
                self.ocr_dense_waiting = False
                return False
            row = conn.execute(
                "SELECT o.sha256,o.text,(SELECT c.text_batch FROM "
                "indexable_media_refs r JOIN chats c "
                "ON c.id=r.chat_id WHERE r.sha256=o.sha256 AND r.status='ready' "
                "AND r.kind='photo' AND c.ocr_dense_paused=0 "
                "ORDER BY c.id LIMIT 1) AS text_batch "
                "FROM ocr_cache o WHERE o.version=? AND o.state='ready' "
                "AND o.text<>'' AND EXISTS (SELECT 1 FROM "
                "indexable_media_refs r WHERE r.sha256=o.sha256 "
                "AND r.kind='photo' AND r.status='ready' AND EXISTS "
                "(SELECT 1 FROM chats c WHERE c.id=r.chat_id AND c.ocr_dense_paused=0 "
                ")) AND NOT EXISTS "
                "(SELECT 1 FROM media_embeddings e WHERE e.sha256=o.sha256 AND e.kind='ocr' "
                "AND e.space_id=? AND e.ocr_version=?) AND NOT EXISTS "
                "(SELECT 1 FROM media_failures f WHERE f.sha256=o.sha256 AND f.space_id=?) LIMIT 1",
                (engine.version, encoder.space_id, engine.version, failure_identity),
            ).fetchone()
        if not row:
            self.ocr_dense_waiting = False
            return False
        self.ocr_dense_waiting = True
        from telegram_search.search.chunks import ChunkBuilder, SourceMessage

        chunks = list(
            ChunkBuilder(encoder.tokenizer, max_messages=1, overlap=0).build(
                [SourceMessage(row["sha256"], 1, 0, "OCR", row["text"])], 1, encoder.space_id
            )
        )
        ids = {
            chunk.id: hashlib.sha256(serialize([engine.version, chunk.id]).encode()).hexdigest()
            for chunk in chunks
        }
        self._register_space(encoder.space_id, encoder.spec.dimension, "ocr")
        with self.lock:
            self.ocr_staging_ids.update(ids.values())
        published = False
        self._set_active("ocr_dense", True)
        try:
            start = 0
            batch_size = indexing_batch_size(
                encoder, row["text_batch"] or self.db.settings.embedding_batch
            )
            while start < len(chunks):
                batch = chunks[start : start + batch_size]
                with self.lock, self.db.connect() as conn:
                    if not self._ocr_guard(conn, row["sha256"], encoder, engine):
                        return True
                try:
                    embeddings = encoder.encode_text([chunk.text for chunk in batch], "passage")
                except Exception as exc:
                    if memory_exhausted(exc) and len(batch) > 1:
                        batch_size = backoff_indexing_batch(encoder, len(batch))
                        continue
                    raise
                # Publish all parts together: no searchable partial OCR on a failed batch.
                with self.lock, self.db.connect() as conn:
                    if not self._ocr_guard(conn, row["sha256"], encoder, engine):
                        return True
                    self.semantic._vector_store().upsert(
                        encoder.space_id,
                        encoder.spec.dimension,
                        [
                            {
                                "id": ids[chunk.id],
                                "chat_id": row["sha256"],
                                "utc_day": "media",
                                "generation": 1,
                            }
                            for chunk in batch
                        ],
                        embeddings,
                    )
                start += len(batch)
            with self.lock, self.db.connect() as conn:
                if self._ocr_guard(conn, row["sha256"], encoder, engine):
                    conn.execute(
                        "INSERT OR IGNORE INTO media_spaces VALUES(?,?,?)",
                        (encoder.space_id, encoder.spec.dimension, "ocr"),
                    )
                    conn.executemany(
                        "INSERT OR IGNORE INTO media_embeddings"
                        "(id,sha256,space_id,kind,ocr_version,ordinal,char_start,char_end) "
                        "VALUES(?,?,?,'ocr',?,?,?,?)",
                        [
                            (
                                ids[chunk.id],
                                row["sha256"],
                                encoder.space_id,
                                engine.version,
                                ordinal,
                                chunk.parts[0].start,
                                chunk.parts[0].end,
                            )
                            for ordinal, chunk in enumerate(chunks)
                        ],
                    )
                    published = True
        except Exception:
            with self.lock, self.db.connect() as conn:
                current = self._ocr_guard(conn, row["sha256"], encoder, engine)
            if current:
                self._failure(
                    row["sha256"],
                    failure_identity,
                    "Не удалось построить смысловой OCR-индекс.",
                    kind="ocr_dense",
                )
        finally:
            with self.lock:
                self.ocr_staging_ids.difference_update(ids.values())
                self._set_active("ocr_dense", False)
            if published:
                self.metrics.record(
                    "ocr_dense",
                    self._stage_identity("ocr_dense"),
                    began,
                    time.monotonic(),
                    phases=getattr(encoder, "last_timings", {}),
                )
        return True

    def _ocr_guard(self, conn, sha, encoder, engine):
        state = conn.execute(
            "SELECT paused,enabled,active_space_id FROM semantic_state WHERE id=1"
        ).fetchone()
        return (
            self.semantic.encoder is encoder
            and self.ocr is engine
            and self._current(conn, sha, kind="ocr_dense")
            and not self.semantic.preparing_encoder.is_set()
            and state["enabled"]
            and state["active_space_id"] == encoder.space_id
            and conn.execute(
                "SELECT 1 FROM indexable_media_refs r JOIN chats c ON c.id=r.chat_id "
                "WHERE r.sha256=? AND r.kind='photo' AND r.status='ready' "
                "AND c.ocr_dense_paused=0 LIMIT 1",
                (sha,),
            ).fetchone()
            is not None
        )

    def _failure(self, sha, space, error, *, kind="images"):
        with self.lock, self.db.connect() as conn:
            if self._current(conn, sha, kind=kind):
                conn.execute(
                    "INSERT INTO media_failures VALUES(?,?,?) ON CONFLICT DO NOTHING",
                    (sha, space, error),
                )

    def cleanup(self, *, compact=False):
        with self.lock, self.db.connect() as conn:
            spaces = [dict(row) for row in conn.execute("SELECT * FROM media_spaces")]
            if spaces and not self.semantic.available:
                raise UserError("Для очистки медиа-векторов установите CPU runtime semantic.")
            tombstones = [
                row[0] for row in conn.execute("SELECT sha256 FROM media_vector_cleanup LIMIT 100")
            ]
            for sha in tombstones:
                if (
                    spaces
                    and not conn.execute(
                        "SELECT 1 FROM media_blobs WHERE sha256=?", (sha,)
                    ).fetchone()
                ):
                    self.semantic._vector_store().delete_chat(spaces, sha)
                conn.execute("DELETE FROM media_vector_cleanup WHERE sha256=?", (sha,))
            if compact and spaces:
                if self.ocr:
                    conn.execute("DELETE FROM ocr_cache WHERE version<>?", (self.ocr.version,))
                    conn.execute(
                        "DELETE FROM media_embeddings WHERE kind='ocr' AND ocr_version<>?",
                        (self.ocr.version,),
                    )
                if self.clip:
                    conn.execute(
                        "DELETE FROM media_embeddings WHERE kind='image' AND space_id<>?",
                        (self.clip.space_id,),
                    )
                if self.semantic.encoder:
                    conn.execute(
                        "DELETE FROM media_embeddings WHERE kind='ocr' AND space_id<>?",
                        (self.semantic.encoder.space_id,),
                    )

                def eligible(ids):
                    placeholders = ",".join("?" for _ in ids)
                    return self.ocr_staging_ids | {
                        row[0]
                        for row in conn.execute(
                            f"SELECT id FROM media_embeddings WHERE id IN ({placeholders})", ids
                        )
                    }

                for space in spaces:
                    self.semantic._vector_store().prune_media(
                        space["id"], space["dimension"], eligible
                    )
                self.semantic._vector_store().compact(spaces)

    def _register_space(self, space, dimension, kind):
        # Commit recovery metadata before any non-transactional Lance write.
        with self.lock, self.db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO media_spaces VALUES(?,?,?)", (space, dimension, kind)
            )

    def shutdown(self):
        self.stop.set()
        self.wake.set()
        # Publish completed replies, then interrupt any hanging child immediately.
        if self.background:
            self.background.join(timeout=2)
        if self.ocr_background:
            self.ocr_background.join(timeout=2)
        if self.ocr and hasattr(self.ocr, "unload"):
            self.ocr.unload()
        if self.background:
            self.background.join()
        if self.ocr_background:
            self.ocr_background.join()
        for worker in (self.image_background, self.semantic_background):
            if worker:
                worker.join()
        if self.preparation:
            self.preparation.join()
        self.image_prefetch.close()
        if self.clip:
            self.clip.unload()
        if self.ocr and hasattr(self.ocr, "unload"):
            self.ocr.unload()
