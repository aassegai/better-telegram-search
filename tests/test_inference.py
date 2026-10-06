import hashlib
import importlib.metadata
import threading

import pytest

from telegram_search.config.model_registry import ModelSpec, registry
from telegram_search.inference.bundles import BundleStore
from telegram_search.shared.errors import UserError

np = pytest.importorskip("numpy")
pytest.importorskip("huggingface_hub")
from telegram_search.inference.e5 import E5Encoder, masked_mean_normalize  # noqa: E402
from telegram_search.search.vectors import VectorStore  # noqa: E402


def test_runtime_dependency_graph_has_no_torch():
    installed = {item.metadata["Name"].lower() for item in importlib.metadata.distributions()}
    assert not installed & {"torch", "torchvision", "torchaudio", "sentence-transformers"}


def test_suspended_encoder_cannot_reload_a_session():
    encoder = E5Encoder.__new__(E5Encoder)
    encoder.condition = threading.Condition()
    encoder.encoding = False
    encoder.suspended = False
    encoder.interactive_waiters = 0
    encoder.session = object()
    encoder.suspend()
    assert encoder.session is None
    with pytest.raises(UserError, match="временно остановлена"):
        encoder.encode_text(["synthetic"], "query", interactive=True)
    assert not encoder.encoding and encoder.interactive_waiters == 0
    encoder.resume()
    assert not encoder.suspended


def test_rejected_last_interactive_request_notifies_background_waiter():
    encoder = E5Encoder.__new__(E5Encoder)
    encoder.condition = threading.Condition()
    encoder.encoding = False
    encoder.suspended = True
    encoder.interactive_waiters = 0
    waiting = threading.Event()
    notified = []

    def waiter():
        with encoder.condition:
            waiting.set()
            notified.append(encoder.condition.wait(timeout=1))

    background = threading.Thread(target=waiter)
    background.start()
    assert waiting.wait(timeout=1)
    with pytest.raises(UserError, match="временно остановлена"):
        encoder.encode_text(["synthetic"], "query", interactive=True)
    background.join(timeout=2)
    assert notified == [True] and encoder.interactive_waiters == 0


def test_masked_pooling_ignores_padding_and_normalizes_float32():
    hidden = np.array([[[3, 4], [900, -900]], [[3, 4], [3, 4]]], dtype=np.float32)
    values = masked_mean_normalize(hidden, np.array([[1, 0], [1, 1]]))
    np.testing.assert_allclose(values, [[0.6, 0.8], [0.6, 0.8]], atol=1e-6)
    assert values.dtype == np.float32
    with pytest.raises(UserError):
        masked_mean_normalize(hidden, np.zeros((2, 2)))
    with pytest.raises(UserError):
        masked_mean_normalize(hidden, np.ones((2, 1)))


def test_registry_is_pinned_and_covers_required_artifacts():
    for spec in registry().values():
        assert len(spec.revision) == 40 and spec.revision != "main"
        assert spec.manifest["tokenizer_revision"] == spec.revision
        names = {item["name"] for item in spec.manifest["files"]}
        assert {"onnx/model.onnx", "tokenizer.json", "tokenizer_config.json"} <= names
        assert all(
            len(item["sha256"]) == 64 and item["bytes"] > 0 for item in spec.manifest["files"]
        )
        assert spec.manifest["precision"] == "fp32"


def test_bundle_publication_is_atomic_and_hashes_are_checked(tmp_path, monkeypatch):
    import huggingface_hub

    payloads = {"onnx/model.onnx": b"synthetic model", "tokenizer.json": b"synthetic tokenizer"}
    spec = ModelSpec(
        "synthetic",
        {
            "model_id": "synthetic/public",
            "revision": "a" * 40,
            "files": [
                {"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in payloads.items()
            ],
        },
    )
    store = BundleStore(tmp_path / "workspace")
    calls = []

    def download(model, filename, **kwargs):
        assert model == spec.model_id and kwargs["revision"] == spec.revision
        calls.append(filename)
        cached = tmp_path / "source" / filename
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(payloads[filename])
        return str(cached)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    events = []
    bundle = store.prepare(spec, progress=events.append)
    assert store.verify(spec) == bundle and len(calls) == 2
    assert events[-1]["completed_bytes"] == sum(map(len, payloads.values()))
    store.prepare(spec, offline=True)
    assert len(calls) == 2
    (bundle / "tokenizer.json").write_bytes(b"changed")
    with pytest.raises(UserError, match="повреждён"):
        store.verify(spec)


def test_failed_download_never_publishes_partial_bundle(tmp_path, monkeypatch):
    import huggingface_hub

    files = [
        {"name": "onnx/model.onnx", "bytes": 5, "sha256": hashlib.sha256(b"hello").hexdigest()}
    ]
    spec = ModelSpec(
        "synthetic", {"model_id": "synthetic/public", "revision": "b" * 40, "files": files}
    )
    store = BundleStore(tmp_path / "workspace")

    def fail(*args, **kwargs):
        raise OSError("synthetic network unavailable")

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fail)
    with pytest.raises(UserError, match="Не удалось получить"):
        store.prepare(spec)
    assert not store.path(spec).exists()
    assert not list(store.path(spec).parent.glob(".preparing-*"))


def test_exact_prefilter_finds_tail_and_idempotent_vectors_compact(tmp_path):
    pytest.importorskip("lancedb")
    store = VectorStore(tmp_path)
    rows = [
        {"id": f"synthetic-{i}", "chat_id": "a", "utc_day": "2025-06-15", "generation": 1}
        for i in range(200)
    ]
    embeddings = np.array([[1.0, i / 100] for i in range(200)], dtype=np.float32)
    embeddings /= np.linalg.norm(embeddings, axis=1, keepdims=True)
    store.upsert("a" * 64, 2, rows, embeddings)
    store.upsert("a" * 64, 2, rows, embeddings)
    table = store.table("a" * 64, 2)
    assert table.count_rows() == 200
    # Globally this is outside top100. Prefilter must rank it before limit.
    hits = store.exact("a" * 64, 2, np.array([1.0, 0.0]), ["synthetic-199"], 1)
    assert hits[0]["id"] == "synthetic-199"
    store.delete_chat([{"id": "a" * 64, "dimension": 2}], "a")
    store.compact([{"id": "a" * 64, "dimension": 2}])
    assert store.table("a" * 64, 2).count_rows() == 0
