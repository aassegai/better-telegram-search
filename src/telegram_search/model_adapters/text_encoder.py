"""Custom text adapters use the existing chunk/generation/vector pipeline."""

import hashlib
import json
import threading
import time
from collections import OrderedDict

import numpy as np

from telegram_search.inference.compatibility import check_vectors
from telegram_search.inference.providers import CPU
from telegram_search.inference.resources import compute_gate
from telegram_search.model_adapters.client import AdapterContext, AdapterWorker
from telegram_search.model_adapters.execution import AdapterExecution
from telegram_search.model_adapters.store import AdapterStore
from telegram_search.search.execution import checkpoint
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize


class AdapterTokenCounter:
    def __init__(self, encoder):
        self.encoder = encoder
        self.lock = threading.Lock()
        self.worker = None
        self.cache = OrderedDict()
        self.cancelled = threading.Event()
        self.closing = threading.Event()

    def count(self, text):
        while not self.lock.acquire(timeout=0.05):
            checkpoint()
            if self.closing.is_set():
                raise UserError("Custom tokenizer is restarting.")
        try:
            checkpoint()
            self.cancelled.clear()
            if self.closing.is_set():
                raise UserError("Custom tokenizer is restarting.")
            if text in self.cache:
                self.cache.move_to_end(text)
                return self.cache[text]
            if self.worker is None or self.worker.closed.is_set():
                execution = self.encoder.query_execution
                if "cpu" in self.encoder.manifest.data["devices"]:
                    execution = AdapterExecution("cpu", self.encoder.settings, {"cpu"})
                self.worker = self.encoder._worker(execution, load=False, cancelled=self.cancelled)
            result = self.worker.request(
                "count_tokens", {"texts": [text]}, cancelled=self.cancelled
            )[0]
            # Bound both entries and retained text bytes, including huge messages.
            if len(text) <= 8192:
                self.cache[text] = result
                while len(self.cache) > 512:
                    self.cache.popitem(last=False)
            return result
        finally:
            self.lock.release()

    def close(self):
        self.closing.set()
        self.cancelled.set()
        try:
            if self.worker:
                self.worker.close()
            with self.lock:
                if self.worker:
                    self.worker.close()
                self.worker = None
        finally:
            self.closing.clear()


class AdapterTextEncoder:
    @property
    def index_batch_limit(self):
        return self._index_batch_limit

    @index_batch_limit.setter
    def index_batch_limit(self, value):
        maximum = self.manifest.data["input_contract"]["max_batch"]
        if value is not None and (type(value) is not int or value < 1):
            raise UserError("Invalid custom model batch limit.")
        self._index_batch_limit = maximum if value is None else min(value, maximum)

    def __init__(self, spec, workspace, settings):
        self.spec, self.settings = spec, settings
        self.store = AdapterStore(workspace)
        self.manifest, self.package, self.registration = self.store.get(
            spec.manifest["adapter_identity"]
        )
        if self.registration["environment"] != spec.manifest["adapter_environment"]:
            raise UserError(
                "The custom model environment changed. Validate and rebuild explicitly."
            )
        devices = self.manifest.data["devices"]
        self.execution = AdapterExecution(settings.model_device("e5"), settings, devices)
        self.query_execution = AdapterExecution(
            settings.model_device("e5", query=True), settings, devices
        )
        self.runtime = {
            "backend": "python-adapter",
            "version": "1",
            "dtype": "float32",
            "environment": self.registration["environment"],
        }
        self.space_manifest = {"model": spec.manifest, "runtime": self.runtime}
        self.space_id = hashlib.sha256(serialize(self.space_manifest).encode()).hexdigest()
        self.session = self.query_session = None
        self.condition = threading.Condition()
        self.active = 0
        self.encoding = self.suspended = False
        self.last_index_used = time.monotonic()
        self.index_batch_limit = self.manifest.data["input_contract"]["max_batch"]
        self.threads = settings.cpu_threads
        self.tokenizer = AdapterTokenCounter(self)
        self.lanes = {"query": threading.Lock(), "passage": threading.Lock()}
        self.last_timings = {}

    def adopt_space(self, value):
        manifest = json.loads(value) if isinstance(value, str) else value
        if manifest != self.space_manifest:
            raise UserError("The adapter or its environment differs from the stored text index.")

    def _worker(self, execution, *, load=True, cancelled=None):
        worker = AdapterWorker(
            self.manifest,
            self.package,
            self.registration["python"],
            self.store.runner(),
            context=AdapterContext(
                device="cpu" if execution.provider == CPU else "gpu",
                gpu_device_id=execution.device_id,
                gpu_memory_limit_mib=execution.memory_limit_mib,
                cpu_threads=self.threads,
                batch_size=self.index_batch_limit,
            ),
            cancelled=cancelled,
        )
        try:
            if (
                worker.request("info", cancelled=cancelled)["environment"]
                != self.registration["environment"]
            ):
                raise UserError("The adapter environment changed. Validate and rebuild explicitly.")
            worker.request("validate", cancelled=cancelled)
            if load:
                worker.request("load", timeout=180, cancelled=cancelled)
            return worker
        except BaseException:
            worker.close()
            raise

    def _load(self, *, query):
        attribute = "query_session" if query else "session"
        execution = self.query_execution if query else self.execution
        worker = getattr(self, attribute)
        if worker is not None and not worker.closed.is_set():
            return worker
        try:
            worker = self._worker(execution)
            if execution.provider != CPU:
                self._check_gpu(worker)
        except UserError:
            if worker is not None:
                worker.close()
            if execution.device != "auto" or "cpu" not in self.manifest.data["devices"]:
                raise
            execution.provider = CPU
            execution.warning = "Custom GPU unavailable; Auto uses CPU."
            worker = self._worker(execution)
        setattr(self, attribute, worker)
        return worker

    def _check_gpu(self, worker):
        cpu_execution = AdapterExecution("cpu", self.settings, self.manifest.data["devices"])
        with self._worker(cpu_execution) as cpu:
            for purpose in ("query", "document"):
                prefix = self.manifest.data["preprocessing"][f"{purpose}_prefix"]
                payload = {
                    "operation": purpose,
                    "items": [
                        {"id": str(i), "text": prefix + value}
                        for i, value in enumerate(
                            ["Find next week's meeting.", "Архив сообщений."][
                                : self.index_batch_limit
                            ]
                        )
                    ],
                }
                expected = np.asarray(
                    cpu.request("encode_text", payload)["vectors"], dtype=np.float32
                )
                actual = np.asarray(
                    worker.request("encode_text", payload)["vectors"], dtype=np.float32
                )
                check_vectors(expected, actual)

    def prepare_text(self, texts, purpose):
        if purpose not in {"query", "passage"} or not 1 <= len(texts) <= self.index_batch_limit:
            raise UserError("Invalid custom text model batch.")
        began = time.perf_counter()
        prefix = self.spec.manifest[f"{purpose}_prefix"]
        prepared = [prefix + text for text in texts]
        lengths = [self.tokenizer.count(text) for text in prepared]
        limit = self.spec.manifest["query_max_tokens" if purpose == "query" else "chunk_max_tokens"]
        if max(lengths) > limit:
            raise UserError("Text exceeds the custom model token budget.")
        return (
            {
                "operation": "query" if purpose == "query" else "document",
                "items": [{"id": str(i), "text": value} for i, value in enumerate(prepared)],
            },
            len(texts),
            purpose,
            time.perf_counter() - began,
            self,
            max(lengths),
        )

    def encode_text(self, texts, purpose, *, interactive=False, _prepared=None):
        if not texts:
            return np.empty((0, self.spec.dimension), dtype=np.float32)
        if purpose not in self.lanes:
            raise UserError("Invalid custom text model operation.")
        lane = self.lanes[purpose]
        while not lane.acquire(timeout=0.05):
            checkpoint()
        try:
            with self.condition:
                if self.suspended:
                    raise UserError("The custom model is suspended for model preparation.")
                self.active += 1
                self.encoding = True
            try:
                payload, count, prepared_purpose, preprocessing, owner, length = (
                    _prepared or self.prepare_text(texts, purpose)
                )
                if owner is not self or count != len(texts) or prepared_purpose != purpose:
                    raise UserError("Prepared batch belongs to another custom model.")
                execution = self.query_execution if purpose == "query" else self.execution
                before_gate = time.perf_counter()
                with compute_gate(execution).slot(interactive=interactive):
                    granted = time.perf_counter()
                    worker = self._load(query=purpose == "query")
                    began = time.perf_counter()
                    try:
                        result = worker.request("encode_text", payload)
                    except BaseException:
                        if worker.closed.is_set():
                            setattr(
                                self, "query_session" if purpose == "query" else "session", None
                            )
                        raise
                    finished = time.perf_counter()
                values = np.asarray(result["vectors"], dtype=np.float32)
                if purpose == "passage":
                    self.last_timings = {
                        "preprocess_seconds": preprocessing,
                        "compute_wait_seconds": granted - before_gate,
                        "model_load_seconds": began - granted,
                        "inference_seconds": finished - began,
                        "batch_size": len(texts),
                        "sequence_length": length,
                        "prefetched": int(_prepared is not None),
                    }
                    self.last_index_used = time.monotonic()
                return values
            finally:
                with self.condition:
                    self.active -= 1
                    self.encoding = bool(self.active)
                    self.condition.notify_all()
        finally:
            lane.release()

    def backend_info(self):
        return {
            **self.runtime,
            **self.execution.info(),
            "model_id": self.spec.model_id,
            "revision": self.spec.revision,
            "embedding_space": self.space_id,
            "dimension": self.spec.dimension,
            "loaded": self.session is not None,
            "query_execution": self.query_execution.info(),
            "query_loaded": self.query_session is not None,
            "threads": self.threads,
            "index_batch_limit": self.index_batch_limit,
            "index_timings": self.last_timings,
        }

    def _detach(self, *, query=True, wait=True):
        with self.condition:
            if not wait and self.active:
                return None
            while self.active:
                self.condition.wait(timeout=0.05)
            workers = [self.session]
            self.session = None
            if query:
                workers.append(self.query_session)
                self.query_session = None
        for worker in workers:
            if worker:
                worker.close()
        return True

    def unload(self):
        self._detach()
        self.tokenizer.close()

    def suspend(self):
        with self.condition:
            self.suspended = True
        self.unload()

    def resume(self):
        with self.condition:
            self.suspended = False
            self.condition.notify_all()

    def unload_index(self):
        self._detach(query=False)

    def unload_index_if_idle(self):
        return bool(self._detach(query=False, wait=False))

    def unload_idle(self, *, query_last_used, idle_seconds, index_paused=False):
        now = time.monotonic()
        all_idle = now - max(query_last_used, self.last_index_used) > idle_seconds
        if all_idle:
            return bool(self._detach(wait=False))
        if index_paused or now - self.last_index_used > 5:
            self._detach(query=False, wait=False)
        return False
