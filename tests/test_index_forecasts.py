import io
import time
from dataclasses import replace
from types import SimpleNamespace

from conftest import load
from PIL import Image
from test_media import fake_ocr, photo_export, services
from test_semantic import setup_index

from telegram_search.indexing.estimates import Estimates
from telegram_search.indexing.forecast import forecast
from telegram_search.indexing.rates import prediction, record_rate


def samples(count=12, work=100, seconds=10):
    return [(index, work, seconds) for index in range(count)]


def test_forecast_requires_recent_samples_and_weights_actual_work():
    assert forecast(samples(2), 1000, now=2)["measuring"]
    varied = [(0, 50, 5), (1, 1000, 100), (2, 10, 1)]
    value = forecast(varied, 1000, now=3)
    assert value["estimated_remaining_seconds"] == 100
    assert value["lower_seconds"] <= 100 <= value["upper_seconds"]
    assert forecast(varied, 1000, now=4000)["estimated_remaining_seconds"] is None
    assert forecast(varied, 0, now=4000)["estimated_remaining_seconds"] == 0
    # A tiny last batch has high overhead per item but should not dominate the archive.
    small_tail = forecast(samples() + [(12, 1, 5)], 1000, now=13)
    assert 100 <= small_tail["estimated_remaining_seconds"] < 102


def test_forecast_resists_single_stalls_but_adapts_to_sustained_changes():
    ordinary = samples(24)
    stalled = forecast(ordinary + [(24, 100, 10000)], 1000, now=25)
    assert 100 <= stalled["estimated_remaining_seconds"] < 120
    assert stalled["upper_seconds"] <= 200
    changed = ordinary + [(24 + index, 100, 50) for index in range(12)]
    assert forecast(changed, 1000, now=36)["estimated_remaining_seconds"] > 400
    assert forecast(changed, 1000, now=36)["eta_samples"] == 24
    projected = forecast(samples(), 1000, now=13, projection=1)
    assert projected["projected"] and projected["upper_seconds"] > 120


def test_samples_exclude_cold_errors_and_legacy_rates_and_are_bounded(db):
    encoder = SimpleNamespace(space_id="synthetic", threads=4)
    with db.connect() as conn:
        conn.execute("INSERT INTO index_rates VALUES('clip','synthetic','cpu',999999,50)")
        for options in ({"cold": True}, {"errors": 1}):
            record_rate(conn, "clip", encoder, 1, 9999, settings=db.settings, **options)
        assert (
            prediction(conn, "clip", encoder, db.settings, 100)["estimated_remaining_seconds"]
            is None
        )
        for _ in range(40):
            record_rate(conn, "clip", encoder, 10, 2, settings=db.settings, batch=4)
        assert conn.execute("SELECT COUNT(*) FROM index_rate_samples").fetchone()[0] == 24
        assert (
            prediction(conn, "clip", encoder, db.settings, 100, batch=4)[
                "estimated_remaining_seconds"
            ]
            == 20
        )
        assert (
            prediction(conn, "clip", encoder, db.settings, 100, batch=16)[
                "estimated_remaining_seconds"
            ]
            is None
        )
        changed = replace(db.settings, gpu_index_load_percent=50)
        assert (
            prediction(conn, "clip", encoder, changed, 100)["estimated_remaining_seconds"] is None
        )
    assert Estimates(db).images(encoder, 100) == 20
    with db.connect() as conn:
        conn.execute("UPDATE index_rate_samples SET observed_at=?", (time.time() - 4000,))
    assert Estimates(db).images(encoder, 100) is None


def test_hybrid_ocr_combines_lane_speeds_and_isolates_dialog_configuration(db):
    encoder = SimpleNamespace(version="synthetic-ocr", threads=4, cpu_peer=object())
    with db.connect() as conn:
        for _ in range(3):
            record_rate(
                conn, "ocr", encoder, 1, 1, settings=db.settings, lane="ocr", variant=(4, 32)
            )
        assert (
            prediction(conn, "ocr", encoder, db.settings, 100, variant=(4, 32))[
                "estimated_remaining_seconds"
            ]
            is None
        )
        for _ in range(3):
            record_rate(
                conn, "ocr", encoder, 1, 4, settings=db.settings, lane="ocr_cpu", variant=(4, 32)
            )
        assert (
            prediction(conn, "ocr", encoder, db.settings, 100, variant=(4, 32))[
                "estimated_remaining_seconds"
            ]
            == 80
        )
        assert (
            prediction(conn, "ocr", encoder, db.settings, 100, variant=(1, 8))[
                "estimated_remaining_seconds"
            ]
            is None
        )


def test_text_forecast_uses_remaining_padded_tokens_not_characters(db, importer, tmp_path):
    chat, service, worker, work_id = setup_index(db, importer, tmp_path)
    with db.connect() as conn:
        work = dict(conn.execute("SELECT * FROM index_work WHERE id=?", (work_id,)).fetchone())
        conn.execute("UPDATE index_work SET state='running' WHERE id=?", (work_id,))
    worker._build(work)
    with db.connect() as conn:
        conn.execute("UPDATE chats SET text_batch=2 WHERE id=?", (chat,))
        conn.execute("UPDATE index_work SET state='pending',chunks_done=2 WHERE id=?", (work_id,))
        conn.execute("UPDATE chunks SET tokens=CASE WHEN ordinal%2=0 THEN 100 ELSE 10 END")
        remaining = [
            row[0]
            for row in conn.execute("SELECT tokens FROM chunks WHERE ordinal>2 ORDER BY ordinal")
        ]
        for _ in range(3):
            record_rate(conn, "e5", service.encoder, 100, 10, settings=db.settings, batch=2)
    expected = (
        sum(
            len(remaining[i : i + 2]) * max(remaining[i : i + 2])
            for i in range(0, len(remaining), 2)
        )
        / 10
    )
    result = service.estimates.text_forecast(service.encoder, chat)
    assert result["estimated_remaining_seconds"] == expected
    assert not result["projected"]
    with db.connect() as conn:
        # History from a larger, faster batch is inapplicable to the current queue,
        # including the global endpoint with just one active chat.
        for _ in range(21):
            record_rate(conn, "e5", service.encoder, 6400, 1, settings=db.settings, batch=64)
        for _ in range(3):
            record_rate(conn, "e5", service.encoder, 100, 10, settings=db.settings, batch=2)
    assert (
        service.estimates.text_forecast(service.encoder)["estimated_remaining_seconds"] == expected
    )
    with db.connect() as conn:
        conn.execute("UPDATE chats SET text_paused=1 WHERE id=?", (chat,))
    for scope in (None, chat):
        state = service.status(scope)
        assert state["estimated_remaining_seconds"] is None and state["estimate"]["paused"]


def test_unbuilt_text_includes_chunk_building_time(db, importer, tmp_path):
    chat, service, _, _ = setup_index(db, importer, tmp_path)
    with db.connect() as conn:
        for _ in range(3):
            record_rate(conn, "e5", service.encoder, 100, 1, settings=db.settings, batch=4)
    assert (
        service.estimates.text(service.encoder, chat) is None
    )  # No chunk-building measurements yet.
    with db.connect() as conn:
        for _ in range(3):
            record_rate(conn, "text_build", service.encoder, 100, 20, settings=db.settings)
        raw = conn.execute("SELECT SUM(length(text)+length(author)+32) FROM messages").fetchone()[0]
    value = service.estimates.text_forecast(service.encoder, chat)
    assert value["projected"] and value["estimated_remaining_seconds"] >= raw / 5


def test_shared_photo_can_finish_through_unpaused_chat_and_ocr_variants_do_not_mix(
    db, importer, tmp_path
):
    first = load(importer, photo_export(tmp_path / "first"))["chat_id"]
    second = load(importer, photo_export(tmp_path / "second", chat_id=200))["chat_id"]
    _, media = services(db, importer)
    fake_ocr(media)
    with db.connect() as conn:
        for _ in range(3):
            record_rate(
                conn, "ocr", media.ocr, 1, 2, settings=db.settings, lane="ocr", variant=(1, 0)
            )
        conn.execute("UPDATE chats SET ocr_paused=1 WHERE id=?", (first,))
    assert media.status()["ocr_estimated_remaining_seconds"] == 2
    scoped = media.status(first)
    assert scoped["ocr_estimated_remaining_seconds"] is None and scoped["ocr_estimate"]["paused"]
    with db.connect() as conn:
        conn.execute("UPDATE chats SET ocr_batch=4,ocr_region_batch=32 WHERE id=?", (second,))
    assert media.status(second)["ocr_estimated_remaining_seconds"] is None
    assert media.status()["ocr_estimated_remaining_seconds"] is None
    with db.connect() as conn:
        conn.execute("UPDATE chats SET ocr_paused=1 WHERE id=?", (second,))
    global_state = media.status()
    assert global_state["ocr_estimated_remaining_seconds"] is None
    assert global_state["queues"]["ocr"]["paused"]
    assert global_state["preparation_estimate"]["paused"]


def test_global_workload_cache_keeps_progress_fresh_and_invalidates_configuration(
    db, importer, tmp_path, monkeypatch
):
    import telegram_search.indexing.media as module

    chat = load(importer, photo_export(tmp_path / "source"))["chat_id"]
    _, media = services(db, importer)
    fake_ocr(media)
    original = module.pending_groups
    calls = []

    def count(*args):
        calls.append(args[1])
        return original(*args)

    monkeypatch.setattr(module, "pending_groups", count)
    assert media.status()["ocr_ready"] == 0
    assert media.status()["ocr_ready"] == 0
    assert calls == ["ocr"]
    assert media._ocr_one()
    assert media.status()["ocr_estimated_remaining_seconds"] == 0
    assert calls == ["ocr"]  # Cached grouping never freezes the progress/remaining volume.
    with db.connect() as conn:
        conn.execute("UPDATE chats SET ocr_region_batch=32 WHERE id=?", (chat,))
    media.status()
    assert calls == ["ocr", "ocr"]
    load(importer, photo_export(tmp_path / "new-chat", chat_id=500))
    media.status()
    assert calls == ["ocr", "ocr", "ocr"]


def test_preparation_range_covers_serial_cpu_stages_and_dense_pause(db, importer, tmp_path):
    chat, semantic, _, _ = setup_index(db, importer, tmp_path)
    _, media = services(db, importer)
    # Use one shared compatible text encoder for both text and recognized OCR.
    media.semantic = semantic
    semantic.media = media
    fake_ocr(media, text="x" * 100)
    for index, color in enumerate(("red", "blue", "green", "yellow", "purple")):
        stream = io.BytesIO()
        Image.new("RGB", (64, 32), color).save(stream, format="PNG")
        load(
            importer,
            photo_export(tmp_path / color, chat_id=400 + index, photo_data=stream.getvalue()),
        )
    with db.connect() as conn:
        conn.execute("UPDATE media_state SET ocr_enabled=1")
    for _ in range(3):
        assert media._ocr_one()
        assert media._ocr_embeddings()
    with db.connect() as conn:
        conn.execute("DELETE FROM index_rate_samples")
        for _ in range(3):
            record_rate(
                conn, "ocr", media.ocr, 1, 1, settings=db.settings, lane="ocr", variant=(1, 0)
            )
            record_rate(
                conn,
                "ocr_dense:" + media.ocr.version,
                semantic.encoder,
                100,
                1,
                settings=db.settings,
                batch=4,
            )
    status = media.status()
    # Two remaining images need 2 seconds recognition + 2 seconds text inference
    # when these stages use the same CPU gate. A point estimate of max(2,2) was false.
    assert status["preparation_estimated_remaining_seconds"] is None
    bounds = status["preparation_estimate"]
    assert bounds["lower_seconds"] <= 4 <= bounds["upper_seconds"]
    with db.connect() as conn:
        conn.execute("UPDATE chats SET ocr_dense_paused=1")
    # Future recognized text is blocked too, even with no discovered dense queue.
    assert media.status()["preparation_estimate"]["paused"]
    with db.connect() as conn:
        conn.execute("UPDATE chats SET ocr_dense_paused=0")
    # Discover another text image, then pause only the semantic OCR queue.
    assert media._ocr_one()
    with db.connect() as conn:
        conn.execute("UPDATE chats SET ocr_dense_paused=1")
    status = media.status()
    assert status["ocr_dense_estimated_remaining_seconds"] is None
    assert status["ocr_dense_estimate"]["paused"]
    assert status["preparation_estimate"]["paused"]
