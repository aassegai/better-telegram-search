import hashlib
import importlib.metadata
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import export, load, message
from test_noise_analysis import start_ready

from telegram_search.indexing.noise_analysis import NoiseAnalysisService
from telegram_search.indexing.noise_dry_run import NoiseDryRun, TokenContract, local_token_contract
from telegram_search.indexing.noise_policy import NoiseDraftConflict, NoisePolicyService
from telegram_search.indexing.noise_source import NoiseSource
from telegram_search.search.chunking.noise.contracts import DryRunOptions
from telegram_search.search.chunking.noise.dry_run import ChunkMetrics, OwnedMessages
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize


def make_tokenizer(tmp_path):
    tokenizers = pytest.importorskip("tokenizers")
    from tokenizers import models, pre_tokenizers, processors

    from telegram_search.inference.tokenization import ModelTokenizer

    bundle = tmp_path / "synthetic-tokenizer"
    bundle.mkdir()
    vocab = {
        word: i
        for i, word in enumerate(
            [
                "[UNK]",
                "[PAD]",
                "[CLS]",
                "[SEP]",
                "passage",
                ":",
                "Содержательная",
                "фраза",
                "сука",
                "бял",
                "да",
                "?",
                "текст",
                "длинный",
                "Тестовый",
                "автор",
                "Ответ",
                "на",
                "сообщение",
                "(",
                ")",
                "Соседний",
                "контекст",
            ]
        )
    }
    tokenizer = tokenizers.Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]",
        special_tokens=[("[CLS]", vocab["[CLS]"]), ("[SEP]", vocab["[SEP]"])],
    )
    tokenizer.save(str(bundle / "tokenizer.json"))
    (bundle / "tokenizer_config.json").write_text('{"pad_token":"[PAD]"}')
    return bundle, ModelTokenizer(bundle)


@pytest.fixture
def tokenizer_bundle(tmp_path):
    return make_tokenizer(tmp_path)


@pytest.fixture
def services(db, importer):
    analyses = NoiseAnalysisService(db)
    try:
        policies = NoisePolicyService(db, analyses, importer.lifecycle_lock)
        yield analyses, policies
    finally:
        analyses.close()


def prepare(db, importer, tmp_path, services, tokenizer_bundle, messages=None, *, legacy=False):
    chat = load(
        importer,
        export(
            tmp_path / "synthetic",
            messages
            or [
                message(1, "Содержательная фраза"),
                message(2, "бял"),
                message(3, "сука"),
            ],
        ),
    )["chat_id"]
    with db.connect() as conn:
        conn.execute("INSERT INTO embedding_spaces VALUES('synthetic','small','{}',4,0)")
        conn.execute("UPDATE semantic_state SET active_space_id='synthetic',enabled=1")
        conn.execute("UPDATE chats SET text_batch=3 WHERE id=?", (chat,))
        if legacy:
            conn.execute(
                "UPDATE chat_chunking SET policy_id='telegram-windows-v1' WHERE chat_id=?", (chat,)
            )
            conn.execute(
                "UPDATE index_segments SET chunking_policy_id='telegram-windows-v1' "
                "WHERE chat_id=?",
                (chat,),
            )
            conn.execute(
                "UPDATE index_work SET chunking_policy_id='telegram-windows-v1' WHERE chat_id=?",
                (chat,),
            )
    analyses, policies = services
    state = start_ready(analyses, chat)
    assert state["state"] == "ready", state["summary"]["limits"]
    job = state["id"]
    policies.save_overrides(chat, job, 0, {"бял": "omit"})
    return (
        chat,
        job,
        TokenContract(
            tokenizer_bundle[1], "synthetic", "synthetic/test", "passage: ", 480, 3, "synthetic"
        ),
    )


def snapshot(db):
    with db.connect() as conn:
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not row[0].startswith("sqlite_")
        ]
        return {
            table: [tuple(row) for row in conn.execute(f'SELECT * FROM "{table}"')]
            for table in tables
        }


def test_batch_padding_respects_day_boundary_and_distinct_owned_ids(tokenizer_bundle):
    from telegram_search.search.chunking.legacy import TextPart

    tokenizer = tokenizer_bundle[1]
    texts = ["текст", "длинный текст " * 3, "текст", "текст"]
    metrics = ChunkMetrics(3)
    with_owned = OwnedMessages()
    try:
        for i, text in enumerate(texts):
            chunk = SimpleNamespace(
                day="2025-01-01" if i < 2 else "2025-01-02",
                tokens=tokenizer.count("passage: " + text),
                parts=(),
            )
            metrics.add(chunk, frozenset(), lambda _: None)
        actual = metrics.result()
        arrays = [
            tokenizer.batch(["passage: " + text for text in texts[start : start + 2]])
            for start in (0, 2)
        ]
        assert actual["padded_token_slots"] == sum(array["input_ids"].size for array in arrays)
        assert actual["tokens"] == sum(int(array["attention_mask"].sum()) for array in arrays)
        assert actual["batches"] == 2 and actual["padding_tokens"] > 0
        with_owned.add(
            0, [TextPart(1, 0, 3), TextPart(1, 3, 6), TextPart(9, 0, 1, "borrowed_context")]
        )
        with_owned.add(1, [TextPart(2, 0, 1)])
        assert with_owned.count(0) == 1
        assert with_owned.differences() == {"removed_own_messages": 1, "restored_own_messages": 1}
    finally:
        with_owned.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_comparison_matches_actual_worker_staging_without_mutating_archive(
    db,
    importer,
    tmp_path,
    services,
    tokenizer_bundle,
    legacy,
):
    from telegram_search.indexing.worker import SegmentWorker

    midnight = 1750032000  # UTC day boundary
    messages = [
        message(1, "Содержательная фраза?", date=midnight - 10),
        message(2, "угу", date=midnight + 1, reply_to_message_id=1),
        message(3, "бял", date=midnight + 2),
        message(4, "сука", date=midnight + 3),
        message(5, "сука", date=midnight + 4, file="missing.txt", media_type="file"),
        message(6, "длинный-текст " * 200, date=midnight + 5),
        *[message(i, "Содержательная фраза", date=midnight + i) for i in range(7, 24)],
    ]
    chat, job, contract = prepare(
        db, importer, tmp_path, services, tokenizer_bundle, messages, legacy=legacy
    )
    # Deleted canonical rows still have the worker's historical segment boundary
    # semantics. Service rows do not become model parts.
    with db.connect() as conn:
        conn.execute("UPDATE messages SET remote_deleted=1 WHERE message_id=8")
        conn.execute("UPDATE messages SET kind='service' WHERE message_id=9")
        conn.execute("UPDATE chats SET revision=revision+1 WHERE id=?", (chat,))
    # Start a fresh analysis after changing the synthetic source.
    analyses, policies = services
    job = start_ready(analyses, chat)["id"]
    policies.save_overrides(chat, job, 0, {"бял": "omit"})
    result_before = snapshot(db)
    result = NoiseDryRun(policies).compare(chat, job, 1, contract=contract)
    assert result["complete"] and result["comparison_complete"]
    assert result_before == snapshot(db)
    assert result["before"]["source_messages"] == len(messages)
    assert result["after"]["classified_omitted_messages"] >= 2
    assert result["after"]["protected_noise_messages"] == 1

    def actual_worker_metrics():
        encoder = SimpleNamespace(
            tokenizer=contract.tokenizer,
            spec=SimpleNamespace(manifest={"passage_prefix": contract.prefix}),
            space_id=contract.space_id,
        )
        worker = SegmentWorker(db, encoder, None, threading.RLock())
        with db.connect() as conn:
            conn.execute(
                "UPDATE index_work SET state='running',embedding_space_id='synthetic' "
                "WHERE state='pending'"
            )
            works = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM index_work WHERE state='running' ORDER BY utc_day"
                )
            ]
        for work in works:
            worker._build(work)
        with db.connect() as conn:
            chunks = [
                dict(row)
                for row in conn.execute(
                    "SELECT c.* FROM chunks c JOIN index_work w ON "
                    "w.chat_id=c.chat_id AND w.utc_day=c.utc_day AND w.generation=c.generation "
                    "WHERE w.state='running' ORDER BY c.utc_day,c.ordinal",
                )
            ]
            own = conn.execute(
                "SELECT count(DISTINCT p.message_id) FROM chunk_embedding_parts p "
                "JOIN chunks c ON c.id=p.chunk_id JOIN index_work w ON "
                "w.chat_id=c.chat_id AND w.utc_day=c.utc_day AND w.generation=c.generation "
                "WHERE w.state='running' AND p.role<>'borrowed_context'",
            ).fetchone()[0]
        token_slots = batches = 0
        for day in sorted({c["utc_day"] for c in chunks}):
            texts = [contract.prefix + c["text"] for c in chunks if c["utc_day"] == day]
            for start in range(0, len(texts), contract.batch_size):
                batch = contract.tokenizer.batch(
                    texts[start : start + contract.batch_size], limit=480
                )
                token_slots += batch["input_ids"].size
                batches += 1
        return {
            "chunks": len(chunks),
            "tokens": sum(c["tokens"] for c in chunks),
            "padded_token_slots": token_slots,
            "batches": batches,
            "own_messages": own,
        }

    actual = actual_worker_metrics()
    assert {key: result["before"][key] for key in actual} == actual
    policies.apply(chat, job, 1)
    # Old running work is superseded and the new generation is staged separately.
    actual = actual_worker_metrics()
    assert {key: result["after"][key] for key in actual} == actual
    assert result["delta"]["removed_own_messages"] >= 1


def test_message_limit_compares_the_same_prefix_but_reports_partial_coverage(
    db, importer, tmp_path, services, tokenizer_bundle
):
    chat, job, contract = prepare(db, importer, tmp_path, services, tokenizer_bundle)
    result = NoiseDryRun(services[1]).compare(
        chat, job, 1, contract=contract, options=DryRunOptions(max_messages=2)
    )
    assert result["comparison_complete"] and not result["complete"] and result["state"] == "limited"
    assert result["limits"] == ["message_limit"] and result["delta"] is not None
    assert result["before"]["source_messages"] == result["after"]["source_messages"] == 2
    assert result["source_verified"] and result["options"]["max_messages"] == 2
    assert result["progress"]["completed_passes"] == 2


def test_new_form_structural_protection_is_counted_by_the_actual_policy(
    db, importer, tmp_path, services, tokenizer_bundle
):
    chat, job, contract = prepare(
        db,
        importer,
        tmp_path,
        services,
        tokenizer_bundle,
        [message(1, "Бял"), message(2, "бял")],
    )
    result = NoiseDryRun(services[1]).compare(chat, job, 1, contract=contract)
    assert result["after"]["own_messages"] == 1
    assert result["after"]["protected_noise_messages"] == 1
    assert result["after"]["classified_omitted_messages"] == 1
    assert result["before"]["protected_noise_messages"] == 0


@pytest.mark.parametrize("change", ["draft", "source", "batch", "space"])
def test_concurrent_contract_changes_reject_comparison(
    db, importer, tmp_path, services, tokenizer_bundle, change
):
    chat, job, contract = prepare(db, importer, tmp_path, services, tokenizer_bundle)
    changed = False

    def progress(value):
        nonlocal changed
        if changed:
            return
        changed = True
        with db.connect() as conn:
            if change == "draft":
                conn.execute(
                    "UPDATE noise_analyses SET draft_revision=draft_revision+1 WHERE id=?", (job,)
                )
            elif change == "source":
                conn.execute("UPDATE chats SET revision=revision+1 WHERE id=?", (chat,))
            elif change == "batch":
                conn.execute("UPDATE chats SET text_batch=4 WHERE id=?", (chat,))
            else:
                conn.execute("UPDATE semantic_state SET active_space_id=NULL")

    with pytest.raises(NoiseDraftConflict):
        NoiseDryRun(services[1]).compare(chat, job, 1, contract=contract, progress=progress)


def test_cancellation_and_time_limit_during_tokenization_publish_no_false_delta(
    db, importer, tmp_path, services, tokenizer_bundle
):
    chat, job, contract = prepare(db, importer, tmp_path, services, tokenizer_bundle)
    cancelled = False

    class CancellingTokenizer:
        def count(self, text):
            nonlocal cancelled
            cancelled = True
            return contract.tokenizer.count(text)

    with pytest.raises(UserError, match="отменено"):
        NoiseDryRun(services[1]).compare(
            chat,
            job,
            1,
            contract=replace(contract, tokenizer=CancellingTokenizer()),
            cancelled=lambda: cancelled,
        )

    class LimitedTokenizer:
        def count(self, text):
            from telegram_search.indexing.noise_source import DryRunLimited

            raise DryRunLimited("time_limit")

    result = NoiseDryRun(services[1]).compare(
        chat, job, 1, contract=replace(contract, tokenizer=LimitedTokenizer())
    )
    assert result["limits"] == ["time_limit"] and result["delta"] is None
    assert not result["comparison_complete"] and not result["complete"]


@pytest.mark.parametrize("sql_interrupt", [False, True])
def test_final_deadline_or_sql_interrupt_never_publishes_a_verified_delta(
    db, importer, tmp_path, services, tokenizer_bundle, monkeypatch, sql_interrupt
):
    chat, job, contract = prepare(db, importer, tmp_path, services, tokenizer_bundle)
    clock = 0
    original = OwnedMessages.differences

    def expire(self):
        nonlocal clock
        result = original(self)
        clock = 301
        if sql_interrupt:
            raise sqlite3.OperationalError("interrupted")
        return result

    monkeypatch.setattr(
        "telegram_search.indexing.noise_dry_run.time",
        SimpleNamespace(monotonic=lambda: clock, thread_time=time.thread_time),
    )
    monkeypatch.setattr(OwnedMessages, "differences", expire)
    result = NoiseDryRun(services[1]).compare(chat, job, 1, contract=contract)
    assert result["limits"] == ["time_limit"]
    assert result["delta"] is None and not result["comparison_complete"]
    assert result["before"] is not None and result["after"] is not None
    assert not result["source_verified"]


def test_excluded_sender_never_leaks_through_parent_or_neighbor_and_cache_is_bounded(
    db, importer, tmp_path, services, tokenizer_bundle
):
    messages = [
        message(1, "секретный текст?", author="bot"),
        message(2, "угу", reply_to_message_id=1),
        message(3, "бял"),
    ]
    chat, _, contract = prepare(db, importer, tmp_path, services, tokenizer_bundle, messages)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO index_excluded_authors(chat_id,author_id) VALUES(?,?)", (chat, "bot")
        )
        conn.execute("UPDATE chats SET revision=revision+1 WHERE id=?", (chat,))
    source = NoiseSource(db, chat, lambda conn: None, lambda: None, lambda: False)
    rows = list(source.messages(100))
    assert [row.message_id for row in rows] == [2, 3]
    assert source.lookup(chat, 1) is None and source.lookup("other", 2) is None
    assert source.neighbor(rows[0], 3600) is None
    for i in range(1000):
        source._remember(
            1000 + i,
            replace(rows[0], text="я" * 16000, author_id="ю" * 16000, message_id=1000 + i),
        )
    assert len(source.cache) <= 512 and source.cache_bytes <= 4 * 1024**2
    actual_bytes = sum(
        sum(len(value.encode()) for value in vars(cached).values() if isinstance(value, str))
        for cached, _weight in source.cache.values()
        if cached is not None
    )
    assert actual_bytes <= source.cache_bytes
    job = start_ready(services[0], chat)["id"]
    services[1].save_overrides(chat, job, 0, {"бял": "omit"})
    result = NoiseDryRun(services[1]).compare(chat, job, 1, contract=contract)
    assert result["before"]["source_messages"] == result["after"]["source_messages"] == 2
    # With the excluded question unavailable, the isolated reply cannot create
    # a support-only vector once the other own form is omitted.
    assert result["after"]["chunks"] == 0


def test_tokenization_holds_no_main_connection_or_lifecycle_lock(
    db, importer, tmp_path, services, tokenizer_bundle, monkeypatch
):
    chat, job, contract = prepare(db, importer, tmp_path, services, tokenizer_bundle)
    original_connect, active = db.connect, 0

    @contextmanager
    def tracked():
        nonlocal active
        with original_connect() as conn:
            active += 1
            try:
                yield conn
            finally:
                active -= 1

    class CheckedTokenizer:
        def count(self, text):
            assert active == 0
            # A separate thread proves an RLock was not taken by the dry-run.
            acquired = []

            def enter():
                success = importer.lifecycle_lock.acquire(timeout=0.05)
                acquired.append(success)
                if success:
                    importer.lifecycle_lock.release()

            thread = threading.Thread(target=enter)
            thread.start()
            thread.join(timeout=1)
            assert acquired == [True]
            return contract.tokenizer.count(text)

    monkeypatch.setattr(db, "connect", tracked)
    result = NoiseDryRun(services[1]).compare(
        chat, job, 1, contract=replace(contract, tokenizer=CheckedTokenizer())
    )
    assert result["complete"]


def test_cli_dry_run_shares_comparison_and_emits_no_private_forms(
    db, importer, tmp_path, services, tokenizer_bundle, monkeypatch
):
    from test_noise_cli import arguments

    from telegram_search.indexing.noise_commands import noise_command

    chat, job, contract = prepare(db, importer, tmp_path, services, tokenizer_bundle)
    importer.shutdown()
    monkeypatch.setattr(
        "telegram_search.indexing.noise_dry_run.local_token_contract", lambda *_: contract
    )
    result = noise_command(db, arguments("dry-run", chat, job, "--revision", "1"))
    assert result["complete"] and result["delta"]["removed_own_messages"] == 1
    encoded = json.dumps(result, ensure_ascii=False)
    assert all(private not in encoded for private in ("бял", "сука", chat, str(tmp_path)))
    with pytest.raises(UserError, match="лимиты"):
        noise_command(db, arguments("dry-run", chat, job, "--revision", "1", "--max-messages", "0"))


def test_oversized_source_and_metadata_stop_without_truncation(
    db, importer, tmp_path, services, tokenizer_bundle
):
    chat, job, contract = prepare(db, importer, tmp_path, services, tokenizer_bundle)
    for column, payload in (
        ("text", "я" * 17000),
        ("raw_json", json.dumps({"extra": "a" * 70000})),
        ("author_id", "a" * 60000),
        ("kind", "a" * 60000),
    ):
        with db.connect() as conn:
            original = conn.execute(f"SELECT {column} FROM messages WHERE message_id=1").fetchone()[
                0
            ]
            conn.execute(f"UPDATE messages SET {column}=? WHERE message_id=1", (payload,))
        result = NoiseDryRun(services[1]).compare(chat, job, 1, contract=contract)
        assert result["limits"] == ["source_length_limit"] and result["delta"] is None
        assert not result["comparison_complete"]
        with db.connect() as conn:
            conn.execute(f"UPDATE messages SET {column}=? WHERE message_id=1", (original,))


def test_dry_run_needs_active_local_tokenizer_and_never_downloads(
    db, importer, tmp_path, services, tokenizer_bundle, monkeypatch
):
    from telegram_search.config.model_registry import ModelSpec
    from telegram_search.inference.bundles import BundleStore

    chat, job, contract = prepare(db, importer, tmp_path, services, tokenizer_bundle)
    monkeypatch.setattr(BundleStore, "prepare", lambda *a, **k: pytest.fail("download forbidden"))
    with pytest.raises(UserError, match="токенизатор"):
        NoiseDryRun(services[1]).compare(chat, job, 1)
    # Only tokenizer files exist: no ONNX weights and no inference session needed.
    bundle, _ = tokenizer_bundle
    manifest = {
        "model_id": "synthetic/test",
        "passage_prefix": contract.prefix,
        "chunk_max_tokens": 480,
        "files": [
            {
                "name": name,
                "bytes": (bundle / name).stat().st_size,
                "sha256": hashlib.sha256((bundle / name).read_bytes()).hexdigest(),
            }
            for name in ("tokenizer.json", "tokenizer_config.json")
        ],
    }
    spec = ModelSpec("small", manifest)
    space_manifest = {
        "model": manifest,
        "runtime": {"tokenizers_version": importlib.metadata.version("tokenizers")},
    }
    space = hashlib.sha256(serialize(space_manifest).encode()).hexdigest()
    monkeypatch.setattr("telegram_search.indexing.noise_dry_run.model_spec", lambda _: spec)
    target = BundleStore(db.workspace).path(spec)
    target.mkdir(parents=True)
    for name in ("tokenizer.json", "tokenizer_config.json"):
        (target / name).write_bytes((bundle / name).read_bytes())
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO embedding_spaces VALUES(?,?,?,?,0)",
            (space, "small", serialize(space_manifest), 4),
        )
        conn.execute("UPDATE semantic_state SET active_space_id=?", (space,))
    loaded = local_token_contract(db, chat)
    assert loaded.space_id == space and loaded.batch_size == 3
    assert loaded.tokenizer.count("passage: текст") == contract.tokenizer.count("passage: текст")
    incompatible = {**space_manifest, "runtime": {"tokenizers_version": "0.0-incompatible"}}
    incompatible_space = hashlib.sha256(serialize(incompatible).encode()).hexdigest()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO embedding_spaces VALUES(?,?,?,?,0)",
            (incompatible_space, "small", serialize(incompatible), 4),
        )
        conn.execute("UPDATE semantic_state SET active_space_id=?", (incompatible_space,))
    with pytest.raises(UserError, match="токенизатор"):
        local_token_contract(db, chat)
    with db.connect() as conn:
        conn.execute("UPDATE semantic_state SET active_space_id=?", (space,))
    (target / "tokenizer_config.json").write_text('{"pad_token":"[UNK]"}')
    with pytest.raises(UserError, match="повреждён"):
        local_token_contract(db, chat)


@pytest.mark.parametrize(
    "options",
    [
        {"max_messages": 0},
        {"max_messages": True},
        {"max_messages": 1000001},
        {"time_limit_seconds": 3601},
    ],
)
def test_invalid_dry_run_limits(options):
    with pytest.raises(ValueError):
        DryRunOptions(**options)
