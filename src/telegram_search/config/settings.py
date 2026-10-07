import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from telegram_search.shared.errors import UserError


@dataclass(frozen=True)
class Settings:
    version: int = 1
    device: str = "cpu"
    search_device: str = "cpu"
    e5_device: str | None = None
    e5_search_device: str | None = None
    clip_device: str | None = None
    clip_search_device: str | None = None
    ocr_device: str = "cpu"
    ocr_engine: str = "tesseract"
    gpu_device_id: int = 0
    gpu_memory_limit_mib: int = 4096
    search_backend: str = "fts5_messages"
    cpu_threads: int = 4
    embedding_batch: int = 4
    idle_unload_seconds: int = 300
    query_cache_entries: int = 64
    retrieval_candidates: int = 100
    rrf_k: int = 60
    lexical_weight: float = 1.0
    dense_weight: float = 1.0
    image_batch: int = 1
    ocr_timeout_seconds: int = 45
    ocr_max_edge: int = 2400
    ocr_batch_size: int = 1
    ocr_region_batch_size: int = 0
    ocr_cpu_workers: int = 1
    memory_limit_mib: int = 4096
    search_result_limit: int = 20
    display_chunk_size: int = 10

    def model_device(self, model, *, query=False):
        if model == "ocr":
            return self.ocr_device
        suffix = "search_device" if query else "device"
        return getattr(self, f"{model}_{suffix}") or getattr(self, suffix)

    @classmethod
    def load(cls, workspace: Path) -> "Settings":
        path = workspace / "config.json"
        if path.is_symlink() or not path.resolve().is_relative_to(workspace.resolve()):
            raise UserError("Настройки должны находиться внутри workspace.")
        if not path.exists():
            settings = cls()
            settings.save(workspace)
            return settings
        try:
            settings = cls(**json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError) as exc:
            raise UserError("Не удалось прочитать настройки workspace.") from exc
        settings.validate()
        return settings

    def validate(self) -> None:
        settings = self
        for name in ("e5_device", "e5_search_device", "clip_device", "clip_search_device"):
            value = getattr(settings, name)
            if value is not None and (
                type(value) is not str or value not in {"cpu", "auto", "gpu"}
            ):
                raise UserError("Недопустимый профиль устройства или версия настроек.")
        if (
            type(settings.ocr_device) is not str
            or settings.ocr_device not in {"cpu", "auto", "gpu", "hybrid"}
            or type(settings.ocr_engine) is not str
            or settings.ocr_engine not in {"tesseract", "paddle"}
            or (settings.ocr_engine == "tesseract" and settings.ocr_device != "cpu")
        ):
            raise UserError("Tesseract работает на CPU. Для GPU выберите PaddleOCR.")
        if (
            settings.ocr_device == "hybrid"
            and type(settings.cpu_threads) is int
            and settings.cpu_threads < 2
        ):
            raise UserError("Для OCR на CPU + GPU выберите не менее двух CPU-потоков.")
        if (
            type(settings.version) is not int
            or settings.version != 1
            or type(settings.device) is not str
            or settings.device not in {"cpu", "auto", "gpu"}
            or type(settings.search_device) is not str
            or settings.search_device not in {"cpu", "auto", "gpu"}
            or settings.search_backend != "fts5_messages"
        ):
            raise UserError("Недопустимый профиль устройства или версия настроек.")
        for name, lower, upper in (
            ("cpu_threads", 1, 32),
            ("gpu_device_id", 0, 15),
            ("gpu_memory_limit_mib", 512, 65536),
            ("embedding_batch", 1, 128),
            ("idle_unload_seconds", 1, 86400),
            ("query_cache_entries", 0, 256),
            ("retrieval_candidates", 100, 1000),
            ("rrf_k", 1, 1000),
            ("image_batch", 1, 32),
            ("ocr_timeout_seconds", 5, 300),
            ("ocr_max_edge", 512, 4096),
            ("ocr_batch_size", 1, 4),
            ("ocr_region_batch_size", 0, 32),
            ("ocr_cpu_workers", 1, 4),
            ("memory_limit_mib", 1024, 65536),
            ("search_result_limit", 1, 100),
            ("display_chunk_size", 1, 100),
        ):
            value = getattr(settings, name)
            if type(value) is not int or not lower <= value <= upper:
                raise UserError(f"Недопустимое значение настройки {name}.")
        for name in ("lexical_weight", "dense_weight"):
            value = getattr(settings, name)
            if type(value) not in (int, float) or not 0 < value <= 100:
                raise UserError(f"Недопустимое значение настройки {name}.")
        if settings.ocr_cpu_workers > settings.cpu_threads:
            raise UserError("Число OCR-воркеров не должно превышать бюджет потоков CPU.")
        if settings.ocr_cpu_workers > 1 and settings.ocr_device != "cpu":
            raise UserError("Несколько CPU OCR-воркеров доступны при выборе устройства CPU.")

    def save(self, workspace: Path) -> None:
        self.validate()
        path = workspace / "config.json"
        if path.is_symlink() or not path.resolve().is_relative_to(workspace.resolve()):
            raise UserError("Настройки должны находиться внутри workspace.")
        fd, temporary = tempfile.mkstemp(dir=workspace, prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(asdict(self), out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
