"""Regression: chat/date statistics must not restart MATCH once per source row."""

from contextlib import contextmanager

import pytest
from conftest import export, load, message

from telegram_search.search.lexical import Filters, SearchService


def seed_archive(db, importer, tmp_path):
    chat = load(
        importer, export(tmp_path / "source", [message(text="общий термин", date=1)], chat_id=200)
    )["chat_id"]
    with db.connect() as conn:
        conn.execute(
            "WITH RECURSIVE seq(n) AS (SELECT 2 UNION ALL SELECT n+1 FROM seq WHERE n<20000) "
            "INSERT INTO messages(chat_id,message_id,timestamp,author_id,author,kind,text,"
            "text_normalized,has_photo,content_hash,raw_json) "
            "SELECT m.chat_id,seq.n,seq.n,m.author_id,m.author,m.kind,m.text,"
            "m.text_normalized,0,'synthetic','{}' FROM messages m CROSS JOIN seq "
            "WHERE m.chat_id=? AND m.message_id=1",
            (chat,),
        )
        conn.execute("ANALYZE")
        # An imported chat can outgrow statistics collected before its history
        # was loaded. Reproduce that misleadingly selective chronology index.
        conn.execute("UPDATE sqlite_stat1 SET stat='20000 1 1 1' WHERE idx='messages_chronology'")
        conn.execute("ANALYZE sqlite_schema")
    return chat


def guard_ranked_fts(db, monkeypatch):
    connect = db.connect
    measurements = []

    @contextmanager
    def guarded():
        with connect() as conn:
            steps = [0]

            def progress():
                steps[0] += 100
                return int(steps[0] > 10000)

            def trace(sql):
                # FTS emits internal statements prefixed with '--'. They must
                # not disable the budget on the still-running outer statement.
                if sql.startswith("--"):
                    return
                if sql.startswith("SELECT m.*,") and "FROM message_fts" in sql:
                    steps[0] = 0
                    measurements.append(steps)
                    conn.set_progress_handler(progress, 100)
                else:
                    conn.set_progress_handler(None, 0)

            conn.set_trace_callback(trace)
            yield conn

    monkeypatch.setattr(db, "connect", guarded)
    return measurements


def test_filtered_keyword_query_finishes_with_bounded_sqlite_work(
    db, importer, tmp_path, monkeypatch
):
    chat = seed_archive(db, importer, tmp_path)
    measurements = guard_ranked_fts(db, monkeypatch)
    assert not SearchService(db).search(
        "термин несуществующий", Filters(chat_ids=[chat], date_from=500, date_to=1500)
    )["results"]
    assert measurements and max(value[0] for value in measurements) <= 10000


def test_filtered_hybrid_query_finishes_its_keyword_branch(db, importer, tmp_path, monkeypatch):
    pytest.importorskip("lancedb")
    from test_semantic import setup_index

    from telegram_search.search.hybrid import HybridSearch

    chat, semantic, worker, work = setup_index(db, importer, tmp_path / "index", ["общий термин"])
    assert worker.run(work)["state"] == "done"
    chat = seed_archive(db, importer, tmp_path)
    measurements = guard_ranked_fts(db, monkeypatch)
    result = HybridSearch(db, semantic, importer.lifecycle_lock).search(
        "термин несуществующий",
        Filters(chat_ids=[chat], date_from=500, date_to=1500),
        mode="hybrid",
    )
    assert result["effective_mode"] == "hybrid"
    assert measurements and max(value[0] for value in measurements) <= 10000
