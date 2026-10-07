import gc
import hashlib
import importlib.metadata
import json
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import numpy as np

from telegram_search.config.model_registry import ModelSpec
from telegram_search.inference.compatibility import check_vectors, compatible_manifest
from telegram_search.inference.providers import CPU, Execution, runtime_version
from telegram_search.inference.resources import compute_lock
from telegram_search.inference.tokenization import ModelTokenizer
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize


def masked_mean_normalize(hidden: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    if hidden.ndim != 3 or hidden.shape[:2] != attention_mask.shape:
        raise UserError("Форма выхода модели не соответствует attention mask.")
    mask = attention_mask[..., None].astype(np.float32)
    values = hidden.astype(np.float32, copy=False)
    mean = (values * mask).sum(axis=1) / mask.sum(axis=1).clip(min=1)
    norm = np.linalg.norm(mean, axis=1, keepdims=True)
    if not np.isfinite(mean).all() or np.any(norm <= 0):
        raise UserError("Модель вернула недопустимый embedding.")
    return np.ascontiguousarray(mean / norm, dtype=np.float32)


class E5Encoder:
    """Pinned local ONNX FP32 inference on an explicitly selected device."""

    def __init__(
        self,
        spec: ModelSpec,
        bundle: Path,
        *,
        threads: int = 4,
        device="cpu",
        search_device=None,
        gpu_device_id=0,
        gpu_memory_limit_mib=4096,
    ):
        if not 1 <= threads <= 32:
            raise UserError("Число CPU-потоков должно быть от 1 до 32.")
        self.execution = Execution(
            device,
            device_id=gpu_device_id,
            memory_limit_mib=gpu_memory_limit_mib,
            threads=threads,
            probe=False,
        )
        self.query_execution = Execution(
            search_device or device,
            device_id=gpu_device_id,
            memory_limit_mib=gpu_memory_limit_mib,
            threads=threads,
            probe=False,
        )
        self.spec = spec
        self.bundle = bundle
        self.threads = threads
        self.tokenizer = ModelTokenizer(bundle)
        self.session = None
        self.query_session = None
        self.validated_providers = {CPU}
        self.last_index_used = 0.0
        self.condition = threading.Condition()
        self.encoding = False
        self.suspended = False
        self.interactive_waiters = 0
        self.runtime = {
            "backend": "onnxruntime",
            "version": runtime_version(),
            "tokenizers_version": importlib.metadata.version("tokenizers"),
            "dtype": "float32",
            "provider": self.execution.provider,
        }
        # Reference format stays compatible with 0.2.0 CPU spaces. Actual devices
        # and runtime are reported separately, never used to split an index.
        self.space_manifest = {"model": spec.manifest, "runtime": {**self.runtime, "provider": CPU}}
        self.space_id = hashlib.sha256(serialize(self.space_manifest).encode()).hexdigest()

    def adopt_space(self, manifest_json):
        manifest = json.loads(manifest_json) if isinstance(manifest_json, str) else manifest_json
        if not compatible_manifest(self.space_manifest, manifest):
            raise UserError("Модель или обработка текста отличается от закреплённого индекса.")
        self.space_manifest = json.loads(serialize(manifest))
        self.space_id = hashlib.sha256(serialize(manifest).encode()).hexdigest()

    def _load(self, *, query=False):
        import onnxruntime as ort

        ort.disable_telemetry_events()
        execution = self.query_execution if query else self.execution
        attribute = "query_session" if query else "session"
        if getattr(self, attribute) is not None:
            return getattr(self, attribute)
        other = self.session if query else self.query_session
        if other is not None and other.get_providers()[0] == execution.provider:
            setattr(self, attribute, other)
            return other
        execution.threads = self.threads
        try:
            session = execution.session(str(self.bundle / "onnx/model.onnx"))
            if execution.provider not in self.validated_providers:
                self._check_precision(session)
                self.validated_providers.add(execution.provider)
        except UserError:
            if execution.device != "auto":
                raise
            execution.provider = CPU
            execution.warning = "GPU недоступен: режим Авто использует CPU."
            session = execution.session(str(self.bundle / "onnx/model.onnx"))
        inputs = {item.name: item for item in session.get_inputs()}
        outputs = {item.name: item for item in session.get_outputs()}
        if (
            not set(self.spec.manifest["required_inputs"]).issubset(inputs)
            or not set(inputs).issubset(self.spec.manifest["allowed_inputs"])
            or any(item.type != "tensor(int64)" for item in inputs.values())
            or self.spec.manifest["output_name"] not in outputs
            or outputs[self.spec.manifest["output_name"]].type != "tensor(float)"
        ):
            raise UserError("Контракт upstream ONNX-модели не совпадает с manifest.")
        setattr(self, attribute, session)
        return session

    def _check_precision(self, candidate):
        reference = self.query_session if self.query_execution.provider == CPU else None
        if reference is None:
            reference = Execution("cpu", threads=self.threads).session(
                str(self.bundle / "onnx/model.onnx")
            )
            if self.query_execution.provider == CPU:
                self.query_session = reference
        for purpose in ("query", "passage"):
            inputs = self.tokenizer.batch(
                [
                    self.spec.manifest[f"{purpose}_prefix"] + text
                    for text in (
                        "Find the meeting schedule for next week.",
                        "Архив сообщений и поиск фотографий путешествия.",
                        "Budget, release date, schedule. " * 32,
                    )
                ],
                limit=512,
            )
            feed = {item.name: inputs[item.name] for item in candidate.get_inputs()}
            output = [self.spec.manifest["output_name"]]
            check_vectors(
                masked_mean_normalize(reference.run(output, feed)[0], inputs["attention_mask"]),
                masked_mean_normalize(candidate.run(output, feed)[0], inputs["attention_mask"]),
            )

    def encode_text(
        self,
        texts: Sequence[str],
        purpose: Literal["query", "passage"],
        *,
        interactive: bool = False,
    ) -> np.ndarray:
        if purpose not in {"query", "passage"}:
            raise UserError("Неизвестное назначение embedding.")
        if not texts:
            return np.empty((0, self.spec.dimension), dtype=np.float32)
        if len(texts) > 32:
            raise UserError("Батч модели должен содержать не более 32 текстов.")
        with self.condition:
            if interactive:
                self.interactive_waiters += 1
            try:
                while self.encoding or (not interactive and self.interactive_waiters):
                    if self.suspended:
                        raise UserError(
                            "Модель временно остановлена для подготовки нового профиля."
                        )
                    self.condition.wait()
                if self.suspended:
                    raise UserError("Модель временно остановлена для подготовки нового профиля.")
                self.encoding = True
            finally:
                if interactive:
                    self.interactive_waiters -= 1
                self.condition.notify_all()
        try:
            prefix = self.spec.manifest[f"{purpose}_prefix"]
            prepared = [prefix + text for text in texts]
            limit = self.spec.manifest["chunk_max_tokens"] if purpose == "passage" else 512
            inputs = self.tokenizer.batch(prepared, limit)
            try:
                with compute_lock.slot(interactive=interactive):
                    session = self._load(query=purpose == "query")
                    feed = {item.name: inputs[item.name] for item in session.get_inputs()}
                    hidden = session.run([self.spec.manifest["output_name"]], feed)[0]
            except Exception as exc:
                raise UserError("Ошибка ONNX-инференса. Уменьшите batch или выберите CPU.") from exc
            embeddings = masked_mean_normalize(hidden, inputs["attention_mask"])
            if embeddings.shape != (len(texts), self.spec.dimension):
                raise UserError("Размерность embeddings не соответствует закреплённой модели.")
            return embeddings
        finally:
            with self.condition:
                if purpose == "passage":
                    self.last_index_used = time.monotonic()
                self.encoding = False
                self.condition.notify_all()

    def backend_info(self) -> dict:
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
        }

    def unload(self) -> None:
        with self.condition:
            while self.encoding:
                self.condition.wait()
            self.session = None
            self.query_session = None
        gc.collect()

    def suspend(self) -> None:
        with self.condition:
            self.suspended = True
            self.condition.notify_all()
            while self.encoding:
                self.condition.wait()
            self.session = None
            self.query_session = None
        gc.collect()

    def resume(self) -> None:
        with self.condition:
            self.suspended = False
            self.condition.notify_all()

    def unload_index(self):
        with self.condition:
            while self.encoding:
                self.condition.wait()
            self.session = None
        gc.collect()
