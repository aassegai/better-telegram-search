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


def test_ocr_eta_measures_its_own_queue_and_preserves_progress_on_restart(
    db, importer, tmp_path, monkeypatch
):
    chats = []
    for i, color in enumerate(("red", "blue", "green")):
        stream = io.BytesIO()
        Image.new("RGB", (64, 32), color).save(stream, format="PNG")
        chats.append(
            load(
                importer,
                photo_export(tmp_path / color, chat_id=100 + i, photo_data=stream.getvalue()),
            )["chat_id"]
        )
    _, media = services(db, importer)
    fake_ocr(media)
    clock = [0]
    monkeypatch.setattr("telegram_search.indexing.media.time.monotonic", lambda: clock[0])

    def stage(seconds, result):
        clock[0] += seconds
        return result

    media.ocr.recognize = lambda data: stage(2, {"text": "тест", "confidence": 90})
    monkeypatch.setattr(media, "_image_batch", lambda: stage(8, False))
    monkeypatch.setattr(media, "_ocr_embeddings", lambda: stage(5, False))
    assert media._index_cycle()
    assert media.status()["ocr_estimated_remaining_seconds"] is None
    assert media._index_cycle()
    status = media.status()
    assert status["ocr_ready"] == 2 and status["total_photos"] == 3
    assert status["ocr_estimated_remaining_seconds"] == 2
    for chat in chats:
        scoped = media.status(chat)
        assert scoped["total_photos"] == 1
        assert scoped["ocr_estimated_remaining_seconds"] == (1 - scoped["ocr_ready"]) * 2

    _, restarted = services(db, importer)
    fake_ocr(restarted)
    assert restarted.status()["ocr_ready"] == 2
    assert restarted.status()["ocr_estimated_remaining_seconds"] == 2
    restarted.ocr.threads = 2
    assert restarted.status()["ocr_estimated_remaining_seconds"] is None
    restarted.ocr.version = "different-recognition-model"
    assert restarted.status()["ocr_ready"] == 0
    assert restarted.status()["ocr_estimated_remaining_seconds"] is None


def test_ocr_failed_items_finish_queue_and_pause_flushes_completed_results(
    db, importer, tmp_path, monkeypatch
):
    load(importer, photo_export(tmp_path / "source"))
    _, media = services(db, importer)
    fake_ocr(media)

    def failed(data):
        raise UserError("synthetic timeout")

    media.ocr.recognize = failed
    assert media._index_cycle()
    status = media.status()
    assert status["ocr_failed"] == 1 and status["ocr_ready"] == 0
    assert status["ocr_estimated_remaining_seconds"] == 0
    media.control("retry")

    def pause(data):
        media.control("pause")
        return {"text": "сохранить готовый результат при паузе", "confidence": 90}

    media.ocr.recognize = pause
    assert media._index_cycle()
    status = media.status()
    assert status["ocr_failed"] == 0 and status["ocr_ready"] == 1
    with db.connect() as conn:
        assert conn.execute("SELECT batches FROM index_rates WHERE kind='ocr'").fetchone()[0] == 2


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
        {"image_batch": 33},
        {"ocr_max_edge": 0},
    ):
        with pytest.raises(UserError):
            workspace.update_settings(body)
    assert workspace.update_settings({"cpu_threads": 2, "image_batch": 2})["cpu_threads"] == 2
    from telegram_search.config.settings import Settings

    assert Settings.load(db.workspace).image_batch == 2
    assert not list(db.workspace.glob(".config-*.tmp"))


def test_display_settings_do_not_interrupt_active_models_and_old_configs_load(db, importer):
    import json

    from telegram_search.config.settings import Settings

    semantic, media = services(db, importer)
    workspace = WorkspaceService(db, importer, semantic, media)
    semantic.preparation = SimpleNamespace(is_alive=lambda: True)
    media.running = True
    fake_ocr(media)
    original_ocr = media.ocr
    workspace.update_settings({"search_result_limit": 7, "display_chunk_size": 3})
    assert media.ocr is original_ocr and media.running
    assert Settings.load(db.workspace).display_chunk_size == 3
    with pytest.raises(UserError):
        workspace.update_settings({"cpu_threads": 2, "display_chunk_size": 4})
    assert db.settings.display_chunk_size == 3
    config = db.workspace / "config.json"
    legacy = json.loads(config.read_text())
    del legacy["search_result_limit"], legacy["display_chunk_size"]
    config.write_text(json.dumps(legacy), encoding="utf-8")
    assert Settings.load(db.workspace).search_result_limit == 20
    assert Settings.load(db.workspace).display_chunk_size == 10


def test_ocr_cards_use_configurable_context_without_losing_photo_anchor(db, importer, tmp_path):
    root = photo_export(tmp_path / "source").parent
    first = load(
        importer,
        export(
            root,
            [
                message(i, "подпись", date=i, **({"photo": "photo.png"} if i == 3 else {}))
                for i in range(1, 6)
            ],
        ),
    )
    semantic, media = services(db, importer)
    fake_ocr(media)
    assert media._ocr_one()
    search = MediaSearch(db, media, semantic, importer.lifecycle_lock)
    for size, expected in ((1, [3]), (3, [2, 3, 4])):
        hits, _ = search.search("12345", Filters([first["chat_id"]]), kind="ocr", chunk_size=size)
        assert len(hits) == 1 and hits[0]["message_id"] == 3
        assert [row["message_id"] for row in hits[0]["messages"]] == expected
        assert next(row for row in hits[0]["messages"] if row["message_id"] == 3)["media"]


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
        execution=SimpleNamespace(
            info=lambda: {"device": "cpu", "provider": "CPUExecutionProvider"}
        ),
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


def test_image_cosine_scores_preserve_rank_and_survive_combined_search(db, importer, tmp_path):
    np = pytest.importorskip("numpy")
    source = tmp_path / "synthetic-ranking"
    source.mkdir()
    messages = []
    for mid, color in ((10, "red"), (20, "blue"), (30, "green")):
        Image.new("RGB", (64, 64), color).save(source / f"{mid}.png")
        messages.append(message(mid=mid, text="ranking", photo=f"{mid}.png"))
    load(importer, export(source, messages))
    semantic, media = services(db, importer)
    media.clip = SimpleNamespace(
        execution=SimpleNamespace(info=lambda: {"device": "cpu"}),
        space_id="synthetic-ranking",
        encode_text=lambda texts: np.eye(1, 512, dtype=np.float32),
    )
    similarities = {10: -0.5, 20: 1.0, 30: 0.0}
    with db.connect() as conn:
        refs = conn.execute(
            "SELECT message_id,sha256 FROM media_refs ORDER BY message_id"
        ).fetchall()
    vectors = np.zeros((len(refs), 512), dtype=np.float32)
    for index, ref in enumerate(refs):
        similarity = similarities[ref["message_id"]]
        vectors[index, :2] = similarity, np.sqrt(1 - similarity**2)
    media._publish(media.clip.space_id, 512, "image", [ref["sha256"] for ref in refs], vectors)
    search = MediaSearch(db, media, semantic, importer.lifecycle_lock)
    hits, warnings = search.search("ranking", Filters(), kind="images")
    assert not warnings and [hit["message_id"] for hit in hits] == [20, 30, 10]
    assert [hit["image_similarity"] for hit in hits] == pytest.approx([1.0, 0.0, -0.5])
    combined = UnifiedSearch(HybridSearch(db, semantic, importer.lifecycle_lock), search)
    for modalities in (["images"], ["text", "images"]):
        result = combined.search("ranking", Filters(), False, 20, "words", modalities=modalities)
        assert len(result["results"]) == 3
        for hit in result["results"]:
            assert hit["image_similarity"] == pytest.approx(similarities[hit["message_id"]])
        assert [hit["score"] for hit in result["results"]] == sorted(
            [hit["score"] for hit in result["results"]], reverse=True
        )
        if modalities == ["images"]:
            assert [hit["message_id"] for hit in result["results"]] == [20, 30, 10]
        else:
            assert any({"words", "image"} <= set(hit["matched_by"]) for hit in result["results"])


def test_media_scan_rejects_unpublished_vectors_and_other_ocr_versions_before_top_k(
    db, importer, tmp_path
):
    from test_semantic import TestEncoder

    np = pytest.importorskip("numpy")
    job = load(importer, photo_export(tmp_path / "source"))
    semantic, media = services(db, importer)
    semantic.activate(TestEncoder())
    fake_ocr(media)
    assert media._ocr_one() and media._ocr_embeddings()
    store = semantic._vector_store()
    ghosts = [
        {"id": f"ghost-{i}", "chat_id": "synthetic", "utc_day": "media", "generation": 1}
        for i in range(1100)
    ]
    store.upsert("synthetic", 4, ghosts, np.tile([1.0, 0.0, 0.0, 0.0], (len(ghosts), 1)))
    search = MediaSearch(db, media, semantic, importer.lifecycle_lock)
    hits, _ = search.search("unmatched", Filters([job["chat_id"]]), kind="ocr", mode="meaning")
    assert len(hits) == 1 and hits[0]["matched_by"] == ["ocr_meaning"]
    assert not search.search("unmatched", Filters(["absent"]), kind="ocr", mode="meaning")[0]
    with db.connect() as conn:
        conn.execute("UPDATE media_embeddings SET ocr_version='older-model'")
    assert (
        search._dense("synthetic", 4, np.array([1, 0, 0, 0]), "ocr", Filters(), media.ocr.version)
        == []
    )


def test_prepared_clip_without_published_images_returns_empty_without_vector_table(
    db, importer, tmp_path, monkeypatch
):
    np = pytest.importorskip("numpy")
    load(importer, photo_export(tmp_path / "source"))
    semantic, media = services(db, importer)
    media.clip = SimpleNamespace(
        space_id="unindexed-clip", encode_text=lambda texts: np.eye(1, 512, dtype=np.float32)
    )

    def forbidden():
        raise AssertionError("No LanceDB table is needed before any eligible vectors exist")

    monkeypatch.setattr(semantic, "_vector_store", forbidden)
    hits, warnings = MediaSearch(db, media, semantic, importer.lifecycle_lock).search(
        "synthetic", Filters(), kind="images"
    )
    assert hits == [] and warnings == []


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
        media.control("pause", kind="ocr_dense")

    encoder = TestEncoder(on_encode=pause)
    semantic.activate(encoder)
    fake_ocr(media, "поезд " + "ремонт " * 2000)
    media._ocr_one()
    assert media._ocr_embeddings()
    assert len(encoder.calls) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_embeddings").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM media_failures").fetchone()[0] == 0
    media.control("resume", kind="ocr_dense")
    encoder.on_encode = lambda texts: (_ for _ in ()).throw(UserError("synthetic failure"))
    assert media._ocr_embeddings()
    assert not media._ocr_embeddings()
    assert media.status()["ocr_dense_failed"] == 1
    encoder.on_encode = None
    media.ocr.version = "new-version"
    assert media.status()["ocr_dense_failed"] == 0
    media._ocr_one()
    assert media._ocr_embeddings()
    assert not media._ocr_embeddings()
    hits, _ = MediaSearch(db, media, semantic, importer.lifecycle_lock).search(
        "поезд", Filters(), kind="ocr", mode="meaning"
    )
    assert hits[0]["ocr_range"]["char_start"] == 0


def test_semantic_ocr_error_counts_are_scoped_and_retry_preserves_recognition(
    db, importer, tmp_path
):
    from test_semantic import TestEncoder

    from telegram_search.indexing.chats import ChatIndexing

    first = load(importer, photo_export(tmp_path / "first"))["chat_id"]
    # Shared content has one cached failure despite belonging to two chats.
    shared = load(importer, photo_export(tmp_path / "shared", chat_id=200))["chat_id"]
    stream = io.BytesIO()
    Image.new("RGB", (64, 32), "blue").save(stream, format="PNG")
    other = load(
        importer,
        photo_export(tmp_path / "other", chat_id=300, photo_data=stream.getvalue()),
    )["chat_id"]
    semantic, media = services(db, importer)

    def fail(texts):
        raise UserError("synthetic inference failure")

    encoder = TestEncoder(on_encode=fail)
    semantic.activate(encoder)
    fake_ocr(media)
    assert media._ocr_one() and media._ocr_one()
    assert media._ocr_embeddings() and media._ocr_embeddings()
    assert not media._ocr_embeddings()
    assert media.status()["ocr_dense_failed"] == 2
    for chat in (first, shared, other):
        assert media.status(chat)["ocr_dense_failed"] == 1
        assert media.status(chat)["ocr_failed"] == 0
        assert media.status(chat)["ocr_ready"] == 1

    with db.connect() as conn:
        cache_before = [
            tuple(row) for row in conn.execute("SELECT * FROM ocr_cache ORDER BY sha256")
        ]
        sha = conn.execute("SELECT sha256 FROM media_refs WHERE chat_id=?", (first,)).fetchone()[0]
        # Failure counters must exclude unrelated embedding spaces.
        conn.execute("INSERT INTO media_failures VALUES(?, 'old-model', 'stale')", (sha,))
    assert media.status()["ocr_dense_failed"] == 2
    indexing = ChatIndexing(db, semantic, media, importer.lifecycle_lock)
    assert indexing.control(first, "ocr", "retry")["media"]["ocr_dense_failed"] == 1
    retried = indexing.control(first, "ocr_dense", "retry")
    assert retried["media"]["ocr_dense_failed"] == 0
    assert media.status(shared)["ocr_dense_failed"] == 0
    assert media.status(other)["ocr_dense_failed"] == 1
    assert media.status()["ocr_dense_failed"] == 1
    with db.connect() as conn:
        assert cache_before == [
            tuple(row) for row in conn.execute("SELECT * FROM ocr_cache ORDER BY sha256")
        ]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM media_failures WHERE space_id='old-model'"
            ).fetchone()[0]
            == 1
        )

    encoder.on_encode = None
    assert media._ocr_embeddings()
    assert not media._ocr_embeddings()  # Unrelated chat's failure still needs its own retry.
    assert media.status(first)["ocr_dense_ready"] == 1
    assert media.status()["ocr_ready"] == 2
    assert media.status()["ocr_dense_failed"] == 1


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
