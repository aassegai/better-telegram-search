"""Registered OCR uses the durable content/version queue, not a second index."""

import hashlib
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import replace

from telegram_search.model_adapters.execution import AdapterExecution
from telegram_search.model_adapters.image_runtime import RegisteredImageRuntime
from telegram_search.model_adapters.validation import smoke_requests
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize


class _OcrStop:
    def __init__(self, external, epoch):
        self.external = external if external is not None else threading.Event()
        self.epoch = epoch

    def is_set(self):
        return self.external.is_set() or self.epoch.is_set()


class AdapterOcrEngine(RegisteredImageRuntime):
    changed_message = "The OCR model changed. Repeat recognition."
    runtime_kinds = ("image",)
    manages_compute = True
    accepts_generation = True

    def __init__(self, spec, workspace, settings, *, cancelled=None):
        self.epoch = threading.Event()
        super().__init__(
            spec,
            workspace,
            settings,
            role="ocr",
            device_role="ocr",
            cancelled=_OcrStop(cancelled, self.epoch),
        )
        self.state_lock = threading.RLock()
        self.generation = 0
        self.batch_limit = self.index_batch_limit
        self.image_max_edge = settings.ocr_max_edge
        self.timeout = settings.ocr_timeout_seconds
        self.cpu_peer = None
        self.rate_settings = (
            settings.ocr_batch_size,
            settings.ocr_region_batch_size,
            settings.ocr_cpu_workers,
        )
        self.space_manifest.update(
            {"max_edge": self.image_max_edge, "ocr_pipeline": "exif-rgb-lanczos-custom-v1"}
        )
        self.version = self.space_id = hashlib.sha256(
            serialize(self.space_manifest).encode()
        ).hexdigest()

    def verify(self):
        manifest, _, record = self.store.get(self.manifest.identity)
        if (
            manifest.identity != self.manifest.identity
            or record["environment"] != self.registration["environment"]
        ):
            raise UserError("The custom OCR environment changed. Validate and prepare it again.")

    def prepare(self, *, offline=False):
        self.verify()
        self.check_contract()

    def check_contract(self):
        with self.lock:
            self.epoch.clear()
        super().check_contract()
        if self.cpu_peer:
            self.cpu_peer.check_contract()

    def _check_gpu(self, worker):
        if "cpu" not in self.manifest.data["devices"]:
            # GPU-only adapters cannot share a CPU cache; their contract is still
            # checked on the explicit device before activation.
            for operation, payload in smoke_requests(self.manifest):
                worker.request(operation, payload, cancelled=self.cancelled)
            return
        reference = AdapterExecution("cpu", self.settings, self.manifest.data["devices"])

        def confidence_equal(a, b):
            x, y = a.get("confidence"), b.get("confidence")
            return (x is None and y is None) or (
                x is not None and y is not None and abs(x - y) <= 0.02
            )

        with self._worker(reference) as cpu:
            for operation, payload in smoke_requests(self.manifest):
                expected = cpu.request(operation, payload, cancelled=self.cancelled)
                actual = worker.request(operation, payload, cancelled=self.cancelled)
                for a, b in zip(expected, actual, strict=True):
                    same = a["text"] == b["text"] and confidence_equal(a, b)
                    ar, br = a.get("regions", []), b.get("regions", [])
                    same = same and len(ar) == len(br)
                    for x, y in zip(ar, br, strict=False):
                        same = same and x["text"] == y["text"] and confidence_equal(x, y)
                        same = same and all(
                            abs(i - j) <= 1 for i, j in zip(x["bbox"], y["bbox"], strict=True)
                        )
                    if not same:
                        raise UserError("Custom OCR CPU/GPU outputs differ; select CPU.")

    @contextmanager
    def compute_slot(self):
        with self.lock:
            self.epoch.clear()
        with self._compute(self.execution):
            yield

    def recognize(self, data, *, _generation=None, _reserved=False):
        return self.recognize_many([data], _generation=_generation, _reserved=_reserved)[0]

    def recognize_many(self, images, *, _generation=None, _reserved=False):
        if not 1 <= len(images) <= 128:
            raise UserError("Invalid custom OCR batch.")

        results = [None] * len(images)

        def cancelled():
            return [value if value is not None else {"cancelled": True} for value in results]

        with self.lock:
            with self.state_lock:
                generation = self.generation
                if _generation is not None and _generation != generation:
                    return cancelled()
                self.epoch.clear()
        reservation = nullcontext() if _reserved else self._compute(self.execution)
        try:
            with reservation, self.lock:
                if generation != self.generation:
                    return cancelled()
                group, indices, retained = [], [], 0
                started = time.perf_counter()
                inference = 0.0

                def publish():
                    nonlocal inference
                    if not group:
                        return
                    before = time.perf_counter()
                    worker = self._session("image")
                    worker.context = replace(worker.context, batch_size=len(group))
                    output = worker.request(
                        "recognize",
                        {"images": [{**v[0], "id": str(i)} for i, v in enumerate(group)]},
                        timeout=self.timeout,
                        cancelled=self.cancelled,
                    )
                    inference += time.perf_counter() - before
                    for position, item in zip(indices, output, strict=True):
                        if len(item["text"]) > 65536:
                            results[position] = {"error": True}
                        else:
                            confidence = item.get("confidence")
                            results[position] = {
                                "text": item["text"],
                                "confidence": None if confidence is None else confidence * 100,
                                "regions": item.get("regions", []),
                            }

                for position, data in enumerate(images):
                    if generation != self.generation or self.cancelled.is_set():
                        return cancelled()
                    try:
                        value = self.prepare_images([data])[0]
                    except UserError:
                        results[position] = {"error": True}
                        continue
                    if group and (
                        len(group) >= self.batch_limit
                        or retained + value[2] > self.prepared_byte_limit
                    ):
                        publish()
                        group, indices, retained = [], [], 0
                    group.append(value)
                    indices.append(position)
                    retained += value[2]
                publish()
                if generation != self.generation:
                    return cancelled()
                self.last_used = time.monotonic()
                self.last_timings = {
                    "inference_seconds": inference,
                    "preprocess_seconds": max(0, time.perf_counter() - started - inference),
                    "batch_size": len(images),
                }
                return results
        except (UserError, InterruptedError):
            if generation != self.generation or self.cancelled.is_set():
                return cancelled()
            if any(value is not None for value in results):
                # Keep validated replies; retry the unfinished tail individually.
                self.batch_limit = 1
                return cancelled()
            raise

    def unload_worker(self):
        with self.state_lock:
            self.generation += 1
            self.epoch.set()
            workers = set(self.sessions.values()) | self.pending_workers.copy()
        errors = []
        for worker in workers:
            try:
                worker.close()
            except BaseException as exc:
                errors.append(exc)
        if self.lock.acquire(blocking=False):
            try:
                self.sessions = {
                    kind: worker
                    for kind, worker in self.sessions.items()
                    if worker in workers and not worker.closed.is_set()
                }
            finally:
                self.lock.release()
        if errors:
            raise errors[0]

    def unload(self):
        errors = []
        for lane in (self, self.cpu_peer):
            if lane is None:
                continue
            try:
                lane.unload_worker()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    def retire(self):
        if self.cpu_peer:
            self.cpu_peer.retire()
        super().retire()

    def backend_info(self):
        info = {**super().backend_info(), "ocr_version": self.version}
        if self.cpu_peer:
            info.update(
                requested_device="hybrid",
                device="cpu+gpu",
                workers=[self.execution.info(), self.cpu_peer.execution.info()],
            )
        return info
