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


def registry(workspace=None) -> dict[str, ModelSpec]:
    manifest = json.loads(files("telegram_search.config").joinpath("models.json").read_text())
    result = {profile: ModelSpec(profile, value) for profile, value in manifest.items()}
    if workspace is not None:
        from telegram_search.model_adapters.registry import custom_text_specs

        result.update(custom_text_specs(workspace))
    return result


def model_spec(profile: str, workspace=None) -> ModelSpec:
    if workspace is not None and isinstance(profile, str) and profile.startswith("custom:"):
        from telegram_search.model_adapters.registry import custom_spec

        return custom_spec(workspace, profile)
    try:
        return registry()[profile]
    except KeyError as exc:
        raise UserError("Неизвестный профиль текстовой модели.") from exc


def media_registry() -> dict[str, ModelSpec]:
    manifest = json.loads(files("telegram_search.config").joinpath("media_models.json").read_text())
    return {profile: ModelSpec(profile, value) for profile, value in manifest.items()}


def visual_specs(
    profile="clip", workspace=None, *, allow_unavailable=False
) -> dict[str, ModelSpec]:
    if isinstance(profile, str) and profile.startswith("custom:") and workspace is not None:
        from telegram_search.model_adapters.registry import custom_spec

        try:
            return {profile: custom_spec(workspace, profile, role="image_embedding")}
        except (UserError, OSError, ValueError, KeyError):
            if not allow_unavailable:
                raise
            return {
                profile: ModelSpec(
                    profile,
                    {
                        "runtime": "python-adapter",
                        "role": "image",
                        "model_id": profile,
                        "revision": "unavailable",
                        "unavailable": True,
                        "files": [],
                    },
                )
            }
    if profile == "clip":
        return media_registry()
    if profile == "siglip2":
        manifest = json.loads(
            files("telegram_search.config").joinpath("siglip2_model.json").read_text()
        )
        return {profile: ModelSpec(profile, manifest)}
    raise UserError("Неизвестный профиль визуальной модели.")


def rerank_registry(workspace=None) -> dict[str, ModelSpec]:
    manifest = json.loads(files("telegram_search.config").joinpath("rerank_model.json").read_text())
    jina = json.loads(
        files("telegram_search.config").joinpath("jina_rerank_model.json").read_text()
    )
    result = {"giga": ModelSpec("giga", manifest), "jina-v2": ModelSpec("jina-v2", jina)}
    if workspace is not None:
        from telegram_search.model_adapters.registry import custom_specs

        result.update(custom_specs(workspace, "reranker"))
    return result


def rerank_spec(profile="giga", workspace=None) -> ModelSpec:
    if workspace is not None and isinstance(profile, str) and profile.startswith("custom:"):
        from telegram_search.model_adapters.registry import custom_spec

        try:
            return custom_spec(workspace, profile, role="reranker")
        except (UserError, OSError, ValueError, KeyError):
            # A damaged optional model must leave base search and model settings usable.
            return ModelSpec(
                profile,
                {
                    "runtime": "python-adapter",
                    "role": "reranker",
                    "model_id": profile,
                    "revision": "unavailable",
                    "unavailable": True,
                    "files": [],
                },
            )
    try:
        return rerank_registry()[profile]
    except (KeyError, TypeError) as exc:
        raise UserError("Неизвестная модель переранжирования.") from exc


def ocr_spec() -> ModelSpec:
    manifest = json.loads(
        files("telegram_search.config").joinpath("ocr_onnx_model.json").read_text()
    )
    return ModelSpec("paddle-ru-en", manifest)
