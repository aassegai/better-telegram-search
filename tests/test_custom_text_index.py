"""Custom Python text embeddings through the real chunk/index/search lifecycle."""

import json

import pytest
from conftest import export, load, message
from test_custom_adapters import package

pytest.importorskip("numpy")
pytest.importorskip("lancedb")

from telegram_search.config.model_registry import model_spec, registry  # noqa: E402
from telegram_search.indexing.exclusions import update_authors  # noqa: E402
from telegram_search.indexing.service import SemanticService  # noqa: E402
from telegram_search.indexing.worker import SegmentWorker  # noqa: E402
from telegram_search.model_adapters.store import AdapterStore  # noqa: E402
from telegram_search.model_adapters.validation import validate_adapter  # noqa: E402
from telegram_search.search.hybrid import HybridSearch  # noqa: E402
from telegram_search.search.lexical import Filters  # noqa: E402
from telegram_search.shared.errors import UserError  # noqa: E402


def prepare(service, profile, *, reindex=False):
    service.prepare(profile, reindex=reindex)
    service.preparation.join(timeout=30)
    assert not service.preparation.is_alive()
    assert service.status()["preparation_state"] == "ready", service.status()["error"]


def test_custom_text_indexes_filters_restarts_and_requires_explicit_model_change(
    db, importer, tmp_path
):
    import sys

    path, data = package(tmp_path)
    store = AdapterStore(db.workspace)
    checked = validate_adapter(store, path, sys.executable, register=True)
    profile = "custom:" + checked["identity"]
    assert profile in registry(db.workspace) and profile not in registry()
    assert model_spec(profile, db.workspace).manifest["pooling"] == "adapter"
    result = load(
        importer,
        export(
            tmp_path / "source",
            [
                message(1, "Our train leaves at nine in the morning.", author="alice"),
                message(2, "I fixed the bicycle tire.", author="alice", date=1750086400),
                message(3, "Train sale advertisement.", author="bot", date=1750172800),
            ],
        ),
    )
    with db.connect() as conn:
        update_authors(conn, result["chat_id"], {"bot"})
    service = SemanticService(db, importer.lifecycle_lock, start_background=False)
    try:
        prepare(service, profile)
        space = service.encoder.space_id
        worker = SegmentWorker(
            db, service.encoder, service._vector_store(), importer.lifecycle_lock
        )
        with db.connect() as conn:
            works = [r[0] for r in conn.execute("SELECT id FROM index_work WHERE state='pending'")]
        for work in works:
            assert worker.run(work)["state"] == "done"
        search = HybridSearch(db, service, importer.lifecycle_lock)
        hits = search.search("train", Filters(chat_ids=[result["chat_id"]]), mode="meaning")[
            "results"
        ]
        assert hits[0]["message_id"] == 1
        assert all(part["message_id"] != 3 for hit in hits for part in hit.get("matched_parts", []))
        assert service.encoder.backend_info()["provider"] == "custom:cpu"
    finally:
        service.shutdown()
    restarted = SemanticService(db, importer.lifecycle_lock, start_background=False)
    try:
        assert restarted.encoder.space_id == space
        assert restarted.encoder.session is None
        again = HybridSearch(db, restarted, importer.lifecycle_lock)
        assert again.search("train", mode="meaning")["results"][0]["message_id"] == 1
        data["revision"] = "test-v2"
        path.write_text(json.dumps(data))
        second = validate_adapter(store, path, sys.executable, register=True)
        new_profile = "custom:" + second["identity"]
        with pytest.raises(UserError, match="переиндексацию"):
            restarted.prepare(new_profile)
        assert restarted.encoder.space_id == space
        prepare(restarted, new_profile, reindex=True)
        assert restarted.encoder.space_id != space
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
    finally:
        restarted.shutdown()


def test_broken_custom_registration_cannot_hide_builtins_or_execute_code(tmp_path):
    import sys

    store = AdapterStore(tmp_path / "workspace")
    path, _ = package(tmp_path)
    report = validate_adapter(store, path, sys.executable, register=True)
    identity = report["identity"]
    manifest = store.root / identity / "manifest.json"
    manifest.write_text("broken manifest")
    assert "berta" in registry(tmp_path / "workspace")
    assert "custom:" + identity not in registry(tmp_path / "workspace")


def test_custom_text_oom_uses_existing_batch_backoff_without_losing_chunks(db, importer, tmp_path):
    import sys

    path, _ = package(tmp_path)
    registered = validate_adapter(AdapterStore(db.workspace), path, sys.executable, register=True)
    load(
        importer,
        export(
            tmp_path / "source",
            [
                message(
                    i + 1,
                    f"OOM_TRIGGER public message number {i} with a train.",
                    date=1750000000 + i * 3601,
                )
                for i in range(3)
            ],
        ),
    )
    service = SemanticService(db, importer.lifecycle_lock, start_background=False)
    try:
        prepare(service, "custom:" + registered["identity"])
        with db.connect() as conn:
            work = conn.execute("SELECT id FROM index_work WHERE state='pending'").fetchone()[0]
        worker = SegmentWorker(
            db, service.encoder, service._vector_store(), importer.lifecycle_lock, batch_size=4
        )
        assert worker.run(work)["state"] == "done"
        assert service.encoder.index_batch_limit == 1
        with db.connect() as conn:
            row = conn.execute(
                "SELECT chunks_total,chunks_done FROM index_work WHERE id=?", (work,)
            ).fetchone()
            assert row[0] == row[1] == 3
    finally:
        service.shutdown()
