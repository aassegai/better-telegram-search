import gc
import hashlib
import importlib.metadata
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import numpy as np

from telegram_search.config.model_registry import ModelSpec
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
    """Pinned, local ONNX FP32 inference with explicit CPU execution only."""

    def __init__(self, spec: ModelSpec, bundle: Path, *, threads: int = 4):
        if not 1 <= threads <= 32:
            raise UserError("Число CPU-потоков должно быть от 1 до 32.")
        self.spec = spec
        self.bundle = bundle
        self.threads = threads
        self.tokenizer = ModelTokenizer(bundle)
        self.session = None
        self.condition = threading.Condition()
        self.encoding = False
        self.suspended = False
        self.interactive_waiters = 0
        self.runtime = {
            "backend": "onnxruntime",
            "version": importlib.metadata.version("onnxruntime"),
            "tokenizers_version": importlib.metadata.version("tokenizers"),
            "dtype": "float32",
            "provider": "CPUExecutionProvider",
        }
        self.space_manifest = {"model": spec.manifest, "runtime": self.runtime}
        self.space_id = hashlib.sha256(serialize(self.space_manifest).encode()).hexdigest()

    def _load(self):
        import onnxruntime as ort

        ort.disable_telemetry_events()
        if self.session is not None:
            return self.session
        options = ort.SessionOptions()
        options.intra_op_num_threads = self.threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 3
        try:
            session = ort.InferenceSession(
                str(self.bundle / "onnx/model.onnx"),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:
            raise UserError("Не удалось открыть закреплённую ONNX-модель на CPU.") from exc
        inputs = {item.name: item for item in session.get_inputs()}
        outputs = {item.name: item for item in session.get_outputs()}
        if (
            not set(self.spec.manifest["required_inputs"]).issubset(inputs)
            or not set(inputs).issubset(self.spec.manifest["allowed_inputs"])
            or any(item.type != "tensor(int64)" for item in inputs.values())
            or self.spec.manifest["output_name"] not in outputs
            or outputs[self.spec.manifest["output_name"]].type != "tensor(float)"
            or session.get_providers() != ["CPUExecutionProvider"]
        ):
            raise UserError("Контракт upstream ONNX-модели не совпадает с manifest.")
        self.session = session
        return session

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
            session = self._load()
            feed = {item.name: inputs[item.name] for item in session.get_inputs()}
            try:
                hidden = session.run([self.spec.manifest["output_name"]], feed)[0]
            except Exception as exc:
                raise UserError(
                    "Ошибка ONNX-инференса на CPU. Уменьшите batch или повторите."
                ) from exc
            embeddings = masked_mean_normalize(hidden, inputs["attention_mask"])
            if embeddings.shape != (len(texts), self.spec.dimension):
                raise UserError("Размерность embeddings не соответствует закреплённой модели.")
            return embeddings
        finally:
            with self.condition:
                self.encoding = False
                self.condition.notify_all()

    def backend_info(self) -> dict:
        return {
            **self.runtime,
            "model_id": self.spec.model_id,
            "revision": self.spec.revision,
            "embedding_space": self.space_id,
            "dimension": self.spec.dimension,
            "loaded": self.session is not None,
            "threads": self.threads,
        }

    def unload(self) -> None:
        with self.condition:
            while self.encoding:
                self.condition.wait()
            self.session = None
        gc.collect()

    def suspend(self) -> None:
        with self.condition:
            self.suspended = True
            self.condition.notify_all()
            while self.encoding:
                self.condition.wait()
            self.session = None
        gc.collect()

    def resume(self) -> None:
        with self.condition:
            self.suspended = False
            self.condition.notify_all()
