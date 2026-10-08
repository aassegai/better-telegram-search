"""Private developer credentials and resource limits; safe defaults need no SDK."""

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from telegram_search.config.runtime import bundled_directory
from telegram_search.shared.errors import UserError
from telegram_search.telegram_sync.models import SourceFailure
from telegram_search.telegram_sync.paths import owned_path


@dataclass(frozen=True)
class SyncSettings:
    api_id: int = 0
    api_hash: str = field(default="", repr=False)
    reconcile_interval_minutes: int = 15
    reconcile_window_days: int = 7
    media_download_concurrency: int = 2
    max_image_mib: int = 40
    min_free_mib: int = 512
    media_quota_mib: int = 20_480
    pages_per_run: int = 20
    default_deletion_policy: str = "archive"
    download_photos: bool = True

    @property
    def configured(self):
        return (
            type(self.api_id) is int
            and 0 < self.api_id <= 2_147_483_647
            and len(self.api_hash) == 32
            and all(char in "0123456789abcdef" for char in self.api_hash.lower())
        )

    @classmethod
    def load(cls, workspace: Path):
        values = {}
        try:
            bundle = bundled_directory("telegram-app.json")
            if bundle and bundle.is_file():
                values.update(json.loads(bundle.read_text(encoding="utf-8")))
            path = owned_path(workspace.resolve(), "private/telegram.toml")
            if path.exists():
                values = tomllib.loads(path.read_text(encoding="utf-8")).get("telegram_sync", {})
                # Private settings override bundled developer application identifiers.
                if bundle and bundle.is_file():
                    values = {**json.loads(bundle.read_text(encoding="utf-8")), **values}
            if not isinstance(values, dict):
                raise ValueError("format")
        except (OSError, ValueError, TypeError, SourceFailure) as exc:
            raise UserError("Проверьте формат приватной конфигурации Telegram.") from exc
        allowed = set(cls.__dataclass_fields__)
        values = {key: value for key, value in values.items() if key in allowed}
        try:
            api_id = os.environ.get("BTS_TELEGRAM_API_ID", values.get("api_id", 0))
            if type(api_id) not in (str, int):
                raise ValueError("api_id")
            values["api_id"] = int(api_id)
            if not 0 <= values["api_id"] <= 2_147_483_647:
                raise ValueError("api_id")
            values["api_hash"] = os.environ.get("BTS_TELEGRAM_API_HASH", values.get("api_hash", ""))
            settings = cls(**values)
            for key, low, high in (
                ("reconcile_interval_minutes", 1, 1440),
                ("reconcile_window_days", 1, 30),
                ("media_download_concurrency", 1, 2),
                ("max_image_mib", 1, 100),
                ("min_free_mib", 64, 1_048_576),
                ("media_quota_mib", 1, 1_048_576),
                ("pages_per_run", 1, 100),
            ):
                value = getattr(settings, key)
                if type(value) is not int or not low <= value <= high:
                    raise ValueError(key)
            if settings.default_deletion_policy not in {"archive", "mirror"}:
                raise ValueError("deletion_policy")
            if not isinstance(settings.api_hash, str) or type(settings.download_photos) is not bool:
                raise ValueError("type")
            return settings
        except (TypeError, ValueError) as exc:
            raise UserError("Проверьте значения приватной конфигурации Telegram.") from exc
