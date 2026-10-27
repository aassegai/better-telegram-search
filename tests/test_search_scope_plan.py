"""Canonical scope must not scan a whole chat for every indexed chunk."""

import pytest
from conftest import export, load, message

from telegram_search.search.hybrid import HybridSearch
from telegram_search.search.lexical import Filters


def seed(db, importer, tmp_path):
    chat = load(importer, export(tmp_path / "source", [message(1, "seed", date=1)]))["chat_id"]
    with db.connect() as conn:
        conn.execute(
            "WITH RECURSIVE seq(n) AS (SELECT 2 UNION ALL SELECT n+1 FROM seq WHERE n<5000) "
            "INSERT INTO messages(chat_id,message_id,timestamp,author_id,author,kind,text,"
            "text_normalized,has_photo,content_hash,raw_json) "
            "SELECT m.chat_id,seq.n,seq.n,m.author_id,m.author,m.kind,m.text,"
            "m.text_normalized,0,'synthetic','{}' FROM messages m CROSS JOIN seq "
            "WHERE m.chat_id=? AND m.message_id=1",
            (chat,),
        )
        conn.execute("INSERT INTO embedding_spaces VALUES('test','small','{}',4,0)")
        conn.execute(
            "UPDATE index_segments SET chunk_generation=target_generation,"
            "dense_generation=target_generation,embedding_space_id='test'"
        )
        conn.execute(
            "INSERT INTO chunks(id,chat_id,utc_day,generation,embedding_space_id,ordinal,"
            "text,text_normalized,tokens) SELECT 'chunk-'||message_id,chat_id,'1970-01-01',"
            "1,'test',message_id,text,text_normalized,1 FROM messages WHERE chat_id=?",
            (chat,),
        )
        conn.execute(
            "INSERT INTO chunk_embedding_parts SELECT id,0,chat_id,ordinal,0,4,'core' FROM chunks"
        )
        conn.execute("ANALYZE")
        conn.execute("UPDATE sqlite_stat1 SET stat='5000 1 1 1' WHERE idx='messages_chronology'")
        conn.execute("ANALYZE sqlite_schema")
    return chat


def selected(conn, filters, *, scope_first, budget=None):
    predicate, params = HybridSearch.eligible_sql(filters, "test", scope_first=scope_first)
    if budget is not None:
        conn.set_progress_handler(lambda: int(budget.charge()), 100)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT c.id FROM chunks c JOIN index_segments s "
                "ON s.chat_id=c.chat_id AND s.utc_day=c.utc_day WHERE " + predicate,
                params,
            )
        }
    finally:
        conn.set_progress_handler(None, 0)


class Steps:
    def __init__(self, maximum):
        self.used, self.maximum = 0, maximum

    def charge(self):
        self.used += 100
        return self.used > self.maximum


def test_scope_starts_with_filtered_messages_under_stale_statistics(db, importer, tmp_path):
    chat = seed(db, importer, tmp_path)
    with db.connect() as conn:
        budget = Steps(50_000)
        found = selected(
            conn,
            Filters(chat_ids=[chat], date_from=100, date_to=120),
            scope_first=True,
            budget=budget,
        )
        assert found == {"chunk-" + str(n) for n in range(100, 120)}
        assert budget.used <= 50_000


@pytest.mark.parametrize("exclude_deleted", [False, True])
def test_scope_and_point_checks_agree_for_canonical_witnesses(
    db, importer, tmp_path, exclude_deleted
):
    chat = seed(db, importer, tmp_path)
    with db.connect() as conn:
        conn.execute("UPDATE messages SET author_id='excluded' WHERE message_id=103")
        conn.execute("INSERT INTO index_excluded_authors VALUES(?, 'excluded')", (chat,))
        conn.execute("UPDATE messages SET remote_deleted=1 WHERE message_id=104")
        conn.execute(
            "UPDATE chunk_embedding_parts SET role='borrowed_context' WHERE message_id=105"
        )
        conn.execute("UPDATE chunks SET generation=2 WHERE ordinal=106")
        conn.execute(
            "UPDATE messages SET raw_json=json_set(raw_json,'$.forwarded_from','origin') "
            "WHERE message_id BETWEEN 100 AND 110"
        )
        filters = Filters(
            chat_ids=[chat],
            date_from=100,
            date_to=110,
            forwarded_from=["origin"],
            exclude_deleted=exclude_deleted,
        )
        bounded = selected(conn, filters, scope_first=True)
        assert bounded == selected(conn, filters, scope_first=False)
        expected = set(range(100, 110)) - {103, 105, 106} - ({104} if exclude_deleted else set())
        assert bounded == {"chunk-" + str(n) for n in expected}
        assert not selected(conn, Filters(scope_mode="selected"), scope_first=True)
        # A witness date is authoritative even when its chunk's day differs.
        conn.execute("UPDATE messages SET timestamp=90000 WHERE message_id=100")
        assert "chunk-100" in selected(conn, Filters(date_from=89999), scope_first=True)


def test_global_media_coverage_does_not_walk_text_only_history(db, importer, tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from telegram_search.search.media import MediaSearch

    chat = seed(db, importer, tmp_path)
    with db.connect() as conn:
        source = conn.execute("SELECT id FROM source_roots WHERE chat_id=?", (chat,)).fetchone()[0]
        conn.execute("INSERT INTO media_blobs VALUES('synthetic-sha',1)")
        conn.execute(
            "INSERT INTO media_refs(chat_id,message_id,source_root_id,relative_path,"
            "kind,sha256,status) "
            "VALUES(?,1,?,'photo.png','photo','synthetic-sha','ready')",
            (chat, source),
        )
    connect, budget = db.connect, Steps(10_000)

    @contextmanager
    def guarded():
        with connect() as conn:
            conn.set_progress_handler(lambda: int(budget.charge()), 100)
            yield conn

    monkeypatch.setattr(db, "connect", guarded)
    media = SimpleNamespace(ocr=None, clip=None)
    semantic = SimpleNamespace(encoder=None)
    result = MediaSearch(db, media, semantic, importer.lifecycle_lock).coverage(Filters())
    assert result["total_photos"] == 1 and result["ocr_dense_ready"] == 0
    assert budget.used <= 10_000
