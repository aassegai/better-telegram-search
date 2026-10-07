"""Synthetic durable claims, crash recovery, retries and cache preservation."""

import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

from conftest import export, load, message
from PIL import Image
from test_media import services

from telegram_search.indexing.chats import ChatIndexing
from telegram_search.indexing.ocr_queue import OcrQueue
from telegram_search.search.lexical import Filters
from telegram_search.search.media import MediaSearch
from telegram_search.shared.errors import UserError


def photos(importer, root, count=5):
    root.mkdir()
    messages = []
    for i in range(count):
        stream = io.BytesIO()
        Image.new("RGB", (64, 32), (i * 20, 50, 100)).save(stream, format="PNG")
        (root / f"{i}.png").write_bytes(stream.getvalue())
        messages.append(message(i + 1, photo=f"{i}.png"))
    return load(importer, export(root, messages))["chat_id"]


def test_atomic_claims_recovery_cached_results_and_retry(db, importer, tmp_path):
    photos(importer, tmp_path / "source")
    queue = OcrQueue(db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(queue.claim, "v1", 3) for _ in range(2)]
        claims = [key for future in futures for key in future.result()]
    assert len(claims) == len(set(claims)) == 5
    assert not queue.claim("v1")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO ocr_cache(sha256,version,state,text,text_normalized) "
            "VALUES(?,'v1','ready','checkpoint','checkpoint')",
            (claims[0],),
        )
        conn.execute(
            "INSERT INTO ocr_cache(sha256,version,state) VALUES(?,'v1','failed')", (claims[1],)
        )
    recovered = OcrQueue(db)
    assert set(recovered.claim("v1", 4)) == set(claims[2:])
    with db.connect() as conn:
        conn.execute("DELETE FROM ocr_cache WHERE state='failed'")
    assert recovered.claim("v1", 4) == [claims[1]]
    # Changing version creates new work without deleting any earlier cache.
    assert len(recovered.claim("v2", 4)) == 4
    with db.connect() as conn:
        assert (
            conn.execute("SELECT text FROM ocr_cache WHERE state='ready'").fetchone()[0]
            == "checkpoint"
        )


def test_queue_skips_paused_deleted_and_stale_references(db, importer, tmp_path):
    chat = photos(importer, tmp_path / "source", 2)
    queue = OcrQueue(db)
    with db.connect() as conn:
        conn.execute("UPDATE chats SET ocr_paused=1")
    assert not queue.claim("v1", 4)
    with db.connect() as conn:
        conn.execute("UPDATE chats SET ocr_paused=0")
        conn.execute("UPDATE media_refs SET status='missing' WHERE message_id=1")
    assert len(queue.claim("v1", 4)) == 1
    with db.connect() as conn:
        conn.execute("UPDATE media_refs SET status='ready' WHERE message_id=1")
    assert len(queue.claim("v1", 4)) == 1
    with db.connect() as conn:
        conn.execute("DELETE FROM chats WHERE id=?", (chat,))
    OcrQueue(db)
    assert not queue.claim("v1", 4)
    db.compact()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ocr_work").fetchone()[0] == 0


def test_old_claim_cannot_release_reimported_photo(db, importer, tmp_path):
    chat = photos(importer, tmp_path / "source", 1)
    queue = OcrQueue(db)
    previous = queue.claim("v1", with_tokens=True)
    with db.connect() as conn:
        conn.execute("DELETE FROM chats WHERE id=?", (chat,))
    db.compact()
    load(importer, tmp_path / "source/result.json")
    current = queue.claim("v1", with_tokens=True)
    assert previous[0][0] == current[0][0] and previous[0][1] != current[0][1]
    queue.release("v1", previous)
    with db.connect() as conn:
        row = conn.execute("SELECT state,claim_token FROM ocr_work").fetchone()
        assert tuple(row) == ("running", current[0][1])
    queue.release("v1", current)
    assert queue.claim("v1") == [current[0][0]]


def test_deleted_and_reimported_photo_rejects_stale_worker_result(db, importer, tmp_path):
    chat = photos(importer, tmp_path / "source", 1)
    _, media = services(db, importer)
    peer = SimpleNamespace(
        version="v1", recognize=lambda data: {"text": "current", "confidence": 90}
    )

    def recognize(data):
        with db.connect() as conn:
            conn.execute("DELETE FROM chats WHERE id=?", (chat,))
        db.compact()
        load(importer, tmp_path / "source/result.json")
        assert not media._ocr_one(peer)  # The old lane still owns the content in memory.
        return {"text": "stale", "confidence": 90}

    media.ocr = SimpleNamespace(version="v1", recognize=recognize, cpu_peer=peer)
    assert media._ocr_one()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ocr_cache").fetchone()[0] == 0
        assert conn.execute("SELECT state FROM ocr_work").fetchone()[0] == "pending"
    assert media._ocr_one(peer)
    with db.connect() as conn:
        assert conn.execute("SELECT text FROM ocr_cache").fetchone()[0] == "current"


def test_chat_batch_mapping_independent_semantic_pause_and_lexical_publication(
    db, importer, tmp_path
):
    chat = photos(importer, tmp_path / "source", 3)
    semantic, media = services(db, importer)
    batches = []

    def recognize(images):
        batches.append((len(images), media.ocr.worker.region_batch))
        return [
            {"text": "контроль " + hashlib.sha256(data).hexdigest(), "confidence": 99}
            for data in images
        ]

    media.ocr = SimpleNamespace(
        version="v1", worker=SimpleNamespace(), batch_limit=4, recognize_many=recognize
    )
    indexing = ChatIndexing(db, semantic, media, importer.lifecycle_lock)
    indexing.settings(chat, {"ocr_batch_size": 2, "ocr_region_batch_size": 16})
    indexing.control(chat, "ocr_dense", "pause")
    assert media._ocr_run() and media._ocr_run() and not media._ocr_run()
    assert batches == [(2, 16), (1, 16)]
    with db.connect() as conn:
        assert all(
            row["text"] == "контроль " + row["sha256"]
            for row in conn.execute("SELECT * FROM ocr_cache")
        )
    hits, _ = MediaSearch(db, media, semantic, importer.lifecycle_lock).search(
        "контроль", Filters([chat]), kind="ocr"
    )
    assert len(hits) == 3
    status = indexing.status(chat)["media"]
    assert status["ocr_dense_paused"] and not status["ocr_paused"]
    assert status["ocr_ready"] == status["photo_messages"] == status["photo_attachments"] == 3
    assert not media._ocr_embeddings()
    # Plain batch-size changes preserve ready work.
    indexing.settings(chat, {"ocr_batch_size": 4})
    assert not media._ocr_run()


def test_native_batch_timeout_retries_individually_without_failing_good_neighbors(
    db, importer, tmp_path
):
    chat = photos(importer, tmp_path / "source", 2)
    _, media = services(db, importer)
    db.settings = replace(db.settings, ocr_batch_size=2)
    calls = []

    def recognize(images):
        calls.append(len(images))
        if len(images) > 1:
            raise UserError("synthetic timeout")
        return [{"text": "resumed", "confidence": 90}]

    media.ocr = SimpleNamespace(
        version="v1", worker=SimpleNamespace(), batch_limit=4, recognize_many=recognize
    )
    assert media._ocr_run()
    assert media.status(chat)["ocr_failed"] == 0 and media.ocr.batch_limit == 1
    assert media._ocr_run() and media._ocr_run()
    assert calls == [2, 1, 1]
    assert media.status(chat)["ocr_ready"] == 2
