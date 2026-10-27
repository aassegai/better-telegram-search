"""Public synthetic contracts; no downloaded models or working archive DBs."""

import json
import threading
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from conftest import export, load, message

from telegram_search.config.model_registry import rerank_spec
from telegram_search.config.settings import Settings
from telegram_search.inference.rerank_contract import validate_scores
from telegram_search.search.cache import SearchCache
from telegram_search.search.execution import SearchCancelled, SearchExecution, execution_context
from telegram_search.shared.errors import UserError


@pytest.fixture
def tokenizer(tmp_path):
    tk = pytest.importorskip("tokenizers")
    pytest.importorskip("numpy")
    from telegram_search.inference.tokenization import ModelTokenizer

    model = tk.Tokenizer(
        tk.models.WordLevel(
            {"<s>": 0, "<pad>": 1, "</s>": 2, "<unk>": 3, "query": 4, "document": 5},
            unk_token="<unk>",
        )
    )
    model.pre_tokenizer = tk.pre_tokenizers.Whitespace()
    model.post_processor = tk.processors.TemplateProcessing(
        single="<s> $A </s>",
        pair="<s> $A </s> </s> $B </s>",
        special_tokens=[("<s>", 0), ("</s>", 2)],
    )
    model.save(str(tmp_path / "tokenizer.json"))
    (tmp_path / "tokenizer_config.json").write_text(json.dumps({"pad_token": "<pad>"}))
    return ModelTokenizer(tmp_path)


def test_pairs_special_tokens_padding_bounded_query_and_documents(tokenizer):
    inputs, truncated, query_truncated = tokenizer.pairs(
        "query " * 20,
        ["document " * 20, "document"],
        limit=12,
        query_limit=4,
        query_characters=8192,
        document_characters=32768,
    )
    assert inputs["input_ids"].shape == (2, 12)
    assert inputs["input_ids"][0].tolist() == [0, 4, 4, 4, 4, 2, 2, 5, 5, 5, 5, 2]
    assert inputs["attention_mask"][1].tolist() == [1] * 9 + [0] * 3
    assert truncated == [True, False] and query_truncated
    assert tokenizer.count("query " * 20) == 22
    with pytest.raises(UserError):
        tokenizer.batch(["document " * 20], limit=12)
    short, flags, cut = tokenizer.pairs(
        "query",
        ["document"],
        limit=12,
        query_limit=4,
        query_characters=8192,
        document_characters=32768,
    )
    assert short["input_ids"].shape == (1, 6) and flags == [False] and not cut


def test_pair_character_caps_are_reported_without_cross_product_overflow(tokenizer):
    inputs, flags, cut = tokenizer.pairs(
        "query " * 10000,
        ["document " * 10000],
        limit=12,
        query_limit=4,
        query_characters=64,
        document_characters=64,
    )
    assert inputs["input_ids"].shape == (1, 12) and flags == [True] and cut
    assert tokenizer.tokenizer.truncation is None


def test_stable_sigmoid_and_output_shape():
    np = pytest.importorskip("numpy")
    from telegram_search.inference.jina import sigmoid_scores

    actual = sigmoid_scores(np.array([[-10000.0], [0], [10000.0]], dtype=np.float32), 3)
    assert actual.dtype == np.float32 and actual.tolist() == [0, 0.5, 1]
    for value in (
        np.zeros((2,)),
        np.zeros((2, 2)),
        np.array([[float("nan")]]),
        np.array([[float("inf")]]),
    ):
        with pytest.raises(UserError):
            sigmoid_scores(value, 1)


def test_reranker_score_ids_are_strict_and_reordered():
    good = [
        dict(candidate_id="b", score=0.9, truncated=False, query_truncated=False),
        dict(candidate_id="a", score=0.1, truncated=True, query_truncated=False),
    ]
    assert [r["candidate_id"] for r in validate_scores(["a", "b"], good)] == ["a", "b"]
    for bad in (
        good[:1],
        [good[0], good[0]],
        [dict(good[0], candidate_id="foreign"), good[1]],
        [dict(good[0], score=True), good[1]],
        [dict(good[0], score=float("nan")), good[1]],
        [dict(good[0], truncated=1), good[1]],
        [dict(good[0], extra="unexpected"), good[1]],
    ):
        with pytest.raises(UserError):
            validate_scores(["a", "b"], bad)


def test_old_settings_default_and_profile_validation(tmp_path):
    old = Settings(device="gpu", search_device="cpu", embedding_batch=32)
    values = asdict(old)
    values.pop("rerank_profile")
    (tmp_path / "config.json").write_text(json.dumps(values))
    assert Settings.load(tmp_path) == old
    for profile in (None, [], 1, "remote/repo"):
        with pytest.raises(UserError):
            replace(old, rerank_profile=profile).validate()


def test_profile_changes_search_revision_but_no_index_state(db, importer, tmp_path):
    load(importer, export(tmp_path / "source", [message()]))
    before = SearchCache.revision(db)
    with db.connect() as conn:
        segments = [tuple(r) for r in conn.execute("SELECT * FROM index_segments")]
        state = tuple(conn.execute("SELECT * FROM semantic_state").fetchone())
    db.settings = replace(db.settings, rerank_profile="jina-v2")
    assert SearchCache.revision(db) != before
    with db.connect() as conn:
        assert segments == [tuple(r) for r in conn.execute("SELECT * FROM index_segments")]
        assert state == tuple(conn.execute("SELECT * FROM semantic_state").fetchone())


def pair_search(db, importer, tmp_path):
    pytest.importorskip("numpy")
    from telegram_search.search.rerank import SearchReranker

    load(importer, export(tmp_path / "source", [message(1, "other"), message(2, "relevant")]))
    with db.connect() as conn:
        chat = conn.execute("SELECT id FROM chats").fetchone()[0]
    calls = []

    def score(query, candidates):
        calls.append((query, candidates))
        return [
            dict(
                candidate_id=c["candidate_id"],
                score=0.9 if c["text"] == "relevant" else 0.1,
                truncated=c["text"] == "other",
                query_truncated=False,
            )
            for c in reversed(candidates)
        ]

    encoder = SimpleNamespace(score_pairs=score, unload=lambda: None)
    service = SimpleNamespace(
        lock=threading.RLock(),
        spec=rerank_spec("jina-v2"),
        encoder=encoder,
        get_encoder=lambda: encoder,
        mark_used=lambda: None,
        check_memory=lambda e: None,
    )
    db.settings = replace(db.settings, giga_rerank_enabled=True, rerank_profile="jina-v2")
    return (
        SearchReranker(db, service),
        encoder,
        calls,
        [dict(chat_id=chat, message_id=i) for i in (1, 2)],
    )


def test_pairwise_rerank_order_query_bound_cache_and_scope(db, importer, tmp_path):
    from telegram_search.search.lexical import Filters

    search, encoder, calls, hits = pair_search(db, importer, tmp_path)
    ranked, metadata = search.apply("query", hits, "text", Filters())
    assert [h["message_id"] for h in ranked] == [2, 1]
    assert metadata["applied"] and metadata["truncated_candidates"] == 1
    assert metadata["model_id"] == rerank_spec("jina-v2").model_id
    assert search.apply("query", hits, "text", Filters())[0] == ranked and len(calls) == 1
    search.apply("different", hits, "text", Filters())
    assert len(calls) == 2
    with pytest.raises(UserError):
        search.apply("query", hits, "text", Filters(author_ids=["absent"]))
    assert len(calls) == 2
    encoder.score_pairs = lambda *args: []
    fallback, metadata = search.apply("new", hits, "text", Filters())
    assert fallback == hits and not metadata["applied"]


def test_cancellation_discards_pair_scores_before_cache(db, importer, tmp_path):
    from telegram_search.search.lexical import Filters

    search, encoder, calls, hits = pair_search(db, importer, tmp_path)
    execution = SearchExecution()
    original = encoder.score_pairs

    def score(*args):
        rows = original(*args)
        execution.cancelled.set()
        return rows

    encoder.score_pairs = score
    with execution_context(execution), pytest.raises(SearchCancelled):
        search.apply("query", hits, "text", Filters())
    assert not search.cache and len(calls) == 1


def test_profile_setting_is_separate_and_blocked_during_preparation(db):
    from telegram_search.sources.service import WorkspaceService

    rerank = SimpleNamespace(lock=threading.RLock(), preparation=None, encoder=None)
    service = WorkspaceService(
        db, SimpleNamespace(lifecycle_lock=threading.RLock()), None, None, rerank
    )
    assert service.update_settings({"rerank_profile": "jina-v2"})["rerank_profile"] == "jina-v2"
    rerank.preparation = SimpleNamespace(is_alive=lambda: True)
    with pytest.raises(UserError):
        service.update_settings({"rerank_profile": "giga"})
    assert db.settings.rerank_profile == "jina-v2"
    with pytest.raises(UserError):
        service.update_settings({"rerank_profile": "giga", "cpu_threads": 2})


def test_jina_manifest_pins_only_data_files_and_pairwise_contract():
    spec = rerank_spec("jina-v2")
    assert spec.manifest["adapter"] == "onnx-pairwise-v1"
    assert spec.manifest["license"] == "CC-BY-NC-4.0"
    assert spec.manifest["max_length"] == 1024 and spec.manifest["query_max_tokens"] == 512
    assert spec.dimension == 1 and len(spec.revision) == 40
    assert {f["name"] for f in spec.manifest["files"]} == {
        "onnx/model.onnx",
        "tokenizer.json",
        "tokenizer_config.json",
        "config.json",
        "special_tokens_map.json",
        "README.md",
    }
    assert all(len(f["sha256"]) == 64 and f["bytes"] > 0 for f in spec.manifest["files"])


@pytest.mark.parametrize("device", ["auto", "gpu"])
@pytest.mark.parametrize("failure", [UserError, RuntimeError])
def test_gpu_precision_failures_auto_fallback_but_forced_gpu_rejects(device, failure):
    pytest.importorskip("numpy")
    from telegram_search.inference.jina import JinaReranker
    from telegram_search.inference.providers import CPU, CUDA

    model = JinaReranker.__new__(JinaReranker)
    model.session = model.query_session = None
    model.execution = SimpleNamespace(device=device, provider=CUDA, warning=None)
    calls = []
    cpu = object()

    def checked():
        calls.append(model.execution.provider)
        if model.execution.provider == CUDA:
            raise failure("synthetic private native detail")
        return cpu

    model._checked_session = checked
    if device == "auto":
        assert model._load() is cpu and calls == [CUDA, CPU]
        assert model.execution.warning and model.execution.provider == CPU
    else:
        with pytest.raises(UserError):
            model._load()
        assert calls == [CUDA] and model.session is None


def test_failed_preparation_stays_visible_and_cannot_create_encoder(db, monkeypatch):
    from telegram_search.indexing.rerank import RerankService
    from telegram_search.inference.bundles import BundleStore

    db.settings = replace(db.settings, rerank_profile="jina-v2")
    service = RerankService(db)
    monkeypatch.setattr(
        BundleStore,
        "prepare",
        lambda *a, **kw: (_ for _ in ()).throw(UserError("Synthetic failure")),
    )
    try:
        service.prepare()
        service.preparation.join(timeout=5)
        assert not service.preparation.is_alive()
        status = service.status()
        assert status["preparation_state"] == "failed" and status["error"] == "Synthetic failure"
        assert not status["ready"] and status["manifest_id"] == service.spec.identity
        with pytest.raises(UserError):
            service.get_encoder()
    finally:
        service.shutdown()


def test_status_keeps_captured_encoder_and_model_on_interleaved_profile_save(db):
    from telegram_search.indexing.rerank import RerankService
    from telegram_search.sources.service import WorkspaceService

    service = RerankService(db)
    workspace = WorkspaceService(
        db, SimpleNamespace(lifecycle_lock=threading.RLock()), None, None, service
    )
    giga = rerank_spec()

    class InterleavedEncoder:
        @property
        def spec(self):
            workspace.update_settings({"rerank_profile": "jina-v2"})
            return giga

        def unload(self):
            pass

        def backend_info(self):
            return {"model_id": giga.model_id, "loaded": False}

    service.encoder = InterleavedEncoder()
    try:
        status = service.status()
        assert status["profile"] == "giga" and status["model_id"] == giga.model_id
        assert status["backend"]["model_id"] == giga.model_id
        assert giga.identity in status["path"]
        assert service.status()["profile"] == "jina-v2" and service.encoder is None
    finally:
        service.shutdown()


def test_history_records_jina_manifest_with_jina_identity(db):
    from telegram_search.search.history import SearchHistory

    db.settings = replace(db.settings, rerank_profile="jina-v2", giga_rerank_enabled=True)
    spec = rerank_spec("jina-v2")
    with db.connect() as conn:
        conn.execute(
            "UPDATE rerank_state SET manifest_id=?,preparation_state='ready' WHERE id=1",
            (spec.identity,),
        )
        models = SearchHistory._models(conn, db, {"rerank_applied": True})
    assert models["rerank"]["profile"] == "jina-v2"
    assert models["rerank"]["manifest"] == spec.manifest
    assert models["rerank"]["manifest_id"] == spec.identity and models["rerank"]["applied"]


@pytest.mark.parametrize(
    "field,other", [("rerank_profile", "jina-v2"), ("giga_rerank_enabled", True)]
)
def test_rerank_revision_prevents_settings_aba_and_is_not_client_writable(db, field, other):
    from telegram_search.sources.service import WorkspaceService

    service = WorkspaceService(db, SimpleNamespace(lifecycle_lock=threading.RLock()), None, None)
    original, fingerprint = getattr(db.settings, field), SearchCache.revision(db)
    service.update_settings({field: other})
    assert db.settings.rerank_revision == 1
    service.update_settings({field: original})
    assert db.settings.rerank_revision == 2 and SearchCache.revision(db) != fingerprint
    assert Settings.load(db.workspace).rerank_revision == 2
    service.update_settings({field: original})
    assert db.settings.rerank_revision == 2
    with pytest.raises(UserError):
        service.update_settings({"rerank_revision": 0})


@pytest.mark.parametrize("stage", ["before_inference", "during_inference"])
def test_search_rejects_profile_aba_in_both_inference_windows(db, importer, tmp_path, stage):
    from telegram_search.search.lexical import Filters
    from telegram_search.sources.service import WorkspaceService

    search, encoder, calls, hits = pair_search(db, importer, tmp_path)
    service = search.service
    service.preparation = None
    workspace = WorkspaceService(
        db, SimpleNamespace(lifecycle_lock=threading.RLock()), None, None, service
    )
    original = encoder.score_pairs

    def change_twice():
        workspace.update_settings({"rerank_profile": "giga"})
        workspace.update_settings({"rerank_profile": "jina-v2"})

    if stage == "before_inference":
        documents = search._documents

        def read(*args):
            result = documents(*args)
            change_twice()
            return result

        search._documents = read
    else:

        def score(*args):
            result = original(*args)
            change_twice()
            return result

        encoder.score_pairs = score
    with pytest.raises(UserError, match="Архив изменился"):
        search.apply("query", hits, "text", Filters())
    assert db.settings.rerank_profile == "jina-v2" and db.settings.rerank_revision == 2
    assert len(calls) == (0 if stage == "before_inference" else 1)


def test_history_uses_captured_reranker_instead_of_later_settings(db):
    from telegram_search.search.history import SearchHistory

    jina = rerank_spec("jina-v2")
    assert db.settings.rerank_profile == "giga"
    result = {
        "rerank_applied": True,
        "rerank_branches": [{"applied": True, "manifest_id": jina.identity, "profile": "jina-v2"}],
    }
    with db.connect() as conn:
        metadata = SearchHistory._models(conn, db, result)["rerank"]
        assert metadata["profile"] == "jina-v2" and metadata["manifest_id"] == jina.identity
        assert metadata["manifest"] == jina.manifest
        result["rerank_branches"].append({"applied": True, "manifest_id": rerank_spec().identity})
        with pytest.raises(UserError):
            SearchHistory._models(conn, db, result)
