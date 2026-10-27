"""External Python pair scoring in the existing filtered reranking stage."""

import threading
import time
from contextlib import contextmanager

from telegram_search.inference.providers import CPU
from telegram_search.inference.resources import compute_gate
from telegram_search.model_adapters.client import AdapterContext, AdapterWorker
from telegram_search.model_adapters.contracts import validate_request
from telegram_search.model_adapters.store import AdapterStore
from telegram_search.model_adapters.text_encoder import AdapterExecution
from telegram_search.search.execution import (
    SearchCancelled,
    SearchExecution,
    checkpoint,
    current_execution,
    execution_context,
    search_lock,
)
from telegram_search.shared.errors import UserError


class AdapterReranker:
    def __init__(self, spec, workspace, settings, *, cancelled=None):
        self.spec, self.settings = spec, settings
        self.store = AdapterStore(workspace)
        self.manifest, self.package, self.registration = self.store.get(
            spec.manifest["adapter_identity"]
        )
        if (
            self.manifest.role != "reranker"
            or self.registration["environment"] != spec.manifest["adapter_environment"]
        ):
            raise UserError("The reranker environment changed. Validate and prepare it again.")
        self.execution = AdapterExecution(
            settings.model_device("e5", query=True), settings, self.manifest.data["devices"]
        )
        self.query_execution = self.execution
        self.max_batch = min(4, self.manifest.data["input_contract"]["max_batch"])
        self.threads = settings.cpu_threads
        self.session = self.query_session = None
        self.lock = threading.RLock()
        self.last_used = 0
        self.cancelled = cancelled

    def _open(self, execution):
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
                batch_size=self.max_batch,
            ),
            cancelled=self.cancelled,
        )
        try:
            if (
                worker.request("info", cancelled=self.cancelled)["environment"]
                != self.registration["environment"]
            ):
                raise UserError("The reranker environment changed. Validate and prepare it again.")
            worker.request("validate", cancelled=self.cancelled)
            worker.request("load", timeout=180, cancelled=self.cancelled)
            return worker
        except BaseException:
            worker.close()
            raise

    def _load(self):
        if self.session is not None and not self.session.closed.is_set():
            return self.session
        worker = None
        try:
            worker = self._open(self.execution)
            if self.execution.provider != CPU:
                self._check_gpu(worker)
        except UserError:
            if worker is not None:
                worker.close()
            if self.execution.device != "auto" or "cpu" not in self.manifest.data["devices"]:
                raise
            self.execution.provider = CPU
            self.execution.warning = "Custom GPU unavailable; Auto uses CPU."
            worker = self._open(self.execution)
        except BaseException:
            if worker is not None:
                worker.close()
            raise
        self.session = self.query_session = worker
        return worker

    @contextmanager
    def _operation(self):
        if current_execution() is not None or self.cancelled is None:
            yield
            return
        execution = SearchExecution()
        execution.cancelled = self.cancelled
        try:
            with execution_context(execution):
                yield
        except SearchCancelled:
            raise InterruptedError("Custom model operation cancelled.") from None

    def _check_gpu(self, candidate):
        import numpy as np

        payload = {
            "query": "Find the meeting.",
            "candidates": [
                {"candidate_id": "meeting", "text": "The meeting starts at noon."},
                {"candidate_id": "archive", "text": "Архив сообщений."},
            ][: self.max_batch],
        }
        # Scores may be returned in relevance order; compare by candidate ID.
        with self._open(
            AdapterExecution("cpu", self.settings, self.manifest.data["devices"])
        ) as cpu:
            expected = {r["candidate_id"]: r["score"] for r in cpu.request("rerank", payload)}
            actual = {r["candidate_id"]: r["score"] for r in candidate.request("rerank", payload)}
            if not np.allclose(
                [expected[k] for k in expected], [actual[k] for k in expected], atol=0.005, rtol=0
            ):
                raise UserError("Не удалось подтвердить точность переранжирования на GPU.")

    def score_pairs(self, query, candidates):
        payload = {"query": query, "candidates": candidates}
        validate_request(self.manifest, "rerank", payload)
        if len(candidates) > self.max_batch:
            raise UserError("Недопустимый батч переранжирования.")
        query_limit = self.spec.manifest["query_max_characters"]
        document_limit = self.spec.manifest["document_max_characters"]
        bounded = {
            "query": query[:query_limit],
            "candidates": [
                {"candidate_id": c["candidate_id"], "text": c["text"][:document_limit]}
                for c in candidates
            ],
        }
        with (
            self._operation(),
            search_lock(self.lock),
            compute_gate(self.execution).slot(interactive=True),
        ):
            checkpoint()
            worker = self._load()
            try:
                rows = worker.request("rerank", bounded, cancelled=self.cancelled)
            finally:
                if worker.closed.is_set():
                    self.session = self.query_session = None
            checkpoint()
            by_id = {r["candidate_id"]: r for r in rows}
            self.last_used = time.monotonic()
            return [
                {
                    "candidate_id": c["candidate_id"],
                    "score": by_id[c["candidate_id"]]["score"],
                    "truncated": len(c["text"]) > document_limit
                    or by_id[c["candidate_id"]].get("truncated", False),
                    "query_truncated": len(query) > query_limit
                    or by_id[c["candidate_id"]].get("query_truncated", False),
                }
                for c in candidates
            ]

    def unload(self):
        with self.lock:
            worker = self.session
            self.session = self.query_session = None
            if worker is not None:
                worker.close()

    def unload_idle(self, *, query_last_used, idle_seconds, **_kwargs):
        with self.lock:
            if time.monotonic() - max(self.last_used, query_last_used) > idle_seconds:
                self.unload()

    def backend_info(self):
        return {
            "backend": "python-adapter",
            "version": "1",
            "model_id": self.spec.model_id,
            "revision": self.spec.revision,
            "loaded": self.session is not None and not self.session.closed.is_set(),
            "threads": self.threads,
            "max_batch": self.max_batch,
            **self.execution.info(),
        }
