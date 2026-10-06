import io
from types import SimpleNamespace

import pytest
from conftest import export, load, message
from PIL import Image

from telegram_search.indexing.media import MediaService
from telegram_search.indexing.service import SemanticService
from telegram_search.search.hybrid import HybridSearch
from telegram_search.search.lexical import Filters
from telegram_search.search.media import MediaSearch, UnifiedSearch
from telegram_search.shared.errors import UserError
from telegram_search.sources.service import WorkspaceService


def photo_export(root, text="подпись", chat_id=100, photo_data=None):
    root.mkdir(parents=True, exist_ok=True)
    if photo_data is None:
        stream = io.BytesIO()
        Image.new("RGB", (64, 32), "red").save(stream, format="PNG")
        photo_data = stream.getvalue()
    (root / "photo.png").write_bytes(photo_data)
    return export(root, [message(text=text, photo="photo.png")], chat_id=chat_id)


def services(db, importer):
    semantic = SemanticService(db, importer.lifecycle_lock, start_background=False)
    media = MediaService(db, importer.lifecycle_lock, semantic, start_background=False)
    return semantic, media


def fake_ocr(media, text="Стоимость ремонта 12345 рублей. <script>опасно</script>"):
    calls = []
    media.ocr = SimpleNamespace(
        version="synthetic-ocr-v1",
        recognize=lambda data: calls.append(len(data)) or {"text": text, "confidence": 91},
    )
    return calls


def test_ocr_deduplicates_reimports_and_alternative_chats(db, importer, tmp_path):
    first = load(importer, photo_export(tmp_path / "source"))
    semantic, media = services(db, importer)
    calls = fake_ocr(media)
    assert media._ocr_one()
    assert not media._ocr_one()
    load(importer, tmp_path / "source/result.json")
    second = load(importer, photo_export(tmp_path / "other", chat_id=200))
    assert not media._ocr_one()
    assert len(calls) == 1
    search = MediaSearch(db, media, semantic, importer.lifecycle_lock)
    hits, _ = search.search("12345", Filters([second["chat_id"]]), kind="ocr", exact=True)
    assert len(hits) == 1 and hits[0]["chat_id"] == second["chat_id"]
    assert hits[0]["matched_by"] == ["ocr_words"]
    assert "<script>" in hits[0]["ocr_text"]
    hits, _ = search.search("ремонта 12345", Filters([first["chat_id"]], ["absent"]), kind="ocr")
    assert not hits
    with db.connect() as conn:
        conn.execute("DELETE FROM chats WHERE id=?", (first["chat_id"],))
    db.compact()
    assert media.status()["ocr_ready"] == 1
    with db.connect() as conn:
        conn.execute("DELETE FROM chats WHERE id=?", (second["chat_id"],))
    db.compact()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ocr_cache").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM media_vector_cleanup").fetchone()[0] == 1


def test_media_publication_does_not_resurrect_deleted_chat(db, importer, tmp_path):
    job = load(importer, photo_export(tmp_path / "source"))
    semantic, media = services(db, importer)

    def recognize(data):
        with db.connect() as conn:
            conn.execute("DELETE FROM chats WHERE id=?", (job["chat_id"],))
        return {"text": "нельзя публиковать", "confidence": 100}

    media.ocr = SimpleNamespace(version="race", recognize=recognize)
    assert media._ocr_one()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ocr_cache").fetchone()[0] == 0


def test_source_check_relink_and_changed_hash_exclude_cache(db, importer, tmp_path):
    path = photo_export(tmp_path / "source")
    load(importer, path)
    semantic, media = services(db, importer)
    fake_ocr(media)
    media._ocr_one()
    workspace = WorkspaceService(db, importer, semantic, media)
    source = workspace.sources()[0]
    moved = tmp_path / "moved"
    path.parent.rename(moved)
    result = workspace.probe(source["id"], new_path=str(moved), expected_path=source["path"])
    assert result["counts"]["ready"] == 1
    assert workspace.sources()[0]["available"]
    (moved / "photo.png").write_bytes(b"changed")
    assert workspace.probe(source["id"])["counts"]["changed"] == 1
    hits, _ = MediaSearch(db, media, semantic, importer.lifecycle_lock).search(
        "12345", Filters(), kind="ocr"
    )
    assert hits == []
    with pytest.raises(UserError):
        workspace.probe(source["id"], new_path=str(moved), expected_path=source["path"])
    assert workspace.deletion_estimate(source["chat_id"])["source_files_preserved"]
    assert workspace.sizes()["total_bytes"] > 0


def test_settings_reject_invalid_values_and_write_atomic_config(db, importer):
    semantic, media = services(db, importer)
    workspace = WorkspaceService(db, importer, semantic, media)
    for body in (
        {"cpu_threads": True},
        {"device": "cuda"},
        {"image_batch": 9},
        {"ocr_max_edge": 0},
    ):
        with pytest.raises(UserError):
            workspace.update_settings(body)
    assert workspace.update_settings({"cpu_threads": 2, "image_batch": 2})["cpu_threads"] == 2
    from telegram_search.config.settings import Settings

    assert Settings.load(db.workspace).image_batch == 2
    assert not list(db.workspace.glob(".config-*.tmp"))


def test_ocr_dense_parts_preserve_ranges_and_version(db, importer, tmp_path):
    pytest.importorskip("lancedb")
    from test_semantic import TestEncoder

    load(importer, photo_export(tmp_path / "source"))
    semantic, media = services(db, importer)
    semantic.activate(TestEncoder())
    fake_ocr(media, text="ремонт поезд встреча " * 800)
    media._ocr_one()
    assert media._ocr_embeddings()
    assert not media._ocr_embeddings()
    with db.connect() as conn:
        parts = conn.execute(
            "SELECT char_start,char_end FROM media_embeddings ORDER BY ordinal"
        ).fetchall()
    assert (
        len(parts) > 1
        and parts[0][0] == 0
        and parts[-1][1] == len(media.ocr.recognize(b"")["text"])
    )
    hits, warnings = MediaSearch(db, media, semantic, importer.lifecycle_lock).search(
        "ремонт", Filters(), kind="ocr", mode="meaning"
    )
    assert hits and hits[0]["matched_by"] == ["ocr_meaning"] and not warnings
    media.ocr.version = "synthetic-ocr-v2"
    media._ocr_one()
    assert media._ocr_embeddings()
    assert not media._ocr_embeddings()


def test_clip_image_vectors_use_separate_space_and_prefilter(db, importer, tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("lancedb")
    job = load(importer, photo_export(tmp_path / "source"))
    semantic, media = services(db, importer)
    media.clip = SimpleNamespace(
        space_id="a" * 64,
        encode_images=lambda data: np.tile(np.eye(1, 512, dtype=np.float32), (len(data), 1)),
        encode_text=lambda texts: np.eye(1, 512, dtype=np.float32),
    )
    assert media._image_batch()
    assert not media._image_batch()
    search = MediaSearch(db, media, semantic, importer.lifecycle_lock)
    hits, warnings = search.search("Красная фотография", Filters([job["chat_id"]]), kind="images")
    assert len(hits) == 1 and hits[0]["matched_by"] == ["image"] and not warnings
    assert not search.search("Красная фотография", Filters(["wrong"]), kind="images")[0]
    combined = UnifiedSearch(HybridSearch(db, semantic, importer.lifecycle_lock), search)
    result = combined.search("подпись", Filters(), False, 20, "words", "all")
    assert len(result["results"]) == 1
    assert set(result["results"][0]["matched_by"]) == {"words", "image"}
    with db.connect() as conn:
        conn.execute("DELETE FROM chats")
    db.compact()
    media.cleanup(compact=True)
    assert semantic._vector_store().table(media.clip.space_id, 512).count_rows() == 0


def test_clip_rejects_extreme_aspect_before_resize(monkeypatch):
    pytest.importorskip("numpy")
    from telegram_search.inference.clip import image_tensor

    stream = io.BytesIO()
    Image.new("RGB", (1, 10000), "red").save(stream, format="PNG")

    def forbidden_resize(*args, **kwargs):
        raise AssertionError("unbounded allocation attempted")

    monkeypatch.setattr(Image.Image, "resize", forbidden_resize)
    with pytest.raises(UserError):
        image_tensor(stream.getvalue(), {"size": {"shortest_edge": 224}})


def test_crash_after_lance_write_keeps_recovery_space(db, importer, tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    pytest.importorskip("lancedb")
    job = load(importer, photo_export(tmp_path / "source"))
    semantic, media = services(db, importer)
    with db.connect() as conn:
        sha = conn.execute("SELECT sha256 FROM media_refs").fetchone()[0]
    vectors = semantic._vector_store()
    original = vectors.upsert

    def crash(*args):
        original(*args)
        raise RuntimeError("synthetic crash before SQLite publication")

    monkeypatch.setattr(vectors, "upsert", crash)
    with pytest.raises(RuntimeError):
        media._publish("crash-space", 4, "image", [sha], np.eye(1, 4, dtype=np.float32))
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_spaces").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM media_embeddings").fetchone()[0] == 0
        conn.execute("DELETE FROM chats WHERE id=?", (job["chat_id"],))
    db.compact()
    media.cleanup(compact=True)
    assert vectors.table("crash-space", 4).count_rows() == 0


def test_failed_ocr_settings_preserve_previous_state(db, importer, monkeypatch):
    semantic, media = services(db, importer)
    original = SimpleNamespace(version="working")
    media.ocr = original

    def broken_verify():
        raise UserError("synthetic damaged dictionary")

    monkeypatch.setattr(media, "_new_ocr", lambda settings: SimpleNamespace(verify=broken_verify))
    workspace = WorkspaceService(db, importer, semantic, media)
    before = (db.workspace / "config.json").read_text()
    with pytest.raises(UserError):
        workspace.update_settings({"cpu_threads": 2})
    assert db.settings.cpu_threads == 4
    assert (db.workspace / "config.json").read_text() == before
    assert media.ocr is original


def test_ocr_pause_aborts_publication_and_new_version_retries_failure(db, importer, tmp_path):
    pytest.importorskip("lancedb")
    from test_semantic import TestEncoder

    load(importer, photo_export(tmp_path / "source"))
    semantic, media = services(db, importer)

    def pause(texts):
        semantic.control("pause")

    encoder = TestEncoder(on_encode=pause)
    semantic.activate(encoder)
    fake_ocr(media, "поезд " + "ремонт " * 2000)
    media._ocr_one()
    assert media._ocr_embeddings()
    assert len(encoder.calls) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_embeddings").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM media_failures").fetchone()[0] == 0
    semantic.control("resume")
    encoder.on_encode = lambda texts: (_ for _ in ()).throw(UserError("synthetic failure"))
    assert media._ocr_embeddings()
    assert not media._ocr_embeddings()
    encoder.on_encode = None
    media.ocr.version = "new-version"
    media._ocr_one()
    assert media._ocr_embeddings()
    assert not media._ocr_embeddings()
    hits, _ = MediaSearch(db, media, semantic, importer.lifecycle_lock).search(
        "поезд", Filters(), kind="ocr", mode="meaning"
    )
    assert hits[0]["ocr_range"]["char_start"] == 0


def test_failed_model_commit_does_not_activate_in_memory(db, importer, monkeypatch):
    from contextlib import contextmanager

    semantic, media = services(db, importer)
    candidate = SimpleNamespace(prepare=lambda **kwargs: None)
    monkeypatch.setattr(media, "_new_ocr", lambda: candidate)
    original = db.connect
    interrupted = False

    @contextmanager
    def connect():
        nonlocal interrupted
        with original() as conn:
            yield conn
            state = conn.execute("SELECT preparation_state FROM media_state WHERE id=1").fetchone()[
                0
            ]
            if state == "ready" and not interrupted:
                interrupted = True
                raise RuntimeError("synthetic commit failure")

    monkeypatch.setattr(db, "connect", connect)
    media._prepare("ocr", True)
    assert media.ocr is None
    with original() as conn:
        row = conn.execute(
            "SELECT ocr_enabled,preparation_state FROM media_state WHERE id=1"
        ).fetchone()
        assert tuple(row) == (0, "failed")


def test_ocr_actual_native_version_changes_cache_identity(tmp_path, monkeypatch):
    import telegram_search.inference.ocr as module

    monkeypatch.setattr(module.importlib.metadata, "version", lambda name: "synthetic-version")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=b"Tesseract version A"),
    )
    first = module.OcrEngine(tmp_path).version
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=b"Tesseract version B"),
    )
    assert module.OcrEngine(tmp_path).version != first


def test_compact_preserves_inflight_ocr_parts(db, importer, tmp_path, monkeypatch):
    pytest.importorskip("lancedb")
    from test_semantic import TestEncoder

    load(importer, photo_export(tmp_path / "source"))
    semantic, media = services(db, importer)
    semantic.activate(TestEncoder())
    fake_ocr(media, "ремонт поезд " * 2000)
    media._ocr_one()
    store = semantic._vector_store()
    original = store.upsert
    called = False

    def compact_between_batches(*args):
        nonlocal called
        original(*args)
        if not called:
            called = True
            media.cleanup(compact=True)

    monkeypatch.setattr(store, "upsert", compact_between_batches)
    assert media._ocr_embeddings()
    assert not media._ocr_embeddings()
    with db.connect() as conn:
        parts = conn.execute("SELECT COUNT(*) FROM media_embeddings WHERE kind='ocr'").fetchone()[0]
    table = store.table(semantic.encoder.space_id, semantic.encoder.spec.dimension)
    assert table.count_rows() == parts > 4
    assert not media.ocr_staging_ids


def test_cleanup_without_any_vector_space_does_not_require_semantic_runtime(
    db, importer, tmp_path, monkeypatch
):
    load(importer, photo_export(tmp_path / "source"))
    semantic, media = services(db, importer)
    semantic.available = False

    def forbidden_store():
        raise AssertionError("Lance should not be required when no vectors were written")

    monkeypatch.setattr(semantic, "_vector_store", forbidden_store)
    with db.connect() as conn:
        conn.execute("DELETE FROM chats")
    db.compact()
    media.cleanup(compact=True)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_vector_cleanup").fetchone()[0] == 0
