import json
from dataclasses import asdict, dataclass
from pathlib import Path

from telegram_search.shared.errors import UserError


@dataclass(frozen=True)
class Settings:
    version: int = 1
    device: str = "cpu"
    search_backend: str = "fts5_messages"
    cpu_threads: int = 4
    embedding_batch: int = 4
    idle_unload_seconds: int = 300
    query_cache_entries: int = 64
    retrieval_candidates: int = 100
    rrf_k: int = 60
    lexical_weight: float = 1.0
    dense_weight: float = 1.0

    @classmethod
    def load(cls, workspace: Path) -> "Settings":
        path = workspace / "config.json"
        if not path.exists():
            settings = cls()
            path.write_text(json.dumps(asdict(settings)), encoding="utf-8")
            return settings
        try:
            settings = cls(**json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError) as exc:
            raise UserError("Не удалось прочитать настройки workspace.") from exc
        if (
            type(settings.version) is not int
            or settings.version != 1
            or settings.device != "cpu"
            or settings.search_backend != "fts5_messages"
        ):
            raise UserError("Настройки не поддерживаются: сейчас доступен только профиль CPU.")
        for name, lower, upper in (
            ("cpu_threads", 1, 32),
            ("embedding_batch", 1, 32),
            ("idle_unload_seconds", 1, 86400),
            ("query_cache_entries", 0, 256),
            ("retrieval_candidates", 100, 1000),
            ("rrf_k", 1, 1000),
        ):
            value = getattr(settings, name)
            if type(value) is not int or not lower <= value <= upper:
                raise UserError(f"Недопустимое значение настройки {name}.")
        for name in ("lexical_weight", "dense_weight"):
            value = getattr(settings, name)
            if type(value) not in (int, float) or not 0 < value <= 100:
                raise UserError(f"Недопустимое значение настройки {name}.")
        return settings
