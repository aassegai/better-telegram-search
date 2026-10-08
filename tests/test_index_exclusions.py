from types import SimpleNamespace

import pytest
from conftest import export, load, message
from PIL import Image

from telegram_search.indexing.chats import ChatIndexing
from telegram_search.indexing.media import MediaService
from telegram_search.indexing.service import SemanticService
from telegram_search.search.lexical import ContextService, Filters, SearchService
from telegram_search.search.media import MediaSearch
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import normalize_text
from telegram_search.storage.database import Database


@pytest.fixture
def queues(db, importer):
    semantic = SemanticService(db, importer.lifecycle_lock, start_background=False)
    media = MediaService(db, importer.lifecycle_lock, semantic, start_background=False)
    yield ChatIndexing(db, semantic, media, importer.lifecycle_lock), media
    media.shutdown()
    semantic.shutdown()


def seed(importer, root, chat_id=100):
    root.mkdir()
    Image.new("RGB", (12, 12), "red").save(root / "photo.png")
    return load(
        importer,
        export(
            root,
            [
                message(1, "поезд человека", author="human", date=1750000000),
                message(2, "рассылка робота", author="bot", date=1750086400, photo="photo.png"),
                message(3, "поезд человека", author="human", date=1750086410),
            ],
            chat_id=chat_id,
        ),
    )["chat_id"]


def exclude(controls, chat, authors):
    return controls.settings(chat, {"excluded_author_ids": authors})


def fts_integrity(db):
    with db.connect() as conn:
        conn.execute("INSERT INTO message_fts(message_fts,rank) VALUES('integrity-check',1)")


def test_exclusions_persist_and_only_invalidate_changed_days(db, importer, tmp_path, queues):
    controls, _ = queues
    chat = seed(importer, tmp_path / "a")
    other = seed(importer, tmp_path / "b", 200)
    with db.connect() as conn:
        before = [
            tuple(row)
            for row in conn.execute(
                "SELECT chat_id,utc_day,target_generation FROM index_segments "
                "ORDER BY chat_id,utc_day"
            )
        ]
        conn.execute("UPDATE chats SET text_paused=1,ocr_paused=1 WHERE id=?", (chat,))
    value = exclude(controls, chat, ["bot", "bot"])
    assert value["excluded_author_ids"] == ["bot"] and value["excluded_messages"] == 1
    assert value["semantic"]["paused"] and value["media"]["ocr_paused"]
    with db.connect() as conn:
        after = [
            tuple(row)
            for row in conn.execute(
                "SELECT chat_id,utc_day,target_generation FROM index_segments "
                "ORDER BY chat_id,utc_day"
            )
        ]
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 6
    changed = [old for old, new in zip(before, after, strict=True) if old != new]
    assert len(changed) == 1 and changed[0][0] == chat
    exclude(controls, chat, ["bot"])
    with db.connect() as conn:
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT chat_id,utc_day,target_generation FROM index_segments "
                "ORDER BY chat_id,utc_day"
            )
        ] == after
    Database(db.workspace).initialize()
    assert controls.status(chat)["excluded_author_ids"] == ["bot"]
    assert controls.status(other)["excluded_author_ids"] == []


def test_word_index_context_edits_deletion_and_rebuild_respect_exclusions(
    db, importer, tmp_path, queues
):
    controls, _ = queues
    chat = seed(importer, tmp_path / "a")
    exclude(controls, chat, ["bot"])
    search = SearchService(db)
    assert not search.search("рассылка")["results"]
    assert not search.search("рассылка", exact=True)["results"]
    hits = search.search("поезд", chunk_size=5)["results"]
    assert all(m["message_id"] != 2 for hit in hits for m in hit["messages"])
    assert {m["message_id"] for m in ContextService(db).get_context(chat, 2)} == {1, 2, 3}
    assert {a["author_id"] for a in search.authors([chat])} == {"bot", "human"}
    # New imports use the same canonical writer/triggers as Telegram updates.
    load(
        importer,
        export(
            tmp_path / "new",
            [
                message(4, "новая рассылка", author="bot", date=1750086500),
            ],
        ),
    )
    assert controls.status(chat)["excluded_messages"] == 2
    db.rebuild()
    assert not search.search("рассылка")["results"]
    with db.connect() as conn:
        conn.execute(
            "UPDATE messages SET text_normalized=? WHERE chat_id=? AND message_id=2",
            (normalize_text("другая рассылка"), chat),
        )
        conn.execute(
            "UPDATE messages SET author_id='human' WHERE chat_id=? AND message_id=4", (chat,)
        )
    assert search.search("новая")["results"][0]["message_id"] == 4
    fts_integrity(db)
    exclude(controls, chat, [])
    assert search.search("другая")["results"][0]["message_id"] == 2
    exclude(controls, chat, ["bot"])
    with db.connect() as conn:
        conn.execute("DELETE FROM messages WHERE chat_id=? AND author_id='bot'", (chat,))
    # A retained choice can be removed even when the author has no remaining messages.
    assert controls.status(chat)["excluded_author_ids"] == ["bot"]
    exclude(controls, chat, [])
    fts_integrity(db)
    with db.connect() as conn:
        conn.execute("DELETE FROM chats WHERE id=?", (chat,))
        assert conn.execute("SELECT COUNT(*) FROM index_excluded_authors").fetchone()[0] == 0
    fts_integrity(db)


@pytest.mark.parametrize(
    "value", [None, "bot", {}, [1], [True], [""], ["x" * 129], ["x\0y"], ["bot"] * 501]
)
def test_invalid_choices_are_atomic(db, importer, tmp_path, queues, value):
    controls, _ = queues
    chat = seed(importer, tmp_path / "a")
    with pytest.raises(UserError):
        controls.settings(chat, {"embedding_batch": 64, "excluded_author_ids": value})
    assert controls.status(chat)["excluded_author_ids"] == []
    with db.connect() as conn:
        assert (
            conn.execute("SELECT text_batch FROM chats WHERE id=?", (chat,)).fetchone()[0] is None
        )


def test_unknown_author_cannot_change_another_chat_or_batch(db, importer, tmp_path, queues):
    controls, _ = queues
    chat = seed(importer, tmp_path / "a")
    with pytest.raises(UserError, match="автор"):
        controls.settings(chat, {"embedding_batch": 64, "excluded_author_ids": ["unknown"]})
    with db.connect() as conn:
        assert (
            conn.execute("SELECT text_batch FROM chats WHERE id=?", (chat,)).fetchone()[0] is None
        )
    with pytest.raises(UserError, match="Диалог"):
        exclude(controls, "missing", ["bot"])


def test_ocr_shared_photo_scope_cached_results_and_resume(db, importer, tmp_path, queues):
    controls, media = queues
    chat = seed(importer, tmp_path / "a")
    other = seed(importer, tmp_path / "b", 200)
    calls = []
    media.ocr = SimpleNamespace(
        version="synthetic",
        recognize=lambda data: (
            calls.append(data)
            or {
                "text": "текст картинки",
                "confidence": 99,
            }
        ),
    )
    exclude(controls, chat, ["bot"])
    assert media.status(chat)["total_photos"] == 0
    assert media.status(other)["total_photos"] == 1
    assert media._ocr_one() and not media._ocr_one() and len(calls) == 1
    search = MediaSearch(db, media, controls.semantic, controls.lock)
    for exact in (False, True):
        assert not search.search("картинки", Filters([chat]), kind="ocr", exact=exact)[0]
        assert search.search("картинки", Filters([other]), kind="ocr", exact=exact)[0]
    exclude(controls, chat, [])
    assert media.status(chat)["ocr_ready"] == 1
    assert search.search("картинки", Filters([chat]), kind="ocr")[0]
    assert not media._ocr_one() and len(calls) == 1
    exclude(controls, chat, ["bot"])
    exclude(controls, other, ["bot"])
    assert media.status()["total_photos"] == 0
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ocr_cache").fetchone()[0] == 1


def test_inflight_ocr_cannot_publish_after_author_is_excluded(db, importer, tmp_path, queues):
    controls, media = queues
    chat = seed(importer, tmp_path / "a")

    def recognize(data):
        exclude(controls, chat, ["bot"])
        return {"text": "рассылка", "confidence": 99}

    media.ocr = SimpleNamespace(version="synthetic", recognize=recognize)
    assert media._ocr_one()
    with db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM ocr_cache").fetchone()
        assert tuple(conn.execute("SELECT state,claim_token FROM ocr_work").fetchone()) == (
            "pending",
            None,
        )
    assert not media._ocr_one()
    exclude(controls, chat, [])
    media.ocr.recognize = lambda data: {"text": "рассылка", "confidence": 99}
    assert media._ocr_one() and media.status(chat)["ocr_ready"] == 1


def test_ocr_first_prepared_while_author_excluded_can_resume_after_reinclude(
    db, importer, tmp_path, queues
):
    controls, media = queues
    chat = seed(importer, tmp_path / "a")
    exclude(controls, chat, ["bot"])
    calls = []
    media.ocr = SimpleNamespace(
        version="new-model",
        recognize=lambda data: calls.append(1) or {"text": "рассылка", "confidence": 99},
    )
    assert not media._ocr_one() and not calls
    exclude(controls, chat, [])
    assert media._ocr_one() and calls == [1]
    assert media.status(chat)["ocr_ready"] == 1


def test_chat_cascade_with_excluded_author_keeps_fts_consistent(db, importer, tmp_path, queues):
    controls, _ = queues
    chat = seed(importer, tmp_path / "a")
    exclude(controls, chat, ["bot"])
    with db.connect() as conn:
        conn.execute("DELETE FROM chats WHERE id=?", (chat,))
    fts_integrity(db)
    assert not SearchService(db).search("рассылка")["results"]
