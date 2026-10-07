"""ONNX execution selection; never imports a training framework.

Auto falls back before an embedding space is selected. A running encoder cannot
silently change providers and mix embeddings in a published index.
"""

import base64
import ctypes
import sys
import threading
from pathlib import Path

from telegram_search.shared.errors import UserError

CPU = "CPUExecutionProvider"
CUDA = "CUDAExecutionProvider"
COREML = "CoreMLExecutionProvider"
IDENTITY = base64.b64decode(
    "CAg6TAoQCgF4EgF5IghJZGVudGl0eRIOcG9ydGFibGUtc21va2VaEwoBeBIOCgwIARIICgIIAQoCCAJiEwoBeRIOCgwIARIICgIIAQoCCAJCBAoAEA0="
)
_libraries = []
_preload_lock = threading.Lock()
_preloaded = False


def runtime_version():
    import onnxruntime as ort

    return ort.__version__


def preload_cuda():
    global _preloaded
    with _preload_lock:
        if not _preloaded:
            _preload_cuda()
            _preloaded = True


def _preload_cuda():
    """Load only our NVIDIA runtime libraries, including inside a frozen bundle."""
    import onnxruntime as ort

    if getattr(sys, "frozen", False):
        root = Path(sys._MEIPASS) / "nvidia"
        folders = [Path(sys._MEIPASS), *sorted(root.glob("*/lib")), *sorted(root.glob("*/bin"))]
        if sys.platform == "win32":
            import os

            for folder in folders:
                _libraries.append(os.add_dll_directory(str(folder)))
        # Dependencies first; retaining handles keeps libraries loaded.
        for pattern in ("*cudart*", "*cublasLt*", "*cublas*", "*cudnn*"):
            for folder in folders:
                for path in sorted(folder.glob(pattern)):
                    if path.is_file() and (".so" in path.name or path.suffix == ".dll"):
                        try:
                            _libraries.append(ctypes.CDLL(str(path)))
                        except OSError:
                            pass
    elif hasattr(ort, "preload_dlls"):
        ort.preload_dlls(directory="")


def check_bundled_cuda():
    """Load the packaged provider dependencies without requiring a GPU/driver."""
    root = Path(sys._MEIPASS)
    suffix = "*.dll" if sys.platform == "win32" else "*.so*"
    for component in ("cudart", "cublasLt", "cublas", "cudnn", "nvrtc", "nvJitLink"):
        if not list(root.glob("*" + component + suffix)):
            raise RuntimeError("Missing bundled CUDA component: " + component)
    preload_cuda()
    provider_root = root / "onnxruntime/capi"
    shared = (
        "onnxruntime_providers_shared.dll"
        if sys.platform == "win32"
        else "libonnxruntime_providers_shared.so"
    )
    cuda = (
        "onnxruntime_providers_cuda.dll"
        if sys.platform == "win32"
        else "libonnxruntime_providers_cuda.so"
    )
    _libraries.append(ctypes.CDLL(str(provider_root / shared)))
    _libraries.append(ctypes.CDLL(str(provider_root / cuda)))


def options(threads):
    import onnxruntime as ort

    ort.disable_telemetry_events()
    value = ort.SessionOptions()
    value.intra_op_num_threads = threads
    value.inter_op_num_threads = 1
    value.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    value.log_severity_level = 3
    return value


class Execution:
    def __init__(self, device="cpu", *, device_id=0, memory_limit_mib=4096, threads=4, probe=True):
        if device not in {"cpu", "auto", "gpu"}:
            raise UserError("Недопустимый профиль устройства или версия настроек.")
        self.device = device
        self.device_id = device_id
        self.memory_limit_mib = memory_limit_mib
        self.threads = threads
        self.provider = CPU
        self.warning = None
        if device != "cpu":
            import onnxruntime as ort

            candidate = COREML if sys.platform == "darwin" else CUDA
            if not probe:
                self.provider = candidate
                return
            try:
                if candidate not in ort.get_available_providers():
                    raise RuntimeError("provider absent")
                self.provider = candidate
                if candidate == CUDA:
                    preload_cuda()
                import numpy as np

                session = self.session(IDENTITY)
                result = session.run(None, {"x": np.ones((1, 2), dtype=np.float32)})[0]
                if not np.array_equal(result, np.ones((1, 2), dtype=np.float32)):
                    raise RuntimeError("probe failed")
            except Exception as exc:
                self.provider = CPU
                if device == "gpu":
                    raise UserError(
                        "GPU недоступен. Используйте GPU-сборку и совместимый драйвер "
                        "или выберите CPU."
                    ) from exc
                self.warning = "GPU недоступен: режим Авто использует CPU."

    def session(self, model):
        import onnxruntime as ort

        providers = [CPU]
        if self.provider == CUDA:
            providers.insert(
                0,
                (
                    CUDA,
                    {
                        "device_id": str(self.device_id),
                        "gpu_mem_limit": str(self.memory_limit_mib * 1024**2),
                        "arena_extend_strategy": "kSameAsRequested",
                        "cudnn_conv_algo_search": "HEURISTIC",
                        "cudnn_conv_use_max_workspace": "0",
                        "use_tf32": "0",
                    },
                ),
            )
        elif self.provider == COREML:
            providers.insert(
                0,
                (
                    COREML,
                    {"MLComputeUnits": "CPUAndGPU", "AllowLowPrecisionAccumulationOnGPU": "0"},
                ),
            )
        try:
            if self.provider not in ort.get_available_providers():
                raise RuntimeError("provider absent")
            if self.provider == CUDA:
                preload_cuda()
            session = ort.InferenceSession(
                model, sess_options=options(self.threads), providers=providers
            )
            session.disable_fallback()
        except Exception as exc:
            raise UserError("Не удалось открыть ONNX-модель на выбранном устройстве.") from exc
        if session.get_providers()[0] != self.provider:
            raise UserError("ONNX Runtime не активировал выбранное устройство.")
        return session

    def info(self):
        return {
            "requested_device": self.device,
            "provider": self.provider,
            "device": "cpu" if self.provider == CPU else "gpu",
            "gpu_device_id": self.device_id,
            "gpu_memory_limit_mib": self.memory_limit_mib,
            "warning": self.warning,
        }
