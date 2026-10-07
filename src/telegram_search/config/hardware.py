import importlib.metadata
import platform
import shutil
from pathlib import Path

import psutil


def hardware_info(workspace: Path, *, device="cpu", backend=None) -> dict:
    """Read capabilities without allocating a model or probing a GPU."""
    try:
        import onnxruntime

        providers = onnxruntime.get_available_providers()
        version = onnxruntime.__version__
    except ImportError:
        providers, version = [], None
    installed = {item.metadata["Name"].lower() for item in importlib.metadata.distributions()}
    return {
        "platform": platform.system(),
        "architecture": platform.machine(),
        "logical_cpu_cores": psutil.cpu_count(),
        "ram_total_bytes": psutil.virtual_memory().total,
        "ram_available_bytes": psutil.virtual_memory().available,
        "disk_free_bytes": shutil.disk_usage(workspace).free,
        "runtime": "onnxruntime" if version else None,
        "runtime_version": version,
        "available_providers": providers,
        "selected_provider": backend.get("provider") if backend else None,
        "accelerators_probed": False,
        "device_policy": device,
        "torch_installed": bool(installed & {"torch", "torchvision", "torchaudio"}),
        "sentence_transformers_installed": "sentence-transformers" in installed,
    }
