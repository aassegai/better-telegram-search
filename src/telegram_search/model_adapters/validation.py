"""Explicit public-input smoke tests, independent from user messages or indexes."""

import base64
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from telegram_search.inference.providers import COREML, CPU, CUDA
from telegram_search.inference.resources import compute_gate
from telegram_search.model_adapters.client import AdapterContext, AdapterWorker
from telegram_search.model_adapters.manifest import AdapterManifest
from telegram_search.search.execution import (
    SearchCancelled,
    SearchExecution,
    checkpoint,
    execution_context,
)
from telegram_search.shared.errors import UserError


@contextmanager
def validation_slot(context, cancelled):
    execution = SearchExecution()
    if cancelled is not None:
        execution.cancelled = cancelled
    provider = CPU if context.device == "cpu" else COREML if sys.platform == "darwin" else CUDA
    resources = SimpleNamespace(
        provider=provider, device=context.device, device_id=context.gpu_device_id
    )
    try:
        with execution_context(execution), compute_gate(resources).slot():
            yield
    except SearchCancelled:
        raise InterruptedError("Custom model check cancelled.") from None


def inspect_adapter(manifest_path, *, check=lambda: None, on_progress=None, expected_role=None):
    manifest = AdapterManifest.read(Path(manifest_path))
    if expected_role is not None and manifest.role != expected_role:
        raise UserError("The adapter role differs from the selected role.")
    manifest.verify_files(
        Path(manifest_path).expanduser().absolute().parent, check=check, on_progress=on_progress
    )
    return {**manifest.summary(), "state": "inspected", "code_executed": False}


def smoke_requests(manifest):
    items = [
        {"id": "first", "text": "Find a message about a train."},
        {"id": "second", "text": "Поиск сообщения о встрече."},
    ]
    items = items[: manifest.data["input_contract"]["max_batch"]]
    image = {
        "id": "image",
        "width": 2,
        "height": 2,
        "mode": "RGB",
        "pixels": base64.b64encode(bytes([255] * 12)).decode(),
    }
    if manifest.role == "text_embedding":
        yield "count_tokens", {"texts": [item["text"] for item in items]}
        for purpose in ("query", "document"):
            prefix = manifest.data["preprocessing"][f"{purpose}_prefix"]
            yield (
                "encode_text",
                {
                    "items": [{**item, "text": prefix + item["text"]} for item in items],
                    "operation": purpose,
                },
            )
    elif manifest.role == "image_embedding":
        yield "encode_images", {"images": [image]}
        yield "encode_image_queries", {"items": items}
    elif manifest.role == "reranker":
        yield (
            "rerank",
            {
                "query": "Where is the station?",
                "candidates": [
                    {"candidate_id": item["id"], "text": item["text"]} for item in items
                ],
            },
        )
    elif manifest.role == "ocr":
        yield "recognize", {"images": [image]}
    elif manifest.role == "asr":
        rate = manifest.data["input_contract"]["sample_rate"]
        samples = rate // 10
        yield (
            "transcribe",
            {
                "audio": {
                    "id": "silence",
                    "sample_rate": rate,
                    "samples": samples,
                    "pcm_f32le": base64.b64encode(bytes(samples * 4)).decode(),
                },
                "language": "auto",
            },
        )


def validate_adapter(
    store,
    manifest_path,
    python,
    *,
    context=None,
    register=False,
    timeout=60,
    cancelled=None,
    on_progress=None,
    before_register=None,
    expected_role=None,
    expected_identity=None,
):
    def check():
        checkpoint()
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError("Custom model operation cancelled.")

    manifest, staged = store.stage(manifest_path, check=check, on_progress=on_progress)
    try:
        if expected_role is not None and manifest.role != expected_role:
            raise UserError("The adapter role differs from the selected role.")
        if expected_identity is not None and manifest.identity != expected_identity:
            raise UserError("The adapter changed. Inspect it again before validation.")
        operations = []
        selected_context = context or AdapterContext()
        if on_progress:
            on_progress(
                {"phase": "waiting_resources", "completed": 0, "total": None, "unit": "items"}
            )
        with (
            validation_slot(selected_context, cancelled),
            AdapterWorker(
                manifest,
                staged,
                python,
                store.runner(),
                context=context,
                on_progress=on_progress,
                cancelled=cancelled,
            ) as worker,
        ):

            def call(operation, payload=None):
                if on_progress:
                    on_progress(
                        {"phase": operation, "completed": 0, "total": None, "unit": "items"}
                    )
                return worker.request(operation, payload, timeout=timeout, cancelled=cancelled)

            info = call("info")
            call("validate")
            call("load")
            for operation, payload in smoke_requests(manifest):
                call(operation, payload)
                operations.append(operation)
            call("unload")
        report = {
            **manifest.summary(),
            "state": "passed",
            "code_executed": True,
            "environment": info["environment"],
            "device": (context or AdapterContext()).device,
            "smoke": {
                "fixture": "public-adapter-contract-v1",
                "operations": operations,
                "quality_evaluation": False,
            },
        }
        if register:
            check()
            if before_register:
                before_register()
            store.register(manifest, staged, python=python, report=report)
            report["registered"] = True
        return report
    finally:
        if staged.exists():
            shutil.rmtree(staged)
