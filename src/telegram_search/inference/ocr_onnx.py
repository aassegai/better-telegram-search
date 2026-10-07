"""Pinned PaddleOCR ONNX models; CPU/GPU share recognition cache identity."""

import hashlib
import importlib.util
import json
import math
from dataclasses import replace

from telegram_search.config.model_registry import ocr_spec
from telegram_search.config.runtime import ocr_command
from telegram_search.inference.bundles import BundleStore
from telegram_search.inference.ocr_process import OcrProcess
from telegram_search.inference.providers import COREML, CPU, CUDA, Execution
from telegram_search.inference.resources import compute_gate
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize


class OnnxOcrEngine:
    def __init__(self, workspace, settings):
        if not all(importlib.util.find_spec(name) for name in ("onnxruntime", "cv2", "pyclipper")):
            raise UserError(
                "Установите OCR runtime: uv sync --locked --extra semantic --extra ocr."
            )
        self.spec = ocr_spec()
        self.store = BundleStore(workspace)
        self.root = self.store.path(self.spec)
        hybrid = settings.ocr_device == "hybrid"
        device = "gpu" if hybrid else settings.ocr_device
        self.threads = max(1, settings.cpu_threads // 2) if hybrid else settings.cpu_threads
        self.cpu_peer = None
        self.last_timings = {}
        self.batch_limit = 4
        self.rate_settings = (
            settings.ocr_batch_size,
            settings.ocr_region_batch_size,
            settings.ocr_cpu_workers,
        )
        self.execution = Execution(
            device,
            device_id=settings.gpu_device_id,
            memory_limit_mib=settings.gpu_memory_limit_mib,
            threads=self.threads,
            probe=False,
        )
        self.version = hashlib.sha256(
            serialize(
                {
                    "model": self.spec.manifest,
                    "pipeline": "db-ctc-ru-en-v1",
                    "max_edge": settings.ocr_max_edge,
                    "det_limit": 960,
                    "det_threshold": 0.3,
                    "box_threshold": 0.6,
                    "unclip_ratio": 1.5,
                    "text_threshold": 0.5,
                    "recognition_padding": "fixed-320-buckets-max-2048",
                }
            ).encode()
        ).hexdigest()
        self.worker = OcrProcess(
            ocr_command(
                "--onnx",
                str(self.root),
                str(settings.ocr_max_edge),
                device,
                str(settings.gpu_device_id),
                str(settings.gpu_memory_limit_mib),
                str(self.threads),
                "--server",
            ),
            threads=self.threads,
            timeout=settings.ocr_timeout_seconds,
            region_batch=settings.ocr_region_batch_size,
            memory_limit_mib=max(256, settings.memory_limit_mib // 2),
            ready_handshake=True,
        )

        if hybrid:
            self.cpu_peer = OnnxOcrEngine(
                workspace,
                replace(
                    settings,
                    ocr_device="cpu",
                    cpu_threads=max(1, settings.cpu_threads - self.threads),
                ),
            )

    def backend_info(self):
        info = self.execution.info()
        if self.cpu_peer:
            info = {
                **info,
                "requested_device": "hybrid",
                "device": "cpu+gpu",
                "workers": [info, self.cpu_peer.execution.info()],
            }
        return info

    def verify(self):
        self.store.verify(self.spec)

    def prepare(self, *, offline=False):
        self.store.prepare(self.spec, offline=offline, repair=True)
        self.verify()
        self.check_contract()

    def check_contract(self):
        import io

        from PIL import Image

        data = io.BytesIO()
        Image.new("RGB", (64, 64), "white").save(data, format="PNG")
        try:
            with compute_gate(self.execution):
                self.recognize(data.getvalue())
            if self.cpu_peer:
                self.cpu_peer.check_contract()
        finally:
            self.unload()

    def recognize(self, data, *, _generation=None):
        self.last_timings = {}
        try:
            kwargs = {"generation": _generation} if _generation is not None else {}
            value = json.loads(self.worker.recognize(data, **kwargs))
            return self._parse(value)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.worker.unload()
            raise UserError("OCR не смог обработать изображение за заданное время.") from exc

    def recognize_many(self, images):
        try:
            responses = self.worker.recognize_many(images)
        except UserError:
            # A batch timeout/native failure is retried as individual images. Keep
            # this cap for the lifetime of the lane instead of repeating bad batches.
            self.batch_limit = 1
            raise
        values = []
        for response in responses:
            try:
                result = self._parse(json.loads(response))
                values.append({**result, "timings": dict(self.last_timings)})
            except (OSError, ValueError, KeyError, TypeError):
                values.append({"error": True})
        return values

    def _parse(self, value):
        if (
            not isinstance(value["text"], str)
            or len(value["text"]) > 65536
            or type(value["confidence"]) not in {int, float}
            or not 0 <= value["confidence"] <= 100
            or not math.isfinite(value["confidence"])
            or value["provider"] not in {CPU, CUDA, COREML}
            or (self.execution.device == "cpu" and value["provider"] != CPU)
            or (self.execution.device == "gpu" and value["provider"] != self.execution.provider)
        ):
            raise ValueError("OCR output")
        timings = value.get("timings", {})
        durations = {
            "preprocess_seconds",
            "detection_seconds",
            "boxes_seconds",
            "recognition_seconds",
        }
        if (
            not isinstance(timings, dict)
            or set(timings) - durations - {"regions"}
            or any(
                type(seconds) not in {int, float}
                or not 0 <= seconds <= 3600
                or not math.isfinite(seconds)
                for key, seconds in timings.items()
                if key in durations
            )
            or (
                "regions" in timings
                and (type(timings["regions"]) is not int or not 0 <= timings["regions"] <= 1000)
            )
        ):
            raise ValueError("OCR timings")
        self.last_timings = timings
        self.execution.provider = value["provider"]
        if self.execution.device == "auto" and value["provider"] == CPU:
            self.execution.warning = "GPU недоступен: режим Авто использует CPU."
        return {"text": value["text"], "confidence": value["confidence"]}

    def unload_worker(self):
        self.worker.unload()

    def unload(self):
        self.unload_worker()
        if self.cpu_peer:
            self.cpu_peer.unload()
