"""Separate preparation role: no index, generation or active-space mutations."""

import threading
import time

from telegram_search.config.model_registry import rerank_spec
from telegram_search.inference.bundles import BundleStore
from telegram_search.shared.errors import UserError


class RerankService:
    def __init__(self, db):
        self.db = db
        self.spec = rerank_spec()
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

    def _new_encoder(self):
        from telegram_search.inference.giga import GigaEncoder

        settings = self.db.settings
        return GigaEncoder(
            self.spec,
            BundleStore(self.db.workspace).verify(self.spec),
            threads=settings.cpu_threads,
            device=settings.model_device("e5", query=True),
            search_device=settings.model_device("e5", query=True),
            gpu_device_id=settings.gpu_device_id,
            gpu_memory_limit_mib=settings.gpu_memory_limit_mib,
        )

    def get_encoder(self):
        settings = self.db.settings
        key = (
            settings.model_device("e5", query=True),
            settings.cpu_threads,
            settings.gpu_device_id,
            settings.gpu_memory_limit_mib,
        )
        with self.db.connect() as conn:
            state = conn.execute("SELECT * FROM rerank_state WHERE id=1").fetchone()
        if state["preparation_state"] in {"preparing", "downloading"} or (
            state["manifest_id"] != self.spec.identity
        ):
            raise UserError("Giga не подготовлена. Базовый порядок сохранён.")
        if self.encoder_settings != key or self.encoder is None:
            if self.encoder:
                self.encoder.unload()
            self.encoder = self._new_encoder()
            self.encoder_settings = key
        return self.encoder

    def status(self):
        with self.db.connect() as conn:
            state = dict(conn.execute("SELECT * FROM rerank_state WHERE id=1").fetchone())
        return {
            **state,
            "enabled": self.db.settings.giga_rerank_enabled,
            "ready": state["manifest_id"] == self.spec.identity
            and state["preparation_state"] not in {"preparing", "downloading"},
            "model_id": self.spec.model_id,
            "download_bytes": self.spec.download_bytes,
            "path": str(BundleStore(self.db.workspace).path(self.spec)),
            "device": self.db.settings.model_device("e5", query=True),
            "backend": self.encoder.backend_info() if self.encoder else None,
        }

    def prepare(self, *, offline=False):
        with self.lock:
            if self.preparation and self.preparation.is_alive():
                raise UserError("Подготовка Giga уже выполняется.")
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE rerank_state SET preparation_state='downloading',error=NULL,"
                    "download_completed_bytes=0,download_total_bytes=? WHERE id=1",
                    (self.spec.download_bytes,),
                )
            self.preparation = threading.Thread(
                target=self._prepare, args=(offline,), name="rerank-prepare", daemon=True
            )
            self.preparation.start()
        return self.status()

    def _prepare(self, offline):
        candidate = None

        def progress(event):
            if self.stop.is_set():
                raise UserError("Подготовка модели остановлена.")
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE rerank_state SET download_completed_bytes=? WHERE id=1",
                    (event["completed_bytes"],),
                )

        try:
            BundleStore(self.db.workspace).prepare(
                self.spec, offline=offline, repair=True, progress=progress
            )
            with self.db.connect() as conn:
                conn.execute("UPDATE rerank_state SET preparation_state='preparing' WHERE id=1")
            with self.lock:
                if self.encoder:
                    self.encoder.unload()
                if self.stop.is_set():
                    raise UserError("Подготовка модели остановлена.")
            candidate = self._new_encoder()
            self.check_memory(candidate)
            candidate.encode_text(
                ["Архив сообщений и поиск фотографии."], "passage", interactive=True
            )
            candidate.encode_text(["Найти фотографию"], "query", interactive=True)
            if self.stop.is_set():
                raise UserError("Подготовка модели остановлена.")
            with self.lock, self.db.connect() as conn:
                if self.encoder:
                    self.encoder.unload()
                self.encoder = None
                self.encoder_settings = None
                conn.execute(
                    "UPDATE rerank_state SET preparation_state='ready',error=NULL,"
                    "manifest_id=? WHERE id=1",
                    (self.spec.identity,),
                )
        except Exception as exc:
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE rerank_state SET preparation_state='failed',error=? WHERE id=1",
                    (
                        str(exc)
                        if isinstance(exc, UserError)
                        else "Не удалось подготовить Giga. Базовый поиск доступен.",
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

        from telegram_search.inference.providers import CPU

        if encoder.session is not None or encoder.query_session is not None:
            return
        factor = 2 if encoder.execution.provider == CPU else 1
        needed = self.spec.download_bytes * factor
        budget = self.db.settings.memory_limit_mib * 1024**2
        if (
            needed > psutil.virtual_memory().available
            or psutil.Process().memory_info().rss + needed > budget
        ):
            raise UserError("Недостаточно памяти для Giga. Базовый порядок сохранён.")
