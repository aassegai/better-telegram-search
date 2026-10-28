"""Registered image/query adapters share the guarded media vector pipeline."""

import base64
import hashlib
import importlib.metadata
import io
import threading
import time
from contextlib import contextmanager, nullcontext

import numpy as np
from PIL import Image, ImageOps

from telegram_search.inference.compatibility import check_vectors
from telegram_search.inference.providers import CPU
from telegram_search.inference.resources import compute_gate
from telegram_search.model_adapters.client import AdapterContext, AdapterWorker
from telegram_search.model_adapters.store import AdapterStore
from telegram_search.model_adapters.text_encoder import AdapterExecution
from telegram_search.model_adapters.validation import smoke_requests
from telegram_search.search.execution import (
    SearchCancelled,
    SearchExecution,
    checkpoint,
    current_execution,
    execution_context,
    search_lock,
)
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize


class _StopSignal:
    def __init__(self, external):
        self.external = external if external is not None else threading.Event()
        self.retired = threading.Event()

    def is_set(self):
        return self.external.is_set() or self.retired.is_set()


class AdapterImageEncoder:
    # Keep current + lookahead batches bounded, including JSON/base64 copies.
    prepared_byte_limit = 24 * 1024**2

    def __init__(self, spec, workspace, settings, *, cancelled=None):
        self.spec, self.settings, self.profile = spec, settings, spec.profile
        self.store = AdapterStore(workspace)
        self.manifest, self.package, self.registration = self.store.get(
            spec.manifest["adapter_identity"]
        )
        if self.manifest.role != "image_embedding":
            raise UserError("The custom model is not an image/query adapter.")
        if self.registration["environment"] != spec.manifest["adapter_environment"]:
            raise UserError(
                "The custom model environment changed. Validate and rebuild explicitly."
            )
        devices = self.manifest.data["devices"]
        self.execution = AdapterExecution(settings.model_device("clip"), settings, devices)
        self.query_execution = AdapterExecution(
            settings.model_device("clip", query=True), settings, devices
        )
        self.dimension = spec.dimension
        self.index_batch_limit = self.manifest.data["input_contract"]["max_batch"]
        self.threads = settings.cpu_threads
        self.lock = threading.RLock()
        self.cancelled = _StopSignal(cancelled)
        self.retirement_error = None
        self.sessions = {}
        self.last_used = time.monotonic()
        self.last_timings = {}
        self.space_manifest = {
            "models": {self.profile: spec.manifest},
            "environment": self.registration["environment"],
            "decoder": "pillow-exif-rgb-v1",
            "pillow": importlib.metadata.version("pillow"),
        }
        self.space_id = hashlib.sha256(serialize(self.space_manifest).encode()).hexdigest()

    def compatible_spaces(self):
        return {self.space_id}

    def adopt_space(self, space_id):
        if space_id != self.space_id:
            raise UserError("The image adapter or its environment differs from the stored index.")

    def _worker(self, execution):
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
            cancelled=self.cancelled,
        )
        try:
            if (
                worker.request("info", cancelled=self.cancelled)["environment"]
                != self.registration["environment"]
            ):
                raise UserError("The adapter environment changed. Validate and rebuild explicitly.")
            worker.request("validate", cancelled=self.cancelled)
            worker.request("load", timeout=180, cancelled=self.cancelled)
            return worker
        except BaseException:
            worker.close()
            raise

    def _session(self, kind):
        if self.cancelled.external.is_set():
            raise InterruptedError("Custom image model is stopping.")
        if self.cancelled.retired.is_set():
            raise UserError("The image model changed. Repeat the search.")
        worker = self.sessions.get(kind)
        if worker is not None and not worker.closed.is_set():
            return worker
        execution = self.query_execution if kind == "text" else self.execution
        worker = None
        try:
            worker = self._worker(execution)
            if execution.provider != CPU:
                reference = AdapterExecution("cpu", self.settings, self.manifest.data["devices"])
                with self._worker(reference) as cpu:
                    for operation, payload in smoke_requests(self.manifest):
                        check_vectors(
                            np.asarray(
                                cpu.request(operation, payload, cancelled=self.cancelled)[
                                    "vectors"
                                ],
                                dtype=np.float32,
                            ),
                            np.asarray(
                                worker.request(operation, payload, cancelled=self.cancelled)[
                                    "vectors"
                                ],
                                dtype=np.float32,
                            ),
                        )
        except UserError:
            if worker is not None:
                worker.close()
            if execution.device != "auto" or "cpu" not in self.manifest.data["devices"]:
                raise
            execution.provider = CPU
            execution.warning = "Custom GPU unavailable; Auto uses CPU."
            worker = self._worker(execution)
        except BaseException:
            if worker is not None:
                worker.close()
            raise
        self.sessions[kind] = worker
        return worker

    @contextmanager
    def _compute(self, execution, *, interactive=False):
        # Background/preparation waits must observe MediaService.stop too.
        active = current_execution()
        user_execution = active
        if active is None:
            active = SearchExecution()
            active.cancelled = self.cancelled
            context = execution_context(active)
        else:
            context = nullcontext()
        try:
            with context, compute_gate(execution).slot(interactive=interactive):
                yield
        except SearchCancelled:
            if user_execution is not None and user_execution.cancelled.is_set():
                raise
            if self.cancelled.external.is_set():
                raise InterruptedError("Custom image model is stopping.") from None
            if self.cancelled.retired.is_set():
                raise UserError("The image model changed. Repeat the search.") from None
            raise
        except InterruptedError:
            if self.cancelled.retired.is_set() and not self.cancelled.external.is_set():
                raise UserError("The image model changed. Repeat the search.") from None
            raise

    def check_contract(self):
        try:
            for kind, execution in (("image", self.execution), ("text", self.query_execution)):
                with self._compute(execution, interactive=True), self.lock:
                    worker = self._session(kind)
                    for operation, payload in smoke_requests(self.manifest):
                        worker.request(operation, payload, cancelled=self.cancelled)
        finally:
            self.unload()

    def prepare_images(self, images):
        result = []
        retained = 0
        for data in images:
            checkpoint()
            start = time.perf_counter()
            if not isinstance(data, bytes) or len(data) > 32 * 1024**2:
                raise UserError("Invalid image for custom model (maximum encoded size: 32 MiB).")
            try:
                with Image.open(io.BytesIO(data)) as original:
                    if (
                        not 1 <= original.width <= 16384
                        or not 1 <= original.height <= 16384
                        or original.width * original.height > 16 * 1024**2
                        or original.width * original.height * 3 > self.prepared_byte_limit
                        or retained + original.width * original.height * 3
                        > self.prepared_byte_limit
                    ):
                        raise UserError("The decoded image exceeds the custom model memory budget.")
                    image = ImageOps.exif_transpose(original).convert("RGB")
                    raw = image.tobytes()
                    retained += len(raw)
                    value = {
                        "width": image.width,
                        "height": image.height,
                        "mode": "RGB",
                        "pixels": base64.b64encode(raw).decode("ascii"),
                    }
            except UserError:
                raise
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                raise UserError("Could not safely decode the image for the custom model.") from exc
            result.append((value, time.perf_counter() - start, len(raw), self))
        return result

    @staticmethod
    def prepared_bytes(value):
        return value[2]

    def encode_images(self, images, *, _prepared=None):
        if not 1 <= len(images) <= self.index_batch_limit:
            raise UserError("Image batch exceeds the custom model limit.")
        values = self.prepare_images(images) if _prepared is None else _prepared
        if len(values) != len(images) or any(v[3] is not self for v in values):
            raise UserError("Prepared images belong to another custom model or batch.")
        if sum(v[2] for v in values) > self.prepared_byte_limit:
            raise UserError("Custom image batch exceeds its memory budget; reduce its size.")
        payload = {"images": [{**v[0], "id": str(i)} for i, v in enumerate(values)]}
        prepared = time.perf_counter()
        with self._compute(self.execution), self.lock:
            acquired = time.perf_counter()
            worker = self._session("image")
            loaded = time.perf_counter()
            result = np.asarray(
                worker.request("encode_images", payload, cancelled=self.cancelled)["vectors"],
                dtype=np.float32,
            )
            inferred = time.perf_counter()
            self.last_used = time.monotonic()
        self.last_timings = {
            "preprocess_seconds": sum(v[1] for v in values),
            "compute_wait_seconds": acquired - prepared,
            "model_load_seconds": loaded - acquired,
            "inference_seconds": inferred - loaded,
            "batch_size": len(images),
            "prefetched": int(_prepared is not None),
        }
        return result

    def encode_text(self, texts):
        if not 1 <= len(texts) <= self.index_batch_limit:
            raise UserError("Query batch exceeds the custom image model limit.")
        payload = {"items": [{"id": str(i), "text": t} for i, t in enumerate(texts)]}
        with self._compute(self.query_execution, interactive=True), search_lock(self.lock):
            result = np.asarray(
                self._session("text").request(
                    "encode_image_queries", payload, cancelled=self.cancelled
                )["vectors"],
                dtype=np.float32,
            )
            checkpoint()
            self.last_used = time.monotonic()
            return result

    def unload(self):
        with self.lock:
            errors = []
            for kind, worker in list(self.sessions.items()):
                try:
                    worker.close()
                except BaseException as exc:
                    # Each child gets a cleanup attempt, even when another fails.
                    # Failed native handles remain fenced by AdapterWorker.
                    errors.append(exc)
                else:
                    self.sessions.pop(kind, None)
            if errors:
                raise errors[0]

    def retire(self):
        # Explicit replacement is permanent; idle unload must remain reloadable.
        self.cancelled.retired.set()
        try:
            self.unload()
        except (UserError, InterruptedError, SearchCancelled):
            # AdapterWorker retains failed process handles and fences new workers
            # until cleanup succeeds. This is after commit: do not roll settings back.
            self.retirement_error = "Previous custom image worker cleanup is incomplete."

    def unload_index(self):
        with self.lock:
            worker = self.sessions.pop("image", None)
            if worker:
                worker.close()

    def unload_idle(self, *, idle_seconds):
        if not self.lock.acquire(blocking=False):
            return False
        try:
            if time.monotonic() - self.last_used < idle_seconds or not self.sessions:
                return False
            self.unload()
            return True
        finally:
            self.lock.release()

    def update_threads(self, threads):
        with self.lock:
            self.unload()
            self.threads = threads

    def backend_info(self):
        image, text = self.sessions.get("image"), self.sessions.get("text")
        return {
            "backend": "python-adapter",
            "model_id": self.spec.model_id,
            "embedding_space": self.space_id,
            "dimension": self.dimension,
            **self.execution.info(),
            "loaded": bool(image and not image.closed.is_set()),
            "query_execution": self.query_execution.info(),
            "query_loaded": bool(text and not text.closed.is_set()),
        }
