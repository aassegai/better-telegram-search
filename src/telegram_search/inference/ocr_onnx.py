"""Pinned PaddleOCR ONNX models; CPU/GPU share recognition cache identity."""

import hashlib
import importlib.util
import json
import math

from telegram_search.config.model_registry import ocr_spec
from telegram_search.config.runtime import ocr_command
from telegram_search.inference.bundles import BundleStore
from telegram_search.inference.ocr_process import OcrProcess
from telegram_search.inference.providers import COREML, CPU, CUDA, Execution
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
        self.threads = settings.cpu_threads
        self.execution = Execution(
            settings.ocr_device,
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
                settings.ocr_device,
                str(settings.gpu_device_id),
                str(settings.gpu_memory_limit_mib),
                str(self.threads),
                "--server",
            ),
            threads=self.threads,
            timeout=settings.ocr_timeout_seconds,
        )

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
            self.recognize(data.getvalue())
        finally:
            self.unload()

    def recognize(self, data):
        try:
            value = json.loads(self.worker.recognize(data))
            if (
                not isinstance(value["text"], str)
                or len(value["text"]) > 65536
                or type(value["confidence"]) not in {int, float}
                or not math.isfinite(value["confidence"])
                or not 0 <= value["confidence"] <= 100
                or value["provider"] not in {CPU, CUDA, COREML}
                or (self.execution.device == "cpu" and value["provider"] != CPU)
                or (self.execution.device == "gpu" and value["provider"] != self.execution.provider)
            ):
                raise ValueError("OCR output")
            self.execution.provider = value["provider"]
            if self.execution.device == "auto" and value["provider"] == CPU:
                self.execution.warning = "GPU недоступен: режим Авто использует CPU."
            return {"text": value["text"], "confidence": value["confidence"]}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.worker.unload()
            raise UserError("OCR не смог обработать изображение за заданное время.") from exc

    def unload(self):
        self.worker.unload()
