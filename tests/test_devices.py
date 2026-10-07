from dataclasses import replace
from types import SimpleNamespace

import pytest

from telegram_search.config.settings import Settings
from telegram_search.inference.providers import COREML, CPU, CUDA, Execution
from telegram_search.shared.errors import UserError


def test_device_settings_are_backward_compatible_and_validate_limits(tmp_path):
    old = Settings()
    old.save(tmp_path)
    assert Settings.load(tmp_path).device == "cpu"
    for device in ("cpu", "auto", "gpu"):
        replace(old, device=device).validate()
    for values in (
        {"device": "cuda"},
        {"gpu_device_id": -1},
        {"gpu_device_id": True},
        {"gpu_memory_limit_mib": 0},
        {"gpu_memory_limit_mib": 1.5},
    ):
        with pytest.raises(UserError):
            replace(old, **values).validate()


def test_auto_cpu_fallback_and_strict_gpu_are_distinct(monkeypatch):
    import sys

    monkeypatch.setitem(
        sys.modules, "onnxruntime", SimpleNamespace(get_available_providers=lambda: [CPU])
    )
    auto = Execution("auto")
    assert auto.provider == CPU and auto.warning
    with pytest.raises(UserError, match="GPU недоступен"):
        Execution("gpu")


def test_runtime_cannot_silently_fall_back_and_cuda_disables_tf32(monkeypatch):
    np = pytest.importorskip("numpy")
    import sys

    from telegram_search.inference import providers

    monkeypatch.setattr(providers.sys, "platform", "linux")

    requests = []

    class Session:
        fallback = False
        disabled = False

        def __init__(self, model, sess_options, providers):
            requests.append(providers)

        def get_providers(self):
            return [CPU] if self.fallback else [CUDA, CPU]

        def disable_fallback(self):
            self.disabled = True

        def run(self, outputs, feed):
            return [np.ones((1, 2), dtype=np.float32)]

    runtime = SimpleNamespace(
        get_available_providers=lambda: [CUDA, CPU],
        disable_telemetry_events=lambda: None,
        SessionOptions=SimpleNamespace,
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL=0),
        InferenceSession=Session,
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", runtime)
    monkeypatch.setattr(providers, "preload_cuda", lambda: None)
    gpu = Execution("gpu", device_id=1, memory_limit_mib=1024)
    assert gpu.session(b"synthetic graph").disabled
    assert requests[0][0][1]["use_tf32"] == "0"
    assert requests[0][0][1]["gpu_mem_limit"] == str(1024**3)
    assert requests[0][0][1]["device_id"] == "1"
    Session.fallback = True
    with pytest.raises(UserError):
        gpu.session(b"synthetic graph")
    assert Execution("auto").provider == CPU
    with pytest.raises(UserError):
        Execution("gpu")


def test_cpu_and_gpu_share_the_same_model_space_and_keep_execution_provenance(
    tmp_path, monkeypatch
):
    pytest.importorskip("numpy")
    pytest.importorskip("tokenizers")
    from telegram_search.config.model_registry import registry
    from telegram_search.inference import e5

    monkeypatch.setattr(e5, "ModelTokenizer", lambda path: object())
    monkeypatch.setattr(
        e5,
        "Execution",
        lambda device, **kwargs: SimpleNamespace(provider=CPU if device == "cpu" else CUDA),
    )
    monkeypatch.setattr(e5, "runtime_version", lambda: "synthetic-runtime")
    cpu = e5.E5Encoder(registry()["small"], tmp_path)
    gpu = e5.E5Encoder(registry()["small"], tmp_path, device="gpu")
    assert cpu.space_id == gpu.space_id
    assert cpu.space_manifest["runtime"]["provider"] == CPU
    assert gpu.space_manifest["runtime"]["provider"] == CPU
    assert gpu.runtime["provider"] == CUDA


def test_legacy_cpu_reference_is_preserved_across_pinned_runtime_versions(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    from telegram_search.config.model_registry import registry
    from telegram_search.inference import e5

    monkeypatch.setattr(e5, "ModelTokenizer", lambda path: object())
    monkeypatch.setattr(e5, "Execution", lambda device, **kw: SimpleNamespace(provider=CPU))
    monkeypatch.setattr(e5, "runtime_version", lambda: "1.30.0")
    old = e5.E5Encoder(registry()["small"], tmp_path)
    monkeypatch.setattr(e5, "runtime_version", lambda: "1.23.2")
    new = e5.E5Encoder(registry()["small"], tmp_path)
    assert old.space_id != new.space_id
    new.adopt_space(old.space_manifest)
    assert new.space_id == old.space_id and new.space_manifest == old.space_manifest
    other_model = e5.E5Encoder(registry()["base"], tmp_path)
    with pytest.raises(UserError):
        other_model.adopt_space(old.space_manifest)
    old.space_manifest["runtime"]["dtype"] = "float16"
    with pytest.raises(UserError):
        new.adopt_space(old.space_manifest)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
def test_precision_gate_rejects_non_finite_vectors(invalid):
    np = pytest.importorskip("numpy")
    from telegram_search.inference.compatibility import check_vectors

    with pytest.raises(UserError):
        check_vectors(np.array([[1.0, 0.0]]), np.array([[1.0, invalid]]))


def test_lazy_gpu_selection_does_not_probe_or_load_cuda_on_cpu_search_startup(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace())
    gpu = Execution("gpu", probe=False)
    assert gpu.provider == (COREML if sys.platform == "darwin" else CUDA)


def test_queries_use_cpu_sessions_and_unloading_index_keeps_query_session(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    from telegram_search.config.model_registry import registry
    from telegram_search.inference import e5

    calls = []
    spec = registry()["small"]

    class Session:
        def get_providers(self):
            return [CPU]

        def get_inputs(self):
            return [
                SimpleNamespace(name=name, type="tensor(int64)")
                for name in spec.manifest["required_inputs"]
            ]

        def get_outputs(self):
            return [SimpleNamespace(name=spec.manifest["output_name"], type="tensor(float)")]

        def run(self, outputs, feed):
            shape = (*feed["input_ids"].shape, spec.dimension)
            return [np.ones(shape, dtype=np.float32)]

    class Device:
        def __init__(self, device, **kwargs):
            self.device = device
            self.provider = CPU if device == "cpu" else CUDA

        def session(self, model):
            calls.append(self.device)
            return Session()

    def batch(texts, limit):
        return {
            name: np.ones((len(texts), 3), dtype=np.int64)
            for name in spec.manifest["allowed_inputs"]
        }

    monkeypatch.setattr(e5, "Execution", Device)
    monkeypatch.setattr(e5, "ModelTokenizer", lambda path: SimpleNamespace(batch=batch))
    encoder = e5.E5Encoder(spec, tmp_path, device="gpu", search_device="cpu")
    assert calls == []
    encoder.encode_text(["public query"], "query")
    assert calls == ["cpu"] and encoder.session is None
    cpu_session = encoder.query_session
    encoder.encode_text(["public passage"], "passage")
    assert "gpu" in calls and encoder.session is not None
    encoder.unload_index()
    assert encoder.session is None and encoder.query_session is cpu_session
    before = len(calls)
    encoder.encode_text(["another query"], "query")
    assert len(calls) == before
