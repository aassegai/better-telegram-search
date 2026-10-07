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
    memory_limit_mib: int = 4096
    search_result_limit: int = 20
    display_chunk_size: int = 10

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
