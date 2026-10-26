from pathlib import Path

from conftest import export, load, message

from telegram_search.ingestion.importer import ImportService
from telegram_search.search.chunking.policy import DEFAULT_POLICY
from telegram_search.storage.database import SCHEMA_VERSION, Database, execute_sql


def test_noise_migrations_only_add_analysis_metadata_and_preserve_existing_work(tmp_path):
    database = Database(tmp_path / "workspace")
    database.path.parent.mkdir(parents=True)
    storage = Path(__file__).parents[1] / "src/telegram_search/storage"
    with database.connect() as conn:
        execute_sql(conn, (storage / "schema.sql").read_text())
        for migration in sorted((storage / "migrations").glob("*.sql")):
            if int(migration.name.split("_")[0]) <= 17:
                execute_sql(conn, migration.read_text())
    importer = ImportService(database)
    try:
        chat = load(importer, export(tmp_path / "a", [message(), message(2, "сука")]))["chat_id"]
    finally:
        importer.shutdown()
    with database.connect() as conn:
        conn.execute("UPDATE chats SET text_paused=1,text_batch=16 WHERE id=?", (chat,))
        before = {
            name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name}")]
            for name in (
                "messages",
                "chats",
                "index_work",
                "index_segments",
                "chat_chunking",
                "chunking_policies",
                "search_tabs",
            )
        }
    database.initialize()
    with database.connect() as conn:
        assert (
            conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
            == SCHEMA_VERSION
        )
        for name, rows in before.items():
            assert rows == [tuple(row) for row in conn.execute(f"SELECT * FROM {name}")]
        assert not conn.execute("SELECT 1 FROM noise_analyses").fetchone()
        assert (
            conn.execute("SELECT policy_id FROM chat_chunking WHERE chat_id=?", (chat,)).fetchone()[
                0
            ]
            == DEFAULT_POLICY.identity
        )
