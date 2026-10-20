from conftest import export, load, message

from telegram_search.search.chunking.policy import DEFAULT_POLICY, LEGACY_POLICY_ID
from telegram_search.storage.chunking import chat_policy


def test_new_imports_freeze_advanced_policy_without_a_settings_request(db, importer, tmp_path):
    chat = load(importer, export(tmp_path / "new", [message(), message(2, "сука")]))["chat_id"]
    with db.connect() as conn:
        assert chat_policy(conn, chat) == DEFAULT_POLICY.identity
        assert (
            conn.execute(
                "SELECT policy_json FROM chunking_policies WHERE id=?", (DEFAULT_POLICY.identity,)
            ).fetchone()[0]
            == DEFAULT_POLICY.json
        )
        assert {
            row[0]
            for row in conn.execute(
                "SELECT chunking_policy_id FROM index_segments WHERE chat_id=?", (chat,)
            )
        } == {DEFAULT_POLICY.identity}
        assert {
            row[0]
            for row in conn.execute(
                "SELECT chunking_policy_id FROM index_work WHERE chat_id=?", (chat,)
            )
        } == {DEFAULT_POLICY.identity}


def test_legacy_work_without_a_selection_keeps_policy_when_importing_new_days(
    db, importer, tmp_path
):
    original = message(date=1750000000)
    chat = load(importer, export(tmp_path / "old", [original]))["chat_id"]
    # Recreate pre-default work from a chat which had never used the selector.
    with db.connect() as conn:
        conn.execute("DELETE FROM chat_chunking WHERE chat_id=?", (chat,))
        for table in ("index_segments", "index_work"):
            conn.execute(
                f"UPDATE {table} SET chunking_policy_id=? WHERE chat_id=?", (LEGACY_POLICY_ID, chat)
            )
        previous = [tuple(row) for row in conn.execute("SELECT * FROM index_work")]
        assert chat_policy(conn, chat) == LEGACY_POLICY_ID
        assert previous == [tuple(row) for row in conn.execute("SELECT * FROM index_work")]
    load(importer, export(tmp_path / "old", [original, message(2, date=1750000000 + 86400)]))
    with db.connect() as conn:
        assert chat_policy(conn, chat) == LEGACY_POLICY_ID
        current = [tuple(row) for row in conn.execute("SELECT * FROM index_work")]
        assert previous[0] in current
        assert {row[0] for row in conn.execute("SELECT chunking_policy_id FROM index_work")} == {
            LEGACY_POLICY_ID
        }


def test_empty_chat_default_does_not_mutate_legacy_checkpoints(db, importer, tmp_path):
    chat = load(importer, export(tmp_path / "empty", []))["chat_id"]
    with db.connect() as conn:
        assert chat_policy(conn, chat) == DEFAULT_POLICY.identity
        assert conn.execute("SELECT COUNT(*) FROM index_work").fetchone()[0] == 0
