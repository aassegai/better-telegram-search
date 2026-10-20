import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import export, load, message

from telegram_search.search.lexical import ContextService, Filters, SearchService
from telegram_search.storage.database import SCHEMA_VERSION, Database, execute_sql
from telegram_search.telegram_sync.adapter import normalize_message


def test_v12_migration_recovers_sources_without_reimport_or_reindex(tmp_path):
    db = Database(tmp_path / "workspace")
    db.path.parent.mkdir(parents=True)
    storage = Path(__file__).resolve().parents[1] / "src/telegram_search/storage"
    with db.connect() as conn:
        execute_sql(conn, (storage / "schema.sql").read_text())
        for migration in sorted((storage / "migrations").glob("*.sql")):
            if int(migration.name.split("_")[0]) <= 12:
                execute_sql(conn, migration.read_text())
        conn.execute(
            "INSERT INTO chats(id,scope,external_id,name,kind,created_at) "
            "VALUES('test','default','100','Test','personal_chat',0)"
        )
        for mid, source in enumerate(["Источник", None, "", ["invalid"]], 1):
            conn.execute(
                "INSERT INTO messages(chat_id,message_id,timestamp,author,kind,text,"
                "text_normalized,content_hash,raw_json) VALUES('test',?,100,'Author','message',"
                "'велосипед','велосипед','synthetic',?)",
                (mid, json.dumps({"forwarded_from": source})),
            )
        conn.execute(
            "INSERT INTO index_segments(chat_id,utc_day,target_generation,"
            "lexical_generation,dense_generation,chunk_generation) "
            "VALUES('test','1970-01-01',7,7,7,7)"
        )
        generations = list(conn.execute("SELECT * FROM index_segments"))
    db.initialize()
    db.initialize()
    with db.connect() as conn:
        assert (
            conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == SCHEMA_VERSION
        )
        assert [
            row[0]
            for row in conn.execute("SELECT forwarded_from FROM messages ORDER BY message_id")
        ] == ["Источник", None, None, None]
        assert list(conn.execute("SELECT * FROM index_segments")) == generations
    assert (
        len(SearchService(db).search("велосипед", Filters(forwarded_from=["Источник"]))["results"])
        == 1
    )


def test_sources_filter_anchor_and_context_with_other_filters(db, importer, tmp_path):
    chat = load(
        importer,
        export(
            tmp_path / "a",
            [
                message(1, "велосипед", date=100, author="alice", forwarded_from="Канал A"),
                message(2, "велосипед", date=101, author="bob", forwarded_from="Канал B"),
                message(3, "велосипед", date=102, author="alice"),
                message(4, "велосипед", date=103, author="alice", forwarded_from="Канал B"),
            ],
        ),
    )["chat_id"]
    search = SearchService(db)
    filters = Filters(
        chat_ids=[chat], author_ids=["alice"], date_from=101, forwarded_from=["Канал A", "Канал B"]
    )
    hits = search.search("велосипед", filters)["results"]
    assert [hit["message_id"] for hit in hits] == [4]
    context = ContextService(db).get_context(chat, 4, 3, 0, filters)
    assert [item["matches_filters"] for item in context] == [False, False, False, True]
    assert not search.search("велосипед", Filters(forwarded_from=["' OR 1=1 --"]))["results"]
    assert len(search.search("велосипед", chunk_size=1)["results"]) == 4


def test_source_picker_unicode_paging_chat_and_exclusion_scopes(db, importer, tmp_path):
    first = load(
        importer,
        export(
            tmp_path / "a",
            [
                message(1, forwarded_from="ЁЖИК"),
                message(2, forwarded_from="ЁЖИК"),
                message(3, forwarded_from="B %_ name"),
                message(4, author="bot", forwarded_from="C hidden"),
                message(5, forwarded_from="D deleted"),
                message(6),
            ],
        ),
    )["chat_id"]
    other = load(importer, export(tmp_path / "b", [message(forwarded_from="Other")], chat_id=200))[
        "chat_id"
    ]
    with db.connect() as conn:
        conn.execute("INSERT INTO index_excluded_authors VALUES(?, 'bot')", (first,))
        conn.execute(
            "UPDATE messages SET remote_deleted=1 WHERE chat_id=? AND message_id=5", (first,)
        )
    search = SearchService(db)
    assert search.forwarded_sources([first], "ежик")["sources"] == [{"name": "ЁЖИК", "messages": 2}]
    assert search.forwarded_sources([first], "%_")["sources"] == [
        {"name": "B %_ name", "messages": 1}
    ]
    assert search.forwarded_sources([other])["sources"] == [{"name": "Other", "messages": 1}]
    page = search.forwarded_sources([first], limit=1, exclude_deleted=True)
    assert page["sources"] == [{"name": "B %_ name", "messages": 1}]
    assert page["has_more"] and page["next_offset"] == 1
    page = search.forwarded_sources([first], offset=1, limit=1, exclude_deleted=True)
    assert page["sources"] == [{"name": "ЁЖИК", "messages": 2}]
    assert not page["has_more"] and page["next_offset"] is None


def test_source_column_follows_import_updates(db, importer, tmp_path):
    path = export(tmp_path / "a", [message(text="старое", forwarded_from="First")])
    load(importer, path)
    export(
        tmp_path / "a",
        [message(text="новое", forwarded_from="Second", edited_unixtime="1750000010")],
    )
    load(importer, path, policy="prefer_imported")
    assert SearchService(db).forwarded_sources()["sources"] == [{"name": "Second", "messages": 1}]


@pytest.mark.parametrize(
    ("old_source", "new_source", "policy", "origin", "identity", "expected"),
    [
        (None, "Source", "preserve", "desktop", None, "Source"),
        ("First", "Second", "preserve", "desktop", None, "First"),
        ("First", "Second", "prefer_imported", "desktop", None, "Second"),
        ("First", "Second", "preserve", "telegram", "user42", "Second"),
        ("Known name", "user42", "preserve", "telegram", "user42", "Known name"),
        ("Known name", None, "prefer_imported", "desktop", None, "Known name"),
    ],
)
def test_unchanged_body_enriches_source_without_rebuilding_and_invalidates_cache(
    db, importer, tmp_path, old_source, new_source, policy, origin, identity, expected
):
    original = message(forwarded_from=old_source, text=[{"type": "bold", "text": "велосипед"}])
    chat = load(importer, export(tmp_path / "same", [original]))["chat_id"]
    with db.connect() as conn:
        before = dict(conn.execute("SELECT * FROM messages").fetchone())
        epoch = conn.execute("SELECT generation FROM search_archive_epoch").fetchone()[0]
        generations = list(conn.execute("SELECT * FROM index_segments"))
    incoming = {**original, "forwarded_from": new_source, "forwarded_from_id": identity}
    result = importer.writer.apply(
        {"chat_id": chat, "policy": policy, "source": origin, "record_counters": False},
        [(incoming, [])],
    )
    assert result["unchanged"] == 1 and result["updated"] == 0
    with db.connect() as conn:
        after = dict(conn.execute("SELECT * FROM messages").fetchone())
        assert after["forwarded_from"] == expected
        assert after["content_hash"] == before["content_hash"]
        assert json.loads(after["raw_json"])["text"] == original["text"]
        assert list(conn.execute("SELECT * FROM index_segments")) == generations
        assert (
            conn.execute("SELECT generation FROM search_archive_epoch").fetchone()[0] > epoch
        ) == (old_source != expected)


@pytest.mark.parametrize("kind", ["hidden", "user", "channel", "id", "unknown"])
def test_live_forward_sources_use_only_cached_metadata(kind):
    peer = type("PeerUser", (), {"user_id": 1})()
    entity = SimpleNamespace(id=42, title="Канал", first_name="Автор", last_name=None)
    forward = SimpleNamespace(sender=None, chat=None)
    header = SimpleNamespace(from_name=None, from_id=None)
    expected = None
    if kind == "hidden":
        header.from_name = expected = "Скрытый автор"
    elif kind == "user":
        forward.sender = entity
        entity.title = None
        expected = "Автор"
    elif kind == "channel":
        forward.chat = entity
        expected = "Канал"
    elif kind == "id":
        header.from_id = peer
        expected = "user1"
    record = normalize_message(
        SimpleNamespace(
            id=1,
            peer_id=peer,
            from_id=peer,
            date=datetime(2026, 1, 1, tzinfo=UTC),
            fwd_from=header,
            forward=forward,
        )
    )
    assert record.data.get("forwarded_from") == expected
