import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import load
from test_media import fake_ocr, photo_export, services

from telegram_search.config.settings import Settings
from telegram_search.indexing.chats import ChatIndexing
from telegram_search.inference.providers import CPU, CUDA
from telegram_search.shared.errors import UserError
from telegram_search.sources.service import WorkspaceService
from telegram_search.storage.database import execute_sql


class ExecutionStub:
    def __init__(self, device, **options):
        self.device = device
        self.provider = CPU if device == "cpu" else CUDA
        self.warning = None
        self.probe = options.get("probe", True)

    def info(self):
        return {"device": self.device, "provider": self.provider, "warning": self.warning}


def test_legacy_devices_are_inherited_until_overridden_and_validate_types():
    old = Settings(device="gpu", search_device="cpu")
    assert old.model_device("e5") == old.model_device("clip") == "gpu"
    new = replace(old, e5_device="cpu", clip_search_device="gpu")
    assert new.model_device("clip") == "gpu" and new.model_device("e5") == "cpu"
    assert new.model_device("e5", query=True) == "cpu"
    assert new.model_device("clip", query=True) == "gpu"
    for values in (
        {"e5_device": []},
        {"ocr_engine": {}},
        {"ocr_device": "cuda"},
        {"ocr_device": "gpu"},
        {"clip_search_device": True},
    ):
        with pytest.raises(UserError):
            replace(old, **values).validate()


def test_model_device_change_preserves_other_models_and_rejects_failed_probe(
    db,
    importer,
    monkeypatch,
):
    semantic, media = services(db, importer)
    unloaded = []
    media.clip = SimpleNamespace(unload=lambda: unloaded.append("clip"))
    workspace = WorkspaceService(db, importer, semantic, media)
    monkeypatch.setattr("telegram_search.inference.providers.Execution", ExecutionStub)
    result = workspace.change_device("gpu", model="e5")
    assert result["model"]["device"] == "gpu"
    assert db.settings.model_device("clip") == "cpu" and not unloaded
    media.clip = None
    workspace.change_device("cpu", search_device="gpu", model="clip")
    assert db.settings.model_device("e5") == "gpu"
    assert db.settings.model_device("clip", query=True) == "gpu"
    before = (db.workspace / "config.json").read_bytes()

    def unavailable(*args, **kwargs):
        raise UserError("synthetic GPU unavailable")

    monkeypatch.setattr("telegram_search.inference.providers.Execution", unavailable)
    with pytest.raises(UserError):
        workspace.change_device("gpu", model="ocr")
    assert before == (db.workspace / "config.json").read_bytes()


def test_new_ocr_model_selection_keeps_old_cache_and_requires_preparation(
    db,
    importer,
    tmp_path,
    monkeypatch,
):
    load(importer, photo_export(tmp_path / "source"))
    semantic, media = services(db, importer)
    fake_ocr(media, "старый результат")
    media.ocr.unload = lambda: None
    assert media._ocr_one()
    with db.connect() as conn:
        conn.execute("UPDATE media_state SET ocr_enabled=1,ocr_paused=1")
        before = [tuple(row) for row in conn.execute("SELECT * FROM ocr_cache")]
    monkeypatch.setattr("telegram_search.inference.providers.Execution", ExecutionStub)

    def not_prepared():
        raise UserError("synthetic missing pinned model")

    monkeypatch.setattr(media, "_new_ocr", lambda settings: SimpleNamespace(verify=not_prepared))
    result = WorkspaceService(db, importer, semantic, media).change_device("gpu", model="ocr")
    assert result["model"]["engine"] == "paddle"
    assert db.settings.ocr_device == "gpu" and media.ocr is None
    with db.connect() as conn:
        assert before == [tuple(row) for row in conn.execute("SELECT * FROM ocr_cache")]
        assert tuple(conn.execute("SELECT ocr_enabled,ocr_paused FROM media_state").fetchone()) == (
            0,
            1,
        )


def test_ocr_cache_identity_is_shared_by_cpu_gpu_and_auto(tmp_path, monkeypatch):
    from telegram_search.inference.ocr_onnx import OnnxOcrEngine

    monkeypatch.setattr("telegram_search.inference.ocr_onnx.Execution", ExecutionStub)
    monkeypatch.setattr(
        "telegram_search.inference.ocr_onnx.importlib.util.find_spec", lambda name: True
    )
    engines = [
        OnnxOcrEngine(tmp_path, Settings(ocr_engine="paddle", ocr_device=device))
        for device in ("cpu", "gpu", "auto", "hybrid")
    ]
    assert len({engine.version for engine in engines}) == 1
    assert all(not engine.execution.probe and engine.worker.process is None for engine in engines)
    hybrid = engines[-1]
    assert hybrid.cpu_peer.version == hybrid.version
    assert hybrid.execution.device == "gpu" and hybrid.cpu_peer.execution.device == "cpu"
    assert hybrid.threads + hybrid.cpu_peer.threads == Settings().cpu_threads
    assert hybrid.backend_info()["device"] == "cpu+gpu"
    resized = OnnxOcrEngine(tmp_path, Settings(ocr_engine="paddle", ocr_max_edge=1200))
    assert resized.version != engines[0].version
    gpu = engines[1]
    gpu.worker = SimpleNamespace(
        recognize=lambda data: json.dumps({"text": "text", "confidence": 90, "provider": CPU}),
        unload=lambda: None,
    )
    with pytest.raises(UserError):
        gpu.recognize(b"synthetic")  # Explicit GPU must never silently report CPU recognition.


def test_failed_old_ocr_cleanup_cannot_publish_partial_settings(db, importer, monkeypatch):
    semantic, media = services(db, importer)

    def fail_cleanup():
        raise RuntimeError("synthetic cleanup failure")

    old = SimpleNamespace(unload=fail_cleanup)
    media.ocr = old
    with db.connect() as conn:
        conn.execute("UPDATE media_state SET ocr_enabled=1,ocr_paused=1")
    db.settings.save(db.workspace)
    before = (db.workspace / "config.json").read_bytes()
    monkeypatch.setattr("telegram_search.inference.providers.Execution", ExecutionStub)
    monkeypatch.setattr(
        media,
        "_new_ocr",
        lambda settings: SimpleNamespace(verify=lambda: None, unload=lambda: None),
    )
    with pytest.raises(RuntimeError, match="cleanup failure"):
        WorkspaceService(db, importer, semantic, media).change_device("gpu", model="ocr")
    assert media.ocr is old and db.settings.ocr_engine == "tesseract"
    assert (db.workspace / "config.json").read_bytes() == before
    with db.connect() as conn:
        assert tuple(conn.execute("SELECT ocr_enabled,ocr_paused FROM media_state").fetchone()) == (
            1,
            1,
        )


def test_legacy_gpu_resources_refresh_prepared_ocr_without_changing_cache(
    db, importer, monkeypatch
):
    semantic, media = services(db, importer)
    db.settings = replace(db.settings, ocr_engine="paddle", ocr_device="gpu")
    previous = SimpleNamespace(version="stable-ocr-cache", unload=lambda: None)
    media.ocr = previous
    monkeypatch.setattr("telegram_search.inference.providers.Execution", ExecutionStub)
    candidates = []

    def new_ocr(settings):
        candidates.append(settings)
        return SimpleNamespace(
            version=previous.version,
            verify=lambda: None,
            unload=lambda: None,
            check_contract=lambda: None,
        )

    monkeypatch.setattr(media, "_new_ocr", new_ocr)
    with db.connect() as conn:
        conn.execute("UPDATE media_state SET ocr_enabled=1,ocr_paused=1")
    WorkspaceService(db, importer, semantic, media).change_device(
        "cpu", gpu_device_id=1, gpu_memory_limit_mib=8192
    )
    assert media.ocr is not previous and media.ocr.version == previous.version
    assert len(candidates) == 1
    assert candidates[0].gpu_device_id == 1 and candidates[0].gpu_memory_limit_mib == 8192
    assert candidates[0].ocr_device == "gpu"
    with db.connect() as conn:
        assert conn.execute("SELECT ocr_paused FROM media_state").fetchone()[0] == 1


def test_ocr_commit_failure_cannot_publish_new_e5_clip_or_config(db, importer, monkeypatch):
    semantic, media = services(db, importer)

    def encoder():
        return SimpleNamespace(
            space_id="synthetic-stable",
            space_manifest={"test": "stable"},
            spec=SimpleNamespace(profile="small", dimension=4),
            adopt_space=lambda value: None,
            encode_text=lambda *args, **kwargs: None,
            unload_index=lambda: None,
            suspend=lambda: None,
            resume=lambda: None,
            unload=lambda: None,
        )

    old_encoder, new_encoder = encoder(), encoder()
    semantic.activate(old_encoder)
    old_clip = SimpleNamespace(space_id="clip-stable", unload=lambda: None)
    new_clip = SimpleNamespace(
        space_id="clip-stable", unload=lambda: None, check_contract=lambda: None
    )
    old_ocr = SimpleNamespace(unload=lambda: None)
    media.clip, media.ocr = old_clip, old_ocr
    db.settings = replace(db.settings, ocr_engine="paddle", ocr_device="gpu")
    db.settings.save(db.workspace)
    before = (db.workspace / "config.json").read_bytes()
    monkeypatch.setattr("telegram_search.inference.providers.Execution", ExecutionStub)
    monkeypatch.setattr(semantic, "_new_encoder", lambda *args: new_encoder)
    monkeypatch.setattr(media, "_new_clip", lambda settings: new_clip)
    monkeypatch.setattr(
        media,
        "_new_ocr",
        lambda settings: SimpleNamespace(
            verify=lambda: None,
            unload=lambda: None,
            check_contract=lambda: None,
        ),
    )
    with db.connect() as conn:
        state = tuple(conn.execute("SELECT * FROM semantic_state").fetchone())
        conn.execute(
            "CREATE TRIGGER fail_ocr BEFORE UPDATE OF ocr_enabled ON media_state "
            "BEGIN SELECT RAISE(ABORT,'synthetic OCR commit failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="OCR commit failure"):
        WorkspaceService(db, importer, semantic, media).change_device(
            "cpu", gpu_device_id=1, gpu_memory_limit_mib=8192
        )
    assert semantic.encoder is old_encoder and media.clip is old_clip and media.ocr is old_ocr
    assert db.settings.gpu_device_id == 0
    assert (db.workspace / "config.json").read_bytes() == before
    with db.connect() as conn:
        assert tuple(conn.execute("SELECT * FROM semantic_state").fetchone()) == state


def test_ocr_pause_is_independent_and_resume_keeps_other_chats_paused(
    db,
    importer,
    tmp_path,
):
    first = load(importer, photo_export(tmp_path / "first"))["chat_id"]
    second = load(importer, photo_export(tmp_path / "second", chat_id=200))["chat_id"]
    semantic, media = services(db, importer)
    fake_ocr(media)
    indexing = ChatIndexing(db, semantic, media, importer.lifecycle_lock)
    media.control("pause")  # Legacy global pause covers both queues.
    indexing.control(first, "images", "resume")
    status = indexing.status(first)["media"]
    assert not status["paused"] and status["ocr_paused"]
    assert not media._ocr_one()
    indexing.control(first, "ocr", "resume")
    assert not indexing.status(first)["media"]["ocr_paused"]
    assert indexing.status(second)["media"]["ocr_paused"]
    assert indexing.status(second)["media"]["paused"]
    indexing.control(first, "ocr", "pause")
    with db.connect() as conn:
        sha = conn.execute("SELECT sha256 FROM media_refs LIMIT 1").fetchone()[0]
        assert media._current(conn, sha) and not media._current(conn, sha, kind="ocr")
    assert not media._ocr_one()
    indexing.control(first, "ocr", "resume")
    indexing.control(first, "images", "pause")
    assert media._ocr_one() and indexing.status(first)["media"]["ocr_ready"] == 1
    assert indexing.status(first)["media"]["paused"]


def test_migration_preserves_legacy_pause_and_completed_recognition(db, importer, tmp_path):
    chat = load(importer, photo_export(tmp_path / "source"))["chat_id"]
    _, media = services(db, importer)
    fake_ocr(media)
    assert media._ocr_one()
    with db.connect() as conn:
        conn.execute("UPDATE chats SET media_paused=1")
        conn.execute("UPDATE media_state SET paused=1")
        before = [tuple(row) for row in conn.execute("SELECT * FROM ocr_cache")]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'ocr_work_%'"
        ).fetchall():
            conn.execute(f'DROP TRIGGER "{row[0]}"')
        conn.execute("DROP TABLE ocr_work")
        conn.execute("DROP TABLE ocr_queue_version")
        conn.execute("ALTER TABLE chats DROP COLUMN ocr_dense_paused")
        conn.execute("ALTER TABLE media_state DROP COLUMN ocr_dense_paused")
        conn.execute("ALTER TABLE chats DROP COLUMN ocr_batch")
        conn.execute("ALTER TABLE chats DROP COLUMN ocr_region_batch")
        conn.execute("ALTER TABLE chats DROP COLUMN ocr_paused")
        conn.execute("ALTER TABLE media_state DROP COLUMN ocr_paused")
        # Reconstruct the pre-v6 schema, including removal of later optional
        # source/search migrations before replaying all migrations.
        for trigger in (
            "chats_search_ai",
            "chats_search_ad",
            "chats_search_au",
            "index_excluded_authors_ai",
            "index_excluded_authors_ad",
            "messages_ai",
            "messages_ad",
            "messages_au",
        ):
            conn.execute(f'DROP TRIGGER "{trigger}"')
        conn.execute("DROP TABLE message_fts")
        conn.execute("DROP VIEW indexable_media_refs")
        conn.execute("DROP VIEW indexable_messages")
        conn.execute("DROP TABLE index_excluded_authors")
        conn.execute("DROP TABLE search_archive_epoch")
        schema = (Path(__file__).parents[1] / "src/telegram_search/storage/schema.sql").read_text()
        execute_sql(
            conn,
            schema[
                schema.index("CREATE VIRTUAL TABLE message_fts") : schema.index(
                    "CREATE TABLE media_blobs"
                )
            ],
        )
        conn.execute("INSERT INTO message_fts(message_fts) VALUES('rebuild')")
        for trigger in ("ocr_grams_ai", "ocr_grams_ad", "ocr_grams_au"):
            conn.execute(f'DROP TRIGGER "{trigger}"')
        for table in (
            "ocr_gram_fts",
            "telegram_jobs",
            "sync_runs",
            "dialog_sync_cursors",
            "telegram_message_provenance",
            "telegram_tombstones",
            "telegram_sync_conflicts",
            "telegram_assets",
            "dialog_sync_bindings",
            "telegram_connections",
        ):
            conn.execute(f'DROP TABLE "{table}"')
        conn.execute("DROP INDEX media_refs_path")
        conn.execute("ALTER TABLE index_work DROP COLUMN available_at")
        conn.execute("ALTER TABLE messages DROP COLUMN remote_deleted")
        conn.execute("ALTER TABLE source_roots DROP COLUMN managed")
        conn.execute("DELETE FROM schema_migrations WHERE version>=6")
    importer.shutdown()
    db.initialize()
    with db.connect() as conn:
        assert conn.execute("SELECT ocr_paused FROM chats WHERE id=?", (chat,)).fetchone()[0] == 1
        assert conn.execute("SELECT ocr_paused FROM media_state").fetchone()[0] == 1
        assert before == [tuple(row) for row in conn.execute("SELECT * FROM ocr_cache")]
