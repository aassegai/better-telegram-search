"""Resource waits are actionable and never presented as ongoing inference."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_media import fake_ocr, services
from test_ocr_queue import photos

from telegram_search.indexing.rates import record_rate


def test_memory_wait_reports_app_budget_hides_eta_and_preserves_checkpoint(db, importer, tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("lancedb")
    chat = photos(importer, tmp_path / "source", 3)
    _, media = services(db, importer)
    media.clip = SimpleNamespace(
        space_id="synthetic-visual",
        execution=SimpleNamespace(info=lambda: {"device": "cpu", "provider": "CPU"}),
        encode_images=lambda data: np.tile(np.eye(1, 512, dtype=np.float32), (len(data), 1)),
    )
    assert media._image_batch()
    with db.connect() as conn:
        conn.execute("UPDATE media_state SET images_enabled=1")
        for _ in range(3):
            record_rate(conn, "clip", media.clip, 1, 30, settings=db.settings, batch=1)
    usage = {"rss_bytes": 6000 * 1024**2, "ram_available_bytes": 20000 * 1024**2}
    media.resources = SimpleNamespace(snapshot=lambda: dict(usage))
    assert media.status(chat)["images_estimated_remaining_seconds"] is not None
    assert not media._memory_available("clip")  # Free system RAM does not override the app cap.
    for scope in (None, chat):
        state = media.status(scope)
        assert state["images_ready"] == 1
        assert state["images_estimated_remaining_seconds"] is None
        assert state["images_estimate"]["waiting_reason"] == "memory"
        assert state["preparation_estimate"]["waiting_reason"] == "memory"
        assert state["preparation_estimate"]["lower_seconds"] is None
        assert state["memory_wait"] == {
            "stages": ["clip"],
            "rss_bytes": usage["rss_bytes"],
            "limit_bytes": db.settings.memory_limit_mib * 1024**2,
            "available_bytes": usage["ram_available_bytes"],
        }
    # A healthy independent lane must not erase the image worker's wait.
    usage["rss_bytes"] = 3000 * 1024**2
    assert media._memory_available("ocr")
    usage["rss_bytes"] = 6000 * 1024**2
    assert media.status(chat)["memory_wait"]["stages"] == ["clip"]
    # Raising the configured cap unblocks work without resetting completed vectors.
    db.settings = replace(db.settings, memory_limit_mib=8192)
    assert media._memory_available("clip")
    state = media.status(chat)
    assert state["memory_wait"] is None and state["resource_error"] is None
    assert state["images_ready"] == 1
    assert media._image_batch() and media.status(chat)["images_ready"] == 2


def test_hybrid_ocr_wait_requires_both_lanes_and_pause_or_completion_takes_precedence(
    db, importer, tmp_path
):
    chat = photos(importer, tmp_path / "source", 1)
    _, media = services(db, importer)
    fake_ocr(media)
    media.ocr.cpu_peer = object()
    usage = {"rss_bytes": 6000 * 1024**2, "ram_available_bytes": 20000 * 1024**2}
    media.resources = SimpleNamespace(snapshot=lambda: dict(usage))
    assert not media._memory_available("ocr")
    assert media.status(chat)["memory_wait"] is None
    assert not media._memory_available("ocr_cpu")
    assert media.status(chat)["memory_wait"]["stages"] == ["ocr"]
    with db.connect() as conn:
        conn.execute("UPDATE chats SET ocr_paused=1")
    state = media.status(chat)
    assert state["memory_wait"] is None and state["ocr_estimate"]["paused"]
    with db.connect() as conn:
        conn.execute("UPDATE chats SET ocr_paused=0")
    # A current snapshot below the cap removes the wait even before the next worker tick.
    usage["rss_bytes"] = 2000 * 1024**2
    assert media.status(chat)["memory_wait"] is None
    # The direct synchronous worker is used only to finish this synthetic item.
    del media.ocr.cpu_peer
    assert media._ocr_one()
    usage["rss_bytes"] = 6000 * 1024**2
    assert media.status(chat)["memory_wait"] is None
    assert media.status(chat)["ocr_estimated_remaining_seconds"] == 0
