from datetime import UTC, datetime

from conftest import export, load, message
from test_chat_indexing import controls
from test_semantic import setup_index

from telegram_search.search.chunking.noise.compile import compile_policy
from telegram_search.search.chunking.policy import DEFAULT_POLICY
from telegram_search.search.hybrid import HybridSearch
from telegram_search.storage.chunking import chat_policy, reset_for_rebuild
from telegram_search.storage.generations import invalidate_segments, utc_day


def select_policy(conn, chat, policy):
    conn.execute(
        "INSERT OR IGNORE INTO chunking_policies VALUES(?,?)", (policy.identity, policy.json)
    )
    conn.execute("UPDATE chat_chunking SET policy_id=? WHERE chat_id=?", (policy.identity, chat))
    days = {
        utc_day(row[0])
        for row in conn.execute("SELECT DISTINCT timestamp FROM messages WHERE chat_id=?", (chat,))
    }
    invalidate_segments(conn, chat, days, "synthetic_noise_policy")


def test_rebuild_and_import_keep_explicit_noise_policy_but_upgrade_legacy(db, importer, tmp_path):
    first = load(importer, export(tmp_path / "first", [message(text="бял")]))["chat_id"]
    second = load(importer, export(tmp_path / "second", [message()], 200))["chat_id"]
    policy = compile_policy(DEFAULT_POLICY, omit=("бял",), keep=("сука",))
    with db.connect() as conn:
        select_policy(conn, first, policy)
        conn.execute("UPDATE chats SET text_paused=1,text_batch=16 WHERE id=?", (first,))
        conn.execute(
            "UPDATE chat_chunking SET policy_id='telegram-windows-v1' WHERE chat_id=?", (second,)
        )
        reset_for_rebuild(conn)
        assert chat_policy(conn, first) == policy.identity
        assert chat_policy(conn, second) == DEFAULT_POLICY.identity
        reset_for_rebuild(conn, first)
        assert chat_policy(conn, first) == policy.identity
    load(importer, export(tmp_path / "extra", [message(2, "Новый день", date=1750100000)]))
    with db.connect() as conn:
        assert chat_policy(conn, first) == policy.identity
        assert conn.execute(
            "SELECT text_paused,text_batch FROM chats WHERE id=?", (first,)
        ).fetchone()[:] == (1, 16)
        assert {
            row[0]
            for row in conn.execute(
                "SELECT chunking_policy_id FROM index_work WHERE chat_id=? AND state='pending'",
                (first,),
            )
        } == {policy.identity}


def test_worker_reads_frozen_exact_policy_preserves_lexical_evidence_and_model_rebuild(
    db, importer, tmp_path
):
    chat, service, worker, old_work = setup_index(
        db, importer, tmp_path, ["Содержательная фраза", "бял", "сука"], legacy=False
    )
    policy = compile_policy(DEFAULT_POLICY, omit=("бял",), keep=("сука",))
    try:
        indexing, media = controls(db, importer, service)
        with db.connect() as conn:
            select_policy(conn, chat, policy)
            work = conn.execute(
                "SELECT id FROM index_work WHERE chat_id=? AND state='pending'", (chat,)
            ).fetchone()[0]
            media_before = [tuple(r) for r in conn.execute("SELECT * FROM media_state")]
        result = worker.run(work)
        assert result["state"] == "done" and result["skipped_messages"] == 1
        with db.connect() as conn:
            chunk = conn.execute(
                "SELECT * FROM chunks WHERE chunking_policy_id=?", (policy.identity,)
            ).fetchone()
            assert "бял" not in chunk["text"] and "сука" in chunk["text"]
            assert "бял" in chunk["lexical_text"]
            assert [tuple(r) for r in conn.execute("SELECT * FROM media_state")] == media_before
        hits = HybridSearch(db, service, importer.lifecycle_lock).search("бял", mode="words")
        assert hits["results"][0]["message_id"] == 2
        indexing.control(chat, "text", "pause")
        service.activate(service.encoder, reindex=True)
        with db.connect() as conn:
            assert chat_policy(conn, chat) == policy.identity
            assert (
                conn.execute("SELECT text_paused FROM chats WHERE id=?", (chat,)).fetchone()[0] == 1
            )
            assert {
                row[0]
                for row in conn.execute(
                    "SELECT chunking_policy_id FROM index_work WHERE chat_id=? AND state='pending'",
                    (chat,),
                )
            } == {policy.identity}
    finally:
        service.shutdown()


def test_edited_omitted_cross_day_parent_invalidates_owner_and_becomes_useful(
    db, importer, tmp_path
):
    chat, service, worker, old_work = setup_index(
        db, importer, tmp_path, ["placeholder"], legacy=False
    )
    midnight = int(datetime(2025, 6, 16, tzinfo=UTC).timestamp())
    policy = compile_policy(DEFAULT_POLICY, omit=("бял",))
    try:
        load(
            importer,
            export(
                tmp_path / "reply",
                [
                    message(1, "бял", date=midnight - 1),
                    message(2, "термин", date=midnight + 1, reply_to_message_id=1),
                ],
            ),
            target_chat_id=chat,
            policy="prefer_imported",
        )
        with db.connect() as conn:
            select_policy(conn, chat, policy)
            works = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM index_work WHERE state='pending' ORDER BY utc_day"
                )
            ]
        for work in works:
            assert worker.run(work)["state"] == "done"
        with db.connect() as conn:
            owner = conn.execute("SELECT * FROM chunks WHERE utc_day='2025-06-16'").fetchone()
            assert "бял" not in owner["text"]
            assert (
                conn.execute(
                    "SELECT message_id FROM chunk_context_dependencies WHERE chunk_id=?",
                    (owner["id"],),
                ).fetchone()[0]
                == 1
            )
            generation = conn.execute(
                "SELECT target_generation FROM index_segments WHERE utc_day='2025-06-16'"
            ).fetchone()[0]
        load(
            importer,
            export(tmp_path / "edited", [message(1, "Полезная фраза", date=midnight - 1)]),
            target_chat_id=chat,
            policy="prefer_imported",
        )
        with db.connect() as conn:
            assert (
                conn.execute(
                    "SELECT target_generation FROM index_segments WHERE utc_day='2025-06-16'"
                ).fetchone()[0]
                > generation
            )
            assert chat_policy(conn, chat) == policy.identity
            works = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM index_work WHERE state='pending' ORDER BY utc_day"
                )
            ]
        for work in works:
            assert worker.run(work)["state"] == "done"
        with db.connect() as conn:
            assert (
                "Полезная фраза"
                in conn.execute("SELECT text FROM chunks WHERE utc_day='2025-06-16'").fetchone()[0]
            )
            owner_work = conn.execute(
                "SELECT source_messages FROM index_work WHERE utc_day='2025-06-16' AND state='done'"
            ).fetchone()
            assert owner_work[0] == 1
    finally:
        service.shutdown()
