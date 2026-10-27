"""Pinned pairwise Jina ONNX inference; no remote Python or training runtime."""

import gc
import threading
import time

from telegram_search.inference.providers import CPU, Execution, runtime_version
from telegram_search.inference.resources import compute_gate
from telegram_search.inference.tokenization import ModelTokenizer
from telegram_search.search.execution import checkpoint
from telegram_search.shared.errors import UserError


def sigmoid_scores(logits, count):
    import numpy as np

    if logits.shape != (count, 1) or not np.isfinite(logits).all():
        raise UserError("Модель переранжирования вернула недопустимые оценки.")
    values = logits[:, 0].astype(np.float32)
    # exp(-abs) remains finite for either sign, including very large logits.
    exp = np.exp(-np.abs(values))
    scores = np.where(values >= 0, 1 / (1 + exp), exp / (1 + exp))
    return scores.astype(np.float32)


class JinaReranker:
    def __init__(
        self,
        spec,
        bundle,
        *,
        threads=4,
        device="cpu",
        gpu_device_id=0,
        gpu_memory_limit_mib=4096,
        **_kwargs,
    ):
        self.spec, self.bundle, self.threads = spec, bundle, threads
        self.tokenizer = ModelTokenizer(bundle)
        self.execution = Execution(
            device,
            device_id=gpu_device_id,
            memory_limit_mib=gpu_memory_limit_mib,
            threads=threads,
            probe=False,
        )
        self.query_execution = self.execution
        self.session = self.query_session = None
        self.lock = threading.RLock()
        self.last_used = 0

    def _inputs(self, query, documents):
        manifest = self.spec.manifest
        return self.tokenizer.pairs(
            query,
            documents,
            limit=manifest["max_length"],
            query_limit=manifest["query_max_tokens"],
            query_characters=manifest["query_max_characters"],
            document_characters=manifest["document_max_characters"],
        )

    def _load(self):
        if self.session is not None:
            return self.session
        try:
            session = self._checked_session()
        except Exception as exc:
            if self.execution.device != "auto" or self.execution.provider == CPU:
                if isinstance(exc, UserError):
                    raise
                raise UserError("Не удалось проверить ONNX-переранжирование.") from exc
            self.execution.provider = CPU
            self.execution.warning = "GPU недоступен: режим Авто использует CPU."
            try:
                session = self._checked_session()
            except UserError:
                raise
            except Exception as fallback:
                raise UserError("Не удалось проверить ONNX-переранжирование.") from fallback
        self.session = self.query_session = session
        return session

    def _checked_session(self):
        session = self.execution.session(str(self.bundle / "onnx/model.onnx"))
        inputs, outputs = session.get_inputs(), session.get_outputs()
        if (
            {item.name for item in inputs} != set(self.spec.manifest["required_inputs"])
            or any(item.type != "tensor(int64)" or len(item.shape) != 2 for item in inputs)
            or not any(
                item.name == "logits" and item.type == "tensor(float)" and len(item.shape) == 2
                for item in outputs
            )
        ):
            raise UserError("Контракт ONNX-модели переранжирования не совпадает с manifest.")
        if self.execution.provider != CPU:
            self._check_precision(session)
        return session

    def _check_precision(self, candidate):
        import numpy as np

        feed, _, _ = self._inputs(
            "How do I find the meeting?",
            ["The meeting starts at noon.", "Поиск сообщения в архиве."],
        )
        feed = {item.name: feed[item.name] for item in candidate.get_inputs()}
        reference = Execution("cpu", threads=self.threads).session(
            str(self.bundle / "onnx/model.onnx")
        )
        expected = sigmoid_scores(reference.run(["logits"], feed)[0], 2)
        actual = sigmoid_scores(candidate.run(["logits"], feed)[0], 2)
        if not np.allclose(expected, actual, atol=0.005, rtol=0):
            raise UserError("Не удалось подтвердить точность переранжирования на GPU.")

    def score_pairs(self, query, candidates):
        if (
            not isinstance(query, str)
            or not isinstance(candidates, list)
            or not 1 <= len(candidates) <= 4
        ):
            raise UserError("Недопустимый батч переранжирования.")
        if any(
            not isinstance(item, dict)
            or set(item) != {"candidate_id", "text"}
            or type(item["candidate_id"]) is not str
            or not 1 <= len(item["candidate_id"]) <= 128
            or not isinstance(item["text"], str)
            for item in candidates
        ):
            raise UserError("Недопустимый батч переранжирования.")
        ids = [item["candidate_id"] for item in candidates]
        if len(set(ids)) != len(ids) or any(
            not isinstance(item["text"], str) for item in candidates
        ):
            raise UserError("Недопустимый батч переранжирования.")
        with self.lock, compute_gate(self.execution).slot(interactive=True):
            checkpoint()
            feed, truncated, query_truncated = self._inputs(
                query, [item["text"] for item in candidates]
            )
            session = self._load()
            checkpoint()
            try:
                logits = session.run(
                    ["logits"], {item.name: feed[item.name] for item in session.get_inputs()}
                )[0]
            except Exception as exc:
                raise UserError("Ошибка ONNX-переранжирования. Базовый порядок сохранён.") from exc
            checkpoint()
            scores = sigmoid_scores(logits, len(candidates))
            self.last_used = time.monotonic()
            return [
                dict(
                    candidate_id=key,
                    score=float(score),
                    truncated=short,
                    query_truncated=query_truncated,
                )
                for key, score, short in zip(ids, scores, truncated, strict=True)
            ]

    def unload(self):
        with self.lock:
            self.session = self.query_session = None
        gc.collect()

    def unload_idle(self, *, query_last_used, idle_seconds, **_kwargs):
        with self.lock:
            if time.monotonic() - max(self.last_used, query_last_used) > idle_seconds:
                self.unload()

    def backend_info(self):
        return {
            "backend": "onnxruntime",
            "version": runtime_version(),
            "dtype": "float32",
            "model_id": self.spec.model_id,
            "revision": self.spec.revision,
            "loaded": self.session is not None,
            "threads": self.threads,
            **self.execution.info(),
        }
