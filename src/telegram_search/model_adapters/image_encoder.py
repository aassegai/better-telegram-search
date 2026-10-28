"""Registered image/query adapters share the guarded media vector pipeline."""

import time

import numpy as np

from telegram_search.inference.compatibility import check_vectors
from telegram_search.model_adapters.execution import AdapterExecution
from telegram_search.model_adapters.image_runtime import RegisteredImageRuntime
from telegram_search.model_adapters.validation import smoke_requests
from telegram_search.search.execution import checkpoint, search_lock
from telegram_search.shared.errors import UserError


class AdapterImageEncoder(RegisteredImageRuntime):
    def __init__(self, spec, workspace, settings, *, cancelled=None):
        super().__init__(
            spec,
            workspace,
            settings,
            role="image_embedding",
            device_role="clip",
            cancelled=cancelled,
        )
        self.dimension = spec.dimension

    def _check_gpu(self, worker):
        reference = AdapterExecution("cpu", self.settings, self.manifest.data["devices"])
        with self._worker(reference) as cpu:
            for operation, payload in smoke_requests(self.manifest):
                check_vectors(
                    np.asarray(
                        cpu.request(operation, payload, cancelled=self.cancelled)["vectors"],
                        dtype=np.float32,
                    ),
                    np.asarray(
                        worker.request(operation, payload, cancelled=self.cancelled)["vectors"],
                        dtype=np.float32,
                    ),
                )

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

    def backend_info(self):
        return {**super().backend_info(), "dimension": self.dimension}
