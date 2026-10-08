from datetime import UTC, datetime

import pytest
from conftest import export, load, message
from test_chat_indexing import controls
from test_semantic import setup_index

from telegram_search.search.hybrid import HybridSearch
from telegram_search.search.lexical import Filters
from telegram_search.storage.generations import invalidate_segments, utc_day


def enable_episodes(db, importer, service, chat):
    indexing, media = controls(db, importer, service)
    indexing.settings(chat, {"chunking_profile": "episodes"})
    with db.connect() as conn:
        works = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM index_work WHERE state='pending' ORDER BY utc_day"
            )
        ]
    return indexing, media, works


def test_explicit_policy_rebuild_preserves_pauses_model_space_and_old_checkpoints(
    db, importer, tmp_path
):
    chat, service, worker, old = setup_index(db, importer, tmp_path)
    indexing, media = controls(db, importer, service)
    indexing.control(chat, "text", "pause")
    before = indexing.status(chat)
    after = indexing.settings(chat, {"chunking_profile": "episodes"})
    assert before["chunking"]["profile"] == "legacy"
    assert after["chunking"]["profile"] == "episodes" and after["semantic"]["paused"]
    assert after["semantic"]["active_space_id"] == before["semantic"]["active_space_id"]
    with db.connect() as conn:
        assert (
            conn.execute("SELECT state FROM index_work WHERE id=?", (old,)).fetchone()[0]
            == "superseded"
        )
        work = conn.execute("SELECT * FROM index_work WHERE state='pending'").fetchone()
        assert (
            work["chunks_done"] == 0
            and work["chunking_policy_id"] == after["chunking"]["policy_id"]
        )
    indexing.control(chat, "text", "resume")
    assert worker.run(work["id"])["state"] == "done"
    with db.connect() as conn:
        ids = [row[0] for row in conn.execute("SELECT id FROM chunks")]
    indexing.settings(chat, {"embedding_batch": 16})
    with db.connect() as conn:
        assert ids == [row[0] for row in conn.execute("SELECT id FROM chunks")]
    for value in ({}, [], None, "invalid"):
        with pytest.raises(Exception, match="политика чанкинга"):
            indexing.settings(chat, {"chunking_profile": value})


def test_filler_only_day_is_successful_and_hybrid_keeps_original_word_evidence(
    db, importer, tmp_path
):
    chat, service, worker, old = setup_index(db, importer, tmp_path, ["сука", "блядь"])
    indexing, media, works = enable_episodes(db, importer, service, chat)
    work = worker.run(works[0])
    assert work["state"] == "done" and work["chunks_total"] == 0 and work["skipped_messages"] == 2
    result = HybridSearch(db, service, importer.lifecycle_lock).search("сука", mode="hybrid")
    assert result["results"][0]["message_id"] == 1
    assert result["results"][0]["matched_by"] == ["words"]
    assert indexing.status(chat)["chunking"]["skipped_messages"] == 2


def test_cross_day_parent_changes_invalidate_dependent_owner_and_filters_use_owned_reply(
    db, importer, tmp_path
):
    chat, service, worker, old = setup_index(db, importer, tmp_path, ["placeholder"])
    midnight = int(datetime(2025, 6, 16, tzinfo=UTC).timestamp())
    load(
        importer,
        export(
            tmp_path / "replies",
            [
                message(1, "Встреча завтра состоится?", date=midnight - 1, author="user1"),
                message(2, "да", date=midnight + 1, author="user2", reply_to_message_id=1),
            ],
        ),
        target_chat_id=chat,
        policy="prefer_imported",
    )
    indexing, media, works = enable_episodes(db, importer, service, chat)
    for work in works:
        assert worker.run(work)["state"] == "done"
    with db.connect() as conn:
        owner = conn.execute("SELECT * FROM chunks WHERE utc_day='2025-06-16'").fetchone()
        assert owner and "Встреча завтра состоится?" in owner["text"]
        assert [
            row[0]
            for row in conn.execute(
                "SELECT message_id FROM chunk_parts WHERE chunk_id=?", (owner["id"],)
            )
        ] == [2]
        assert (
            conn.execute(
                "SELECT count(*) FROM chunk_context_dependencies WHERE chunk_id=?", (owner["id"],)
            ).fetchone()[0]
            == 1
        )
        predicate, params = HybridSearch.eligible_sql(
            Filters(author_ids=["user1"], date_from=midnight), service.encoder.space_id
        )
        assert not conn.execute(
            "SELECT 1 FROM chunks c JOIN index_segments s "
            "ON s.chat_id=c.chat_id AND s.utc_day=c.utc_day WHERE " + predicate,
            params,
        ).fetchone()
        generation = conn.execute(
            "SELECT target_generation FROM index_segments WHERE chat_id=? AND utc_day='2025-06-16'",
            (chat,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE messages SET content_hash='changed',text='Встреча отменена',"
            "text_normalized='встреча отменена' WHERE chat_id=? AND message_id=1",
            (chat,),
        )
        invalidate_segments(conn, chat, {utc_day(midnight - 1)}, "test_edit")
        assert (
            conn.execute(
                "SELECT target_generation FROM index_segments "
                "WHERE chat_id=? AND utc_day='2025-06-16'",
                (chat,),
            ).fetchone()[0]
            == generation + 1
        )


def test_independent_adjacent_word_hits_are_not_deduplicated_by_preview(db, importer, tmp_path):
    chat, service, worker, old = setup_index(
        db, importer, tmp_path, ["Встреча первая", "Встреча вторая"]
    )
    indexing, media, works = enable_episodes(db, importer, service, chat)
    assert worker.run(works[0])["state"] == "done"
    for mode in ("words", "hybrid"):
        hits = HybridSearch(db, service, importer.lifecycle_lock).search(
            "Встреча", mode=mode, chunk_size=1
        )
        assert {hit["message_id"] for hit in hits["results"]} == {1, 2}


def test_missing_parent_and_later_closer_neighbor_rebuild_zero_or_ready_days(
    db, importer, tmp_path
):
    chat, service, worker, old = setup_index(db, importer, tmp_path, ["placeholder"])
    midnight = int(datetime(2025, 6, 16, tzinfo=UTC).timestamp())
    load(
        importer,
        export(
            tmp_path / "child",
            [
                message(1, "да", date=midnight + 1, reply_to_message_id=99),
            ],
        ),
        target_chat_id=chat,
        policy="prefer_imported",
    )
    indexing, media, works = enable_episodes(db, importer, service, chat)
    for work in works:
        assert worker.run(work)["state"] == "done"
    with db.connect() as conn:
        before = conn.execute(
            "SELECT target_generation FROM index_segments WHERE utc_day='2025-06-16'"
        ).fetchone()[0]
        assert (
            conn.execute(
                "SELECT count(*) FROM segment_context_dependencies WHERE message_id=99"
            ).fetchone()[0]
            == 1
        )
    load(
        importer,
        export(tmp_path / "parent", [message(99, "Встреча завтра состоится?", date=midnight - 50)]),
    )
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT target_generation FROM index_segments WHERE utc_day='2025-06-16'"
            ).fetchone()[0]
            > before
        )
        works = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM index_work WHERE state='pending' ORDER BY utc_day"
            )
        ]
    for work in works:
        assert worker.run(work)["state"] == "done"
    # Make the reply implicit, selecting the preceding cross-day message.
    load(
        importer,
        export(tmp_path / "implicit", [message(1, "да", date=midnight + 1)]),
        policy="prefer_imported",
    )
    with db.connect() as conn:
        works = [row[0] for row in conn.execute("SELECT id FROM index_work WHERE state='pending'")]
    for work in works:
        assert worker.run(work)["state"] == "done"
    with db.connect() as conn:
        before = conn.execute(
            "SELECT target_generation FROM index_segments WHERE utc_day='2025-06-16'"
        ).fetchone()[0]
    load(
        importer,
        export(tmp_path / "nearer", [message(100, "Новый соседний вопрос?", date=midnight - 1)]),
    )
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT target_generation FROM index_segments WHERE utc_day='2025-06-16'"
            ).fetchone()[0]
            > before
        )


def test_unsupported_attachment_caption_is_protected_from_noise_filter(db, importer, tmp_path):
    chat, service, worker, old = setup_index(db, importer, tmp_path, ["placeholder"])
    load(
        importer,
        export(
            tmp_path / "document",
            [message(1, "сука", media_type="document", mime_type="application/pdf")],
        ),
        policy="prefer_imported",
    )
    indexing, media, works = enable_episodes(db, importer, service, chat)
    for work in works:
        assert worker.run(work)["state"] == "done"
    with db.connect() as conn:
        assert (
            conn.execute("SELECT text FROM chunks WHERE chat_id=?", (chat,))
            .fetchone()[0]
            .endswith("сука")
        )
