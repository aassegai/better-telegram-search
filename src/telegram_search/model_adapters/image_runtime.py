"""Bounded RGB and lifecycle shared by registered visual and OCR workers."""

import base64
import hashlib
import importlib.metadata
import io
import threading
import time
from contextlib import contextmanager, nullcontext

from PIL import Image, ImageOps

from telegram_search.inference.providers import CPU
from telegram_search.inference.resources import compute_gate
from telegram_search.model_adapters.client import AdapterContext, AdapterWorker
from telegram_search.model_adapters.execution import AdapterExecution
from telegram_search.model_adapters.store import AdapterStore
from telegram_search.model_adapters.validation import smoke_requests
from telegram_search.search.execution import (
    SearchCancelled,
    SearchExecution,
    checkpoint,
    current_execution,
    execution_context,
)
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize


class _StopSignal:
    def __init__(self, external):
        self.external = external if external is not None else threading.Event()
        self.retired = threading.Event()

    def is_set(self):
        return self.external.is_set() or self.retired.is_set()


class RegisteredImageRuntime:
    changed_message = "The image model changed. Repeat the search."
    runtime_kinds = ("image", "text")
    image_max_edge = None

    # Keep current + lookahead batches bounded, including JSON/base64 copies.
    prepared_byte_limit = 24 * 1024**2

    def __init__(self, spec, workspace, settings, *, role, device_role, cancelled=None):
        self.spec, self.settings, self.profile = spec, settings, spec.profile
        self.store = AdapterStore(workspace)
        self.manifest, self.package, self.registration = self.store.get(
            spec.manifest["adapter_identity"]
        )
        if self.manifest.role != role:
            raise UserError("The custom model has a different role.")
        if self.registration["environment"] != spec.manifest["adapter_environment"]:
            raise UserError(
                "The custom model environment changed. Validate and rebuild explicitly."
            )
        devices = self.manifest.data["devices"]
        self.execution = AdapterExecution(settings.model_device(device_role), settings, devices)
        self.query_execution = AdapterExecution(
            settings.model_device(device_role, query=True), settings, devices
        )
        self.index_batch_limit = self.manifest.data["input_contract"]["max_batch"]
        self.threads = settings.cpu_threads
        self.lock = threading.RLock()
        self.cancelled = _StopSignal(cancelled)
        self.retirement_error = None
        self.sessions = {}
        self.pending_workers = set()
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
        self.pending_workers.add(worker)
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
        finally:
            self.pending_workers.discard(worker)

    def _session(self, kind):
        if self.cancelled.external.is_set():
            raise InterruptedError("Custom image model is stopping.")
        if self.cancelled.retired.is_set():
            raise UserError(self.changed_message)
        worker = self.sessions.get(kind)
        if worker is not None and not worker.closed.is_set():
            return worker
        execution = self.query_execution if kind == "text" else self.execution
        worker = None
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
        except BaseException:
            if worker is not None:
                worker.close()
            raise
        self.sessions[kind] = worker
        return worker

    @contextmanager
    def _compute(self, execution, *, interactive=False):
        # Background/preparation waits must observe MediaService.stop too.
        signal = self.cancelled
        active = current_execution()
        user_execution = active
        if active is None:
            active = SearchExecution()
            active.cancelled = signal
            context = execution_context(active)
        else:
            context = nullcontext()
        try:
            with context, compute_gate(execution).slot(interactive=interactive):
                yield
        except SearchCancelled:
            if user_execution is not None and user_execution.cancelled.is_set():
                raise
            if signal.external.is_set():
                raise InterruptedError("Custom image model is stopping.") from None
            if signal.retired.is_set():
                raise UserError(self.changed_message) from None
            raise
        except InterruptedError:
            if signal.retired.is_set() and not signal.external.is_set():
                raise UserError(self.changed_message) from None
            raise

    def _check_gpu(self, worker):
        raise UserError("This adapter needs a CPU/GPU compatibility check.")

    def check_contract(self):
        try:
            for kind in self.runtime_kinds:
                execution = self.query_execution if kind == "text" else self.execution
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
                    ratio = (
                        min(1, self.image_max_edge / max(1, *original.size))
                        if self.image_max_edge
                        else 1
                    )
                    pixels = max(1, int(original.width * ratio)) * max(
                        1, int(original.height * ratio)
                    )
                    if (
                        not 1 <= original.width <= 16384
                        or not 1 <= original.height <= 16384
                        or original.width * original.height > 16 * 1024**2
                        or pixels * 3 > self.prepared_byte_limit
                        or retained + pixels * 3 > self.prepared_byte_limit
                    ):
                        raise UserError("The decoded image exceeds the custom model memory budget.")
                    image = ImageOps.exif_transpose(original).convert("RGB")
                    if ratio < 1:
                        image = image.resize(
                            (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
                            Image.Resampling.LANCZOS,
                        )
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
            **self.execution.info(),
            "loaded": bool(image and not image.closed.is_set()),
            "query_execution": self.query_execution.info(),
            "query_loaded": bool(text and not text.closed.is_set()),
        }
