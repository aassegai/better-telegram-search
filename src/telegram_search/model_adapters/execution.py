"""Device/resource metadata for external adapters; no scientific imports."""

import sys

from telegram_search.inference.providers import COREML, CPU, CUDA
from telegram_search.shared.errors import UserError


class AdapterExecution:
    def __init__(self, device, settings, supported):
        self.device = device
        gpu = COREML if sys.platform == "darwin" else CUDA
        self.provider = gpu if device != "cpu" and "gpu" in supported else CPU
        if device == "gpu" and self.provider == CPU:
            raise UserError("The custom model does not support GPU.")
        if self.provider == CPU and "cpu" not in supported:
            raise UserError("The custom model does not support CPU.")
        self.device_id = settings.gpu_device_id
        self.memory_limit_mib = settings.gpu_memory_limit_mib
        self.threads = settings.cpu_threads
        self.warning = None

    def info(self):
        return {
            "requested_device": self.device,
            "device": "cpu" if self.provider == CPU else "gpu",
            "provider": "custom:cpu" if self.provider == CPU else "custom:gpu",
            "gpu_device_id": self.device_id,
            "gpu_memory_limit_mib": self.memory_limit_mib,
            "warning": self.warning,
        }
