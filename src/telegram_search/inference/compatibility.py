"""An embedding space describes the model; execution devices are provenance.

Keep the original CPU manifest as the reference so 0.2.0 work can resume without
rewriting chunk IDs. Only the two pinned ORT versions may share that reference;
models, tokenizer versions, pooling, prefixes and dtype still have to match.
GPU sessions additionally check FP32 output against CPU on public probes.
"""

import copy

import numpy as np

from telegram_search.shared.errors import UserError

RUNTIME_VERSIONS = {"1.23.2", "1.30.0"}


def compatible_manifest(left, right):
    left, right = copy.deepcopy(left), copy.deepcopy(right)
    for value in (left, right):
        runtime = value.get("runtime", {})
        if (
            isinstance(runtime, dict)
            and runtime.get("backend") == "onnxruntime"
            and runtime.get("provider") == "CPUExecutionProvider"
            and runtime.get("version") in RUNTIME_VERSIONS
            and runtime.get("dtype") == "float32"
        ):
            runtime["version"] = "validated-onnx-fp32-v1"
    return left == right


def check_vectors(reference, candidate):
    if (
        reference.shape != candidate.shape
        or reference.ndim != 2
        or not reference.size
        or not np.isfinite(reference).all()
        or not np.isfinite(candidate).all()
        or np.max(np.abs(reference - candidate)) > 1e-4
        or np.min(np.sum(reference * candidate, axis=1)) < 0.99999
    ):
        raise UserError("Результат GPU не прошёл проверку совместимости с CPU. Выберите CPU.")
