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


@pytest.mark.parametrize(
    "index_age,query_age,encoding,paused,release_index,release_query",
    [
        (1, 1000, False, False, False, False),
        (1000, 1000, True, False, False, False),
        (1, 1, False, True, True, False),
        (6, 1, False, False, True, False),
        (1000, 1000, False, False, True, True),
    ],
)
def test_idle_cleanup_respects_ocr_passages_and_inflight_batches(
    monkeypatch, index_age, query_age, encoding, paused, release_index, release_query
):
    monkeypatch.setattr("telegram_search.inference.e5.time.monotonic", lambda: 2000)
    encoder = E5Encoder.__new__(E5Encoder)
    encoder.condition = threading.Condition()
    encoder.encoding = encoding
    encoder.last_index_used = 2000 - index_age
    encoder.session = object()
    encoder.query_session = object()
    index_session, query_session = encoder.session, encoder.query_session
    assert (
        encoder.unload_idle(query_last_used=2000 - query_age, idle_seconds=300, index_paused=paused)
        is release_query
    )
    assert encoder.session is (None if release_index else index_session)
    assert encoder.query_session is (None if release_query else query_session)


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
        assert spec.manifest["precision"] == ("mixed-fp16" if spec.profile == "berta" else "fp32")


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


@pytest.mark.parametrize(
    "message",
    [
        "Available memory of 978165760 is smaller than requested bytes of 1007769600",
        "cudaErrorMemoryAllocation",
        "CUBLAS_STATUS_ALLOC_FAILED",
    ],
)
def test_ort_allocation_errors_are_recognized_through_generic_wrappers(message):
    from telegram_search.inference.resources import memory_exhausted

    cause = RuntimeError(message)
    wrapped = UserError("Ошибка ONNX-инференса.")
    wrapped.__cause__ = cause
    assert memory_exhausted(wrapped)
    assert not memory_exhausted(UserError("synthetic unsupported operator"))


def test_auto_fallback_keeps_cpu_and_gpu_reserved_until_inference_finishes():
    from types import SimpleNamespace

    from telegram_search.inference.resources import compute_gate, compute_lock

    execution = SimpleNamespace(device="auto", provider="CUDAExecutionProvider", device_id=15)
    cpu_entered = threading.Event()
    cpu_waiting = threading.Event()

    def cpu_job():
        cpu_waiting.set()
        with compute_lock:
            cpu_entered.set()

    with compute_gate(execution):
        execution.provider = "CPUExecutionProvider"  # Session/child falls back.
        thread = threading.Thread(target=cpu_job)
        thread.start()
        assert cpu_waiting.wait(2)
        assert compute_lock.owner == threading.get_ident()
        assert not cpu_entered.is_set()
    thread.join(timeout=2)
    assert cpu_entered.is_set() and not thread.is_alive()
