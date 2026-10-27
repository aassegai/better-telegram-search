"""Role-aware model specs for checked local packages, without executing adapters."""

from telegram_search.config.model_registry import ModelSpec
from telegram_search.model_adapters.store import AdapterStore
from telegram_search.shared.errors import UserError


def custom_spec(workspace, profile, *, role="text_embedding"):
    if not isinstance(profile, str) or not profile.startswith("custom:"):
        raise UserError("Invalid custom model profile.")
    manifest, _package, registration = AdapterStore(workspace).get(profile.removeprefix("custom:"))
    if manifest.role != role:
        raise UserError("The custom model has a different role.")
    data = manifest.data
    if role != "text_embedding":
        raise UserError("This adapter role cannot be used as a text index.")
    return ModelSpec(
        profile,
        {
            "schema_version": 2,
            "runtime": "python-adapter",
            "runtime_dtype": "float32",
            "role": "text",
            "model_id": "custom/" + data["id"],
            "revision": data["revision"],
            "dimension": data["output_contract"]["dimension"],
            "precision": "custom-float32",
            "output_dtype": "float32",
            "storage_dtype": "float32",
            "normalization": "l2",
            "pooling": "adapter",
            "query_prefix": data["preprocessing"]["query_prefix"],
            "passage_prefix": data["preprocessing"]["document_prefix"],
            "chunk_max_tokens": min(480, data["input_contract"]["document_max_tokens"]),
            "query_max_tokens": data["input_contract"]["query_max_tokens"],
            "preprocessing_version": data["preprocessing"]["version"],
            "adapter_identity": manifest.identity,
            "adapter_manifest": data,
            "adapter_environment": registration["environment"],
            "files": [],
        },
    )


def custom_text_specs(workspace):
    result = {}
    try:
        registrations = AdapterStore(workspace).registrations()
    except UserError:
        return result
    for identity, record in registrations.items():
        if record["summary"].get("role") != "text_embedding":
            continue
        profile = "custom:" + identity
        try:
            result[profile] = custom_spec(workspace, profile)
        except (UserError, OSError, ValueError, KeyError):
            # A damaged local package must not hide working built-in models.
            continue
    return result
