"""Synthetic contracts and lifecycle checks; no downloaded weights or archives."""

import hashlib
import io
import json
import subprocess
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import export, load, message

from telegram_search.config.model_registry import model_spec, rerank_spec, visual_specs
from telegram_search.config.settings import Settings
from telegram_search.search.cache import SearchCache
from telegram_search.shared.errors import UserError


def test_models_have_separate_roles_pinned_provenance_and_legacy_identity():
    berta, giga = model_spec("berta"), rerank_spec()
    siglip = visual_specs("siglip2")["siglip2"]
    assert [s.dimension for s in (berta, giga, siglip)] == [768, 1024, 768]
    assert [s.manifest["role"] for s in (berta, giga, siglip)] == ["text", "rerank", "visual"]
    assert berta.manifest["query_prefix"] == "search_query: "
    assert berta.manifest["passage_prefix"] == "search_document: "
    assert giga.manifest["passage_prefix"] == ""
    assert giga.manifest["query_prefix"].endswith("\nQuery: ")
    assert giga.manifest["attention"] == "bidirectional-no-cache"
    for spec in (berta, giga, siglip):
        assert spec.manifest["weight_dtype"] == "float16"
        assert spec.manifest["storage_dtype"] == "float32"
        assert len(spec.revision) == 40
        assert all(len(f["sha256"]) == 64 and f["bytes"] > 0 for f in spec.manifest["files"])
    with pytest.raises(UserError):
        model_spec("giga")
    # Pin the old JSON semantics against the last published implementation.
    # Copying a manifest into a new profile must never rename existing spaces.
    assert (
        model_spec("small").identity
        == "f4abd1c2060c067266d88c7a60e889a7aadb3bbbafd52bb04a5926ae60e2c959"
    )
    assert (
        model_spec("base").identity
        == "5f9daa0aa37c1774ccaed959599ecf422b57b6364afa90420703dc3cf91775b1"
    )


def test_basic_api_import_and_lifespan_need_no_ml_dependencies(tmp_path):
    program = """
import importlib.abc, importlib.util, sys
blocked = {'numpy', 'onnxruntime', 'tokenizers', 'torch', 'transformers'}
original = importlib.util.find_spec
def find_spec(name, *args):
    return None if name.split('.')[0] in blocked else original(name, *args)
importlib.util.find_spec = find_spec
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in blocked:
            raise ImportError('Optional dependency imported: ' + fullname)
sys.meta_path.insert(0, Block())
from pathlib import Path
from fastapi.testclient import TestClient
from telegram_search.backend.api import create_app
with TestClient(create_app(Path(sys.argv[1])), base_url='http://127.0.0.1') as client:
    assert client.get('/api/semantic').json()['runtime_installed'] is False
    assert client.get('/api/rerank').json()['enabled'] is False
    assert client.get('/api/chats').status_code == 200
"""
    subprocess.run(
        [sys.executable, "-c", program, str(tmp_path / "minimal")],
        check=True,
        timeout=30,
        capture_output=True,
    )


def test_new_settings_load_old_configs_without_changing_devices_batches_or_pauses(tmp_path):
    old = Settings(device="gpu", search_device="cpu", embedding_batch=32, image_batch=8)
    old.save(tmp_path)
    values = json.loads((tmp_path / "config.json").read_text())
    del values["giga_rerank_enabled"]
    (tmp_path / "config.json").write_text(json.dumps(values))
    restored = Settings.load(tmp_path)
    assert restored == old and not restored.giga_rerank_enabled
    with pytest.raises(UserError):
        replace(restored, giga_rerank_enabled=1).validate()


def test_rerank_toggle_invalidates_snapshot_without_reindex(db, importer, tmp_path):
    load(importer, export(tmp_path / "source", [message()]))
    with db.connect() as conn:
        before = [tuple(r) for r in conn.execute("SELECT * FROM index_segments")]
        state = tuple(conn.execute("SELECT * FROM semantic_state").fetchone())
    revision = SearchCache.revision(db)
    db.settings = replace(db.settings, giga_rerank_enabled=True)
    assert SearchCache.revision(db) != revision
    with db.connect() as conn:
        assert before == [tuple(r) for r in conn.execute("SELECT * FROM index_segments")]
        assert state == tuple(conn.execute("SELECT * FROM semantic_state").fetchone())


def test_release_model_cache_is_verified_offline_and_rejects_wrong_sources(tmp_path, monkeypatch):
    from telegram_search.inference.bundles import BundleStore
    from telegram_search.updates import network

    payload = b"synthetic ONNX artifact"
    item = dict(
        url="https://github.com/aassegai/better-telegram-search/releases/download/test/graph",
        sha256=hashlib.sha256(payload).hexdigest(),
        bytes=len(payload),
    )
    store = BundleStore(tmp_path)
    store.root.mkdir()
    monkeypatch.setattr(network, "open_url", lambda url: io.BytesIO(payload))
    path = store._release_file(item, offline=False, report=lambda n: None)
    assert path.read_bytes() == payload
    monkeypatch.setattr(network, "open_url", lambda url: pytest.fail("offline network access"))
    assert store._release_file(item, offline=True, report=lambda n: None) == path
    path.write_bytes(b"corrupt")
    with pytest.raises(UserError):
        store._release_file(item, offline=True, report=lambda n: None)
    with pytest.raises(UserError):
        store._release_file(
            {**item, "url": "https://attacker.invalid/model"}, offline=False, report=lambda n: None
        )


@pytest.fixture
def np():
    return pytest.importorskip("numpy")


def test_siglip_preprocessing_uses_direct_bilinear_resize_and_original_pixel_guard(np):
    from PIL import Image

    from telegram_search.inference.siglip import image_tensor

    image = Image.new("RGB", (400, 100), (250, 10, 30))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    actual = image_tensor(stream.getvalue())
    expected = np.asarray(image.resize((224, 224), Image.Resampling.BILINEAR), dtype=np.float32)
    expected = (expected * np.float32(1 / 255) - np.float32(0.5)) / np.float32(0.5)
    np.testing.assert_array_equal(actual, expected.transpose(2, 0, 1))
    assert actual.shape == (3, 224, 224) and actual.dtype == np.float32
    with pytest.raises(UserError):
        image_tensor(b"not an image")


def test_fp16_pooling_accumulates_in_float32(np):
    from telegram_search.inference.e5 import masked_mean_normalize

    hidden = np.array([[[50000, 50000], [50000, 50000]]], dtype=np.float16)
    result = masked_mean_normalize(hidden, np.ones((1, 2)))
    np.testing.assert_allclose(result, [[2**-0.5, 2**-0.5]], atol=1e-6)
    assert result.dtype == np.float32


def test_rerank_orders_prefix_keeps_tail_and_handles_failure(db, importer, tmp_path, np):
    from telegram_search.search.lexical import Filters
    from telegram_search.search.rerank import SearchReranker

    load(importer, export(tmp_path / "source", [message(1, "irrelevant"), message(2, "relevant")]))
    with db.connect() as conn:
        chat = conn.execute("SELECT id FROM chats").fetchone()[0]
    encoder = SimpleNamespace(
        session=object(),
        query_session=object(),
        tokenizer=SimpleNamespace(count=lambda text: len(text)),
        unload=lambda: None,
    )
    calls = []

    def encode(texts, purpose, **kwargs):
        calls.append((texts, purpose))
        return np.array(
            [
                [1.0, 0.0] if purpose == "query" or text == "relevant" else [0.0, 1.0]
                for text in texts
            ],
            dtype=np.float32,
        )

    encoder.encode_text = encode
    service = SimpleNamespace(
        lock=threading.RLock(),
        spec=rerank_spec(),
        encoder=encoder,
        check_memory=lambda encoder: None,
        get_encoder=lambda: encoder,
        mark_used=lambda: None,
    )
    reranker = SearchReranker(db, service)
    hits = [dict(chat_id=chat, message_id=i) for i in (1, 2)]
    # Disabled and images-only branches do no inference.
    assert reranker.apply("query", hits, "text", Filters())[0] == hits
    assert not calls
    db.settings = replace(db.settings, giga_rerank_enabled=True)
    assert reranker.apply("query", hits, "images", Filters())[1]["reason"] == "images_only"
    ordered, status = reranker.apply("query", hits, "text", Filters())
    assert status["applied"] and [h["message_id"] for h in ordered] == [2, 1]
    old_calls = len(calls)
    assert reranker.apply("query", hits, "text", Filters())[0] == ordered
    assert len(calls) == old_calls  # candidate and query cache both reused
    # Full-source filtering is a hard constraint, not a rerank score.
    with pytest.raises(UserError):
        reranker.apply("query", hits, "text", Filters(author_ids=["excluded"]))
    service.get_encoder = lambda: (_ for _ in ()).throw(UserError("not ready"))
    base, status = reranker.apply("another query", hits, "text", Filters())
    assert base == hits and not status["applied"] and status["reason"] == "not ready"


def test_rerank_cache_does_not_evict_vectors_needed_by_current_batch(db, np):
    from telegram_search.search.rerank import SearchReranker

    service = SimpleNamespace(spec=rerank_spec())
    reranker = SearchReranker(db, service)
    # Force eviction with unusually large synthetic vectors.
    encoder = SimpleNamespace(
        encode_text=lambda texts, purpose, **kwargs: np.ones(
            (len(texts), 500_000), dtype=np.float32
        )
    )
    reranker._vectors(encoder, ["one"], "passage", "revision")
    values = reranker._vectors(encoder, ["one", *map(str, range(20))], "passage", "revision")
    assert values.shape == (21, 500_000)
    assert reranker.cache_bytes <= 16 * 1024**2


def tiny_tokenizer(path):
    pytest.importorskip("tokenizers")
    from tokenizers import Tokenizer, models, pre_tokenizers, processors

    tok = Tokenizer(
        models.WordLevel({"[PAD]": 0, "[UNK]": 1, "[CLS]": 2, "[SEP]": 3}, unk_token="[UNK]")
    )
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 2), ("[SEP]", 3)]
    )
    tok.save(str(path / "tokenizer.json"))
    (path / "tokenizer_config.json").write_text(json.dumps({"pad_token": "[PAD]"}))


def test_giga_truncation_is_bounded_and_does_not_change_count_or_other_batches(tmp_path, np):
    from telegram_search.inference.giga import GigaEncoder

    tiny_tokenizer(tmp_path)
    encoder = GigaEncoder(rerank_spec(), tmp_path)
    text = "token " * 600
    before = encoder.tokenizer.count(text)
    prepared = encoder.prepare_text([text], "passage")[0]
    assert prepared["input_ids"].shape == (1, 512)
    assert prepared["input_ids"][0, -1] == 3  # preserve the terminal special token
    assert encoder.tokenizer.count(text) == before > 512
    with pytest.raises(UserError):
        encoder.tokenizer.batch([text], 512)
    fixed = encoder.tokenizer.batch(["token"], 64, fixed_length=64)
    assert fixed["input_ids"].shape == (1, 64)
    assert np.all(fixed["input_ids"][0, 3:] == 0)


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("visual", [False, True])
def test_native_gpu_precision_failure_only_falls_back_in_auto(
    tmp_path, monkeypatch, np, automatic, visual
):
    from telegram_search.inference.e5 import E5Encoder
    from telegram_search.inference.providers import CPU, CUDA
    from telegram_search.inference.siglip import SiglipEncoder

    tiny_tokenizer(tmp_path)
    settings = []

    class Session:
        def __init__(self, gpu):
            self.gpu = gpu

        def get_inputs(self):
            names = ["input_ids"] if visual else ["input_ids", "attention_mask", "token_type_ids"]
            return [SimpleNamespace(name=n, type="tensor(int64)") for n in names]

        def get_outputs(self):
            return [
                SimpleNamespace(
                    name="pooler_output" if visual else "last_hidden_state",
                    type="tensor(float)" if visual else "tensor(float16)",
                )
            ]

        def run(self, outputs, feed):
            if self.gpu:
                raise RuntimeError("native kernel secret diagnostics")
            batch, sequence = feed["input_ids"].shape
            return [
                np.ones(
                    (batch, 768) if visual else (batch, sequence, 768),
                    dtype=np.float32 if visual else np.float16,
                )
            ]

        def get_providers(self):
            return [CUDA if self.gpu else CPU]

    class Execution:
        def __init__(self, device, **kwargs):
            self.device, self.provider = device, CPU if device == "cpu" else CUDA
            self.warning = None

        def session(self, path, **kwargs):
            settings.append(kwargs)
            return Session(self.provider != CPU)

    if visual:
        from telegram_search.inference import siglip
        from telegram_search.inference.bundles import BundleStore

        monkeypatch.setattr(siglip, "Execution", Execution)
        monkeypatch.setattr(siglip, "runtime_version", lambda: "1.23.2")
        monkeypatch.setattr(BundleStore, "verify", lambda self, spec: tmp_path)
        encoder = SiglipEncoder(
            tmp_path,
            device="auto" if automatic else "gpu",
            search_device="auto" if automatic else "gpu",
        )

        def action():
            return encoder._session("text")

        execution = encoder.query_execution
    else:
        from telegram_search.inference import e5

        monkeypatch.setattr(e5, "Execution", Execution)
        encoder = E5Encoder(
            model_spec("berta"),
            tmp_path,
            device="auto" if automatic else "gpu",
            search_device="cpu",
        )
        action = encoder._load
        execution = encoder.execution
    if automatic:
        assert action().get_providers() == [CPU]
        assert execution.provider == CPU and execution.warning
    else:
        with pytest.raises(UserError, match="точность") as error:
            action()
        assert "secret" not in str(error.value)
        assert execution.provider == CUDA
    if visual:
        assert all(
            s["disabled_optimizers"] == ["SimplifiedLayerNormFusion", "LayerNormFusion"]
            for s in settings
        )


def test_visual_default_uses_durable_profile_when_old_bundle_is_missing(db, importer, monkeypatch):
    from telegram_search.indexing.media import MediaService
    from telegram_search.indexing.service import SemanticService

    semantic = SemanticService(db, importer.lifecycle_lock, start_background=False)
    media = MediaService(db, importer.lifecycle_lock, semantic, start_background=False)
    try:
        with db.connect() as conn:
            conn.execute("UPDATE media_state SET images_enabled=1,visual_profile='clip'")
        requested = []
        monkeypatch.setattr(
            media, "_prepare", lambda kind, offline, profile, reindex: requested.append(profile)
        )
        media.prepare("images", offline=True)
        media.preparation.join()
        assert requested == ["clip"]
    finally:
        media.shutdown()
        semantic.shutdown()
