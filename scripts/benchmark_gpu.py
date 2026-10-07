"""Compare pinned E5/CLIP CPU and GPU outputs using public synthetic input only."""

import argparse
import importlib.util
import io
import json
import math
import time
from pathlib import Path

import numpy as np
from PIL import Image

from telegram_search.config.model_registry import registry
from telegram_search.inference.bundles import BundleStore
from telegram_search.inference.clip import ClipEncoder
from telegram_search.inference.compatibility import check_vectors
from telegram_search.inference.e5 import E5Encoder
from telegram_search.inference.providers import runtime_version


def measure(function, values):
    output = function(values)
    start = time.perf_counter()
    for _ in range(3):
        output = function(values)
    return output, (time.perf_counter() - start) / 3


def compare(cpu, gpu, cpu_time, gpu_time):
    check_vectors(cpu, gpu)
    if not all(math.isfinite(value) and value > 0 for value in (cpu_time, gpu_time)):
        raise RuntimeError("Invalid inference timing")
    error = float(np.max(np.abs(cpu - gpu)))
    cosine = float(np.min(np.sum(cpu * gpu, axis=1)))
    if error > 1e-4 or cosine < 0.99999:
        raise RuntimeError("CPU/GPU embeddings exceed FP32 acceptance tolerance")
    return {
        "max_absolute_error": error,
        "minimum_cosine": cosine,
        "cpu_seconds": cpu_time,
        "gpu_seconds": gpu_time,
        "speedup": cpu_time / gpu_time,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path("workspace"))
    parser.add_argument("--output", type=Path, default=Path("workspace/gpu-validation.json"))
    parser.add_argument("--cpu-only", action="store_true")
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--vectors-output", type=Path)
    args = parser.parse_args()
    if importlib.util.find_spec("torch") is not None:
        raise RuntimeError("The benchmark must not have Torch installed")
    texts = [
        "Find the meeting schedule for next week.",
        "Найти фотографии летнего путешествия.",
        "We discussed the project budget and release date.",
        "Архив сообщений и локальный поиск.",
    ]
    report = {
        "runtime": runtime_version(),
        "torch_absent": True,
        "synthetic_only": True,
        "models": {},
    }
    vectors = {}
    reference = np.load(args.reference, allow_pickle=False) if args.reference else None
    device = "cpu" if args.cpu_only else "gpu"
    for profile, spec in registry().items():
        bundle = BundleStore(args.workspace).verify(spec)
        cpu, gpu = E5Encoder(spec, bundle), E5Encoder(spec, bundle, device=device)
        left, cpu_time = measure(
            lambda items, encoder=cpu: encoder.encode_text(items, "passage"), texts
        )
        right, gpu_time = measure(
            lambda items, encoder=gpu: encoder.encode_text(items, "passage"), texts
        )
        report["models"]["e5-" + profile] = compare(left, right, cpu_time, gpu_time)
        vectors["e5-" + profile] = right
        if reference is not None:
            check_vectors(reference["e5-" + profile], right)
        query = gpu.encode_text(texts, "query")
        vectors["e5-" + profile + "-query"] = query
        if reference is not None:
            check_vectors(reference["e5-" + profile + "-query"], query)
        assert cpu.space_id == gpu.space_id
        cpu.unload()
        gpu.unload()
    cpu, gpu = ClipEncoder(args.workspace), ClipEncoder(args.workspace, device=device)
    left, cpu_time = measure(cpu.encode_text, texts)
    right, gpu_time = measure(gpu.encode_text, texts)
    report["models"]["clip-text"] = compare(left, right, cpu_time, gpu_time)
    vectors["clip-text"] = right
    if reference is not None:
        check_vectors(reference["clip-text"], right)
    images = []
    for color in ("red", "green", "blue", "white"):
        output = io.BytesIO()
        Image.new("RGB", (320, 240), color).save(output, format="PNG")
        images.append(output.getvalue())
    left, cpu_time = measure(cpu.encode_images, images)
    right, gpu_time = measure(gpu.encode_images, images)
    report["models"]["clip-image"] = compare(left, right, cpu_time, gpu_time)
    vectors["clip-image"] = right
    if reference is not None:
        check_vectors(reference["clip-image"], right)
    cpu.unload()
    gpu.unload()
    report["reference_runtime_checked"] = bool(args.reference)
    if args.vectors_output:
        np.savez(args.vectors_output, **vectors)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
