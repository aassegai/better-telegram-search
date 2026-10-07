import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files

from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize


@dataclass(frozen=True)
class ModelSpec:
    profile: str
    manifest: dict

    @property
    def revision(self) -> str:
        return self.manifest["revision"]

    @property
    def model_id(self) -> str:
        return self.manifest["model_id"]

    @property
    def dimension(self) -> int:
        return self.manifest["dimension"]

    @property
    def download_bytes(self) -> int:
        return sum(item["bytes"] for item in self.manifest["files"])

    @property
    def identity(self) -> str:
        return hashlib.sha256(serialize(self.manifest).encode()).hexdigest()


def registry() -> dict[str, ModelSpec]:
    manifest = json.loads(files("telegram_search.config").joinpath("models.json").read_text())
    return {profile: ModelSpec(profile, value) for profile, value in manifest.items()}


def model_spec(profile: str) -> ModelSpec:
    try:
        return registry()[profile]
    except KeyError as exc:
        raise UserError("Неизвестный профиль модели. Выберите small или base.") from exc


def media_registry() -> dict[str, ModelSpec]:
    manifest = json.loads(files("telegram_search.config").joinpath("media_models.json").read_text())
    return {profile: ModelSpec(profile, value) for profile, value in manifest.items()}


def ocr_spec() -> ModelSpec:
    manifest = json.loads(
        files("telegram_search.config").joinpath("ocr_onnx_model.json").read_text()
    )
    return ModelSpec("paddle-ru-en", manifest)
