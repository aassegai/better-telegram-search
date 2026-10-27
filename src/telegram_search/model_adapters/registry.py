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
    if role == "reranker":
        return ModelSpec(
            profile,
            {
                "schema_version": 2,
                "runtime": "python-adapter",
                "adapter": "python-pairwise-v1",
                "role": "reranker",
                "model_id": "custom/" + data["id"],
                "revision": data["revision"],
                "adapter_identity": manifest.identity,
                "adapter_manifest": data,
                "adapter_environment": registration["environment"],
                "adapter_package_bytes": sum(f["bytes"] for f in data["files"]),
                "query_max_characters": data["input_contract"].get("query_max_characters", 8192),
                "document_max_characters": data["input_contract"].get(
                    "document_max_characters", 32768
                ),
                "files": [],
            },
        )
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


def custom_specs(workspace, role):
    result = {}
    try:
        registrations = AdapterStore(workspace).registrations()
    except UserError:
        return result
    for identity, record in registrations.items():
        if record["summary"].get("role") != role:
            continue
        profile = "custom:" + identity
        try:
            result[profile] = custom_spec(workspace, profile, role=role)
        except (UserError, OSError, ValueError, KeyError):
            # A damaged local package must not hide working built-in models.
            continue
    return result


def custom_text_specs(workspace):
    return custom_specs(workspace, "text_embedding")
