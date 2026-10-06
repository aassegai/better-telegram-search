import json
from dataclasses import asdict, dataclass
from pathlib import Path

from telegram_search.shared.errors import UserError


@dataclass(frozen=True)
class Settings:
    version: int = 1
    device: str = "cpu"
    search_backend: str = "fts5_messages"

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
        return settings
