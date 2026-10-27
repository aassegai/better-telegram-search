"""Separate preparation role: no index, generation or active-space mutations."""

import threading
import time

from telegram_search.config.model_registry import rerank_registry, rerank_spec
from telegram_search.inference.bundles import BundleStore
from telegram_search.shared.errors import UserError


class RerankService:
    def __init__(self, db):
        self.db = db
        self.lock = threading.RLock()
        self.preparation = None
        self.encoder = None
        self.encoder_settings = None
        self.stop = threading.Event()
        with db.connect() as conn:
            conn.execute(
                "UPDATE rerank_state SET preparation_state='interrupted' "
                "WHERE preparation_state IN ('preparing','downloading')"
            )
        self.background = threading.Thread(target=self._idle, name="rerank-idle", daemon=True)
        self.background.start()

    @property
    def spec(self):
        return rerank_spec(self.db.settings.rerank_profile, self.db.workspace)

    def _new_encoder(self, spec=None):
        spec = spec or self.spec
        if spec.manifest.get("unavailable"):
            raise UserError("The registered reranker is unavailable. Select or register a model.")
        if spec.manifest.get("runtime") == "python-adapter":
            from telegram_search.model_adapters.reranker import AdapterReranker

            return AdapterReranker(spec, self.db.workspace, self.db.settings, cancelled=self.stop)
        from telegram_search.inference.giga import GigaEncoder
        from telegram_search.inference.jina import JinaReranker

        settings = self.db.settings
        spec = spec or self.spec
        factory = (
            JinaReranker if spec.manifest.get("adapter") == "onnx-pairwise-v1" else GigaEncoder
        )
        return factory(
            spec,
            BundleStore(self.db.workspace).verify(spec),
            threads=settings.cpu_threads,
            device=settings.model_device("e5", query=True),
            search_device=settings.model_device("e5", query=True),
            gpu_device_id=settings.gpu_device_id,
            gpu_memory_limit_mib=settings.gpu_memory_limit_mib,
        )

    def get_encoder(self):
        settings = self.db.settings
        key = (
            self.spec.identity,
            settings.model_device("e5", query=True),
            settings.cpu_threads,
            settings.gpu_device_id,
            settings.gpu_memory_limit_mib,
        )
        with self.db.connect() as conn:
            state = conn.execute("SELECT * FROM rerank_state WHERE id=1").fetchone()
        if state["preparation_state"] != "ready" or (state["manifest_id"] != self.spec.identity):
            raise UserError("Модель переранжирования не подготовлена. Базовый порядок сохранён.")
        if self.encoder_settings != key or self.encoder is None:
            if self.encoder:
                self.encoder.unload()
            self.encoder = self._new_encoder()
            self.encoder_settings = key
        return self.encoder

    def status(self):
        settings = self.db.settings
        spec = rerank_spec(settings.rerank_profile, self.db.workspace)
        profiles = rerank_registry(self.db.workspace)
        profiles.setdefault(spec.profile, spec)
        custom = spec.manifest.get("runtime") == "python-adapter"
        encoder = self.encoder
        with self.db.connect() as conn:
            state = dict(conn.execute("SELECT * FROM rerank_state WHERE id=1").fetchone())
        current = state["manifest_id"] == spec.identity
        if not current and state["preparation_state"] not in {"preparing", "downloading"}:
            state.update(preparation_state="idle", error=None, download_completed_bytes=0)
        return {
            **state,
            "enabled": settings.giga_rerank_enabled,
            "ready": current and state["preparation_state"] == "ready",
            "model_id": spec.model_id,
            "profile": spec.profile,
            "profiles": [
                {
                    "profile": value.profile,
                    "model_id": value.model_id,
                    "license": value.manifest.get("license"),
                    "download_bytes": value.download_bytes,
                    "revision": value.revision,
                    "available": not value.manifest.get("unavailable", False),
                }
                for value in profiles.values()
            ],
            "license": spec.manifest.get("license"),
            "source": "custom" if custom else spec.manifest.get("source"),
            "download_bytes": spec.download_bytes,
            "path": str(self.db.workspace / "models" / "custom-adapters" / spec.profile[7:])
            if custom
            else str(BundleStore(self.db.workspace).path(spec)),
            "device": settings.model_device("e5", query=True),
            "backend": encoder.backend_info()
            if encoder and encoder.spec.identity == spec.identity
            else None,
        }

    def prepare(self, *, offline=False):
        with self.lock:
            if self.preparation and self.preparation.is_alive():
                raise UserError("Подготовка модели переранжирования уже выполняется.")
            spec = self.spec
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE rerank_state SET preparation_state='downloading',error=NULL,"
                    "download_completed_bytes=0,download_total_bytes=?,manifest_id=? WHERE id=1",
                    (spec.download_bytes, spec.identity),
                )
            self.preparation = threading.Thread(
                target=self._prepare, args=(offline, spec), name="rerank-prepare", daemon=True
            )
            self.preparation.start()
        return self.status()

    def _prepare(self, offline, spec):
        candidate = None

        def progress(event):
            if self.stop.is_set() or self.spec.identity != spec.identity:
                raise UserError("Подготовка модели остановлена.")
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE rerank_state SET download_completed_bytes=? WHERE id=1",
                    (event["completed_bytes"],),
                )

        try:
            if spec.manifest.get("runtime") != "python-adapter":
                BundleStore(self.db.workspace).prepare(
                    spec, offline=offline, repair=True, progress=progress
                )
            with self.db.connect() as conn:
                conn.execute("UPDATE rerank_state SET preparation_state='preparing' WHERE id=1")
            with self.lock:
                if self.encoder:
                    self.encoder.unload()
                if self.stop.is_set():
                    raise UserError("Подготовка модели остановлена.")
            candidate = self._new_encoder(spec)
            self.check_memory(candidate)
            if hasattr(candidate, "score_pairs"):
                candidate.score_pairs(
                    "Find a photograph",
                    [dict(candidate_id="smoke", text="A photograph in the archive.")],
                )
            else:
                candidate.encode_text(
                    ["Архив сообщений и поиск фотографии."], "passage", interactive=True
                )
                candidate.encode_text(["Найти фотографию"], "query", interactive=True)
            if self.stop.is_set() or self.spec.identity != spec.identity:
                raise UserError("Подготовка модели остановлена.")
            with self.lock, self.db.connect() as conn:
                if self.encoder:
                    self.encoder.unload()
                self.encoder = None
                self.encoder_settings = None
                conn.execute(
                    "UPDATE rerank_state SET preparation_state='ready',error=NULL,"
                    "manifest_id=? WHERE id=1",
                    (spec.identity,),
                )
        except Exception as exc:
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE rerank_state SET preparation_state='failed',error=? WHERE id=1",
                    (
                        str(exc)
                        if isinstance(exc, UserError)
                        else "Не удалось подготовить переранжирование. Базовый поиск доступен.",
                    ),
                )
        finally:
            if candidate:
                candidate.unload()

    def _idle(self):
        while not self.stop.wait(5):
            if self.lock.acquire(blocking=False):
                try:
                    if self.encoder:
                        self.encoder.unload_idle(
                            query_last_used=getattr(self, "last_used", 0),
                            idle_seconds=self.db.settings.idle_unload_seconds,
                            index_paused=True,
                        )
                finally:
                    self.lock.release()

    def shutdown(self):
        self.stop.set()
        self.background.join()
        if self.preparation:
            self.preparation.join()
        with self.lock:
            if self.encoder:
                self.encoder.unload()
            self.encoder = None

    def mark_used(self):
        self.last_used = time.monotonic()

    def check_memory(self, encoder):
        import psutil

        from telegram_search.inference.memory import process_tree_rss
        from telegram_search.inference.providers import CPU

        if encoder.session is not None or encoder.query_session is not None:
            return
        factor = 2 if encoder.execution.provider == CPU else 1
        needed = max(
            encoder.spec.download_bytes * factor,
            encoder.spec.manifest.get("adapter_package_bytes", 0) * factor,
            64 * 1024**2 if encoder.spec.manifest.get("runtime") == "python-adapter" else 0,
        )
        budget = self.db.settings.memory_limit_mib * 1024**2
        if needed > psutil.virtual_memory().available or process_tree_rss() + needed > budget:
            raise UserError("Недостаточно памяти для переранжирования. Базовый порядок сохранён.")
