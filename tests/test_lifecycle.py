from pathlib import Path

import pytest
from conftest import export, load, message
from PIL import Image

from telegram_search.search.lexical import ContextService
from telegram_search.shared.errors import UserError
from telegram_search.storage.database import SCHEMA_VERSION, Database, execute_sql
from telegram_search.storage.generations import utc_day


def preview(importer, path, **kwargs):
    pid = importer.previews.create(str(path), **kwargs)
    report = importer.previews.run(pid)
    assert report["state"] == "ready", report["error"]
    return report


def apply(importer, report):
    job_id = importer.previews.apply(report["id"])
    result = importer.futures[job_id].result(timeout=5)
    assert result["state"] == "completed", result["error"]
    return result


def test_preflight_is_read_only_then_frozen_report_applies(importer, db, tmp_path):
    report = preview(importer, export(tmp_path / "a", [message(i) for i in range(1, 6)]))
    assert report["processed"] == report["added"] == 5
    with db.connect() as conn:
        for table in ("chats", "messages", "source_roots", "imports", "index_work"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    result = apply(importer, report)
    assert result["added"] == result["processed"] == 5
    again = preview(importer, tmp_path / "a/result.json")
    assert again["unchanged"] == 5
    assert apply(importer, again)["unchanged"] == 5
    with pytest.raises(UserError, match="отчёта"):
        importer.previews.apply(report["id"])


def test_preflight_reports_all_classes_and_older_always_conflicts(importer, tmp_path):
    load(
        importer,
        export(
            tmp_path / "a",
            [
                message(1),
                message(2, edited_unixtime="1750000010"),
                message(3, edited_unixtime="1750000030"),
                message(4),
            ],
        ),
    )
    report = preview(
        importer,
        export(
            tmp_path / "b",
            [
                message(1),
                message(2, "новая", edited_unixtime="1750000020"),
                message(3, "старая", edited_unixtime="1750000020"),
                message(4, "нет даты"),
                message(5),
            ],
        ),
    )
    assert [report[key] for key in ("added", "unchanged", "updated", "conflicts")] == [1, 1, 1, 2]
    result = apply(importer, report)
    for key in ("added", "unchanged", "updated", "conflicts"):
        assert result[key] == report[key]
    authoritative = preview(importer, tmp_path / "b/result.json", policy="prefer_imported")
    assert authoritative["conflicts"] == 1
    assert authoritative["updated"] == 1


def test_stale_target_and_changed_json_cannot_apply(importer, tmp_path):
    path = export(tmp_path / "a", [message()])
    first = preview(importer, path)
    load(importer, path)
    with pytest.raises(UserError, match="Диалог изменился"):
        importer.previews.apply(first["id"])
    second = preview(importer, path)
    load(importer, export(tmp_path / "b", [message(2)]))
    with pytest.raises(UserError, match="Диалог изменился"):
        importer.previews.apply(second["id"])
    third = preview(importer, path)
    export(path.parent, [message(3)])
    with pytest.raises(UserError, match="JSON изменился"):
        importer.previews.apply(third["id"])


def test_preview_pause_resume_and_duplicate_ids(importer, db, tmp_path, monkeypatch):
    import telegram_search.ingestion.importer as module

    path = export(tmp_path / "a", [message(i) for i in range(1, 5)])
    pid = importer.previews.create(str(path))
    original = module.inspect_media
    paused = False

    def inspect(root, item):
        nonlocal paused
        if not paused:
            paused = True
            importer.previews.control(pid, "pause")
        return original(root, item)

    monkeypatch.setattr(module, "inspect_media", inspect)
    assert importer.previews.run(pid)["processed"] == 0
    importer.previews.control(pid, "resume")
    report = importer.futures[pid].result(timeout=5)
    assert report["state"] == "ready" and report["added"] == 4
    duplicate = importer.previews.create(str(export(tmp_path / "b", [message(), message()])))
    assert importer.previews.run(duplicate)["state"] == "failed"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_generations_only_change_affected_days_and_are_atomic(importer, db, tmp_path, monkeypatch):
    first = load(importer, export(tmp_path / "a", [message(1), message(2, date=1750086400)]))
    with db.connect() as conn:
        baseline = {
            row["utc_day"]: row["target_generation"]
            for row in conn.execute("SELECT * FROM index_segments")
        }
    load(importer, tmp_path / "a/result.json")
    update = export(tmp_path / "b", [message(1, "новая", edited_unixtime="1750000020")])
    load(importer, update)
    with db.connect() as conn:
        actual = {
            row["utc_day"]: row["target_generation"]
            for row in conn.execute("SELECT * FROM index_segments")
        }
        assert actual[utc_day(1750000000)] == baseline[utc_day(1750000000)] + 1
        assert actual[utc_day(1750086400)] == baseline[utc_day(1750086400)]
        assert (
            conn.execute("SELECT COUNT(*) FROM index_work WHERE state='superseded'").fetchone()[0]
            == 1
        )
        revision = conn.execute("SELECT revision FROM chats").fetchone()[0]
    # A failure writing the outbox must roll back the message and checkpoint as well.
    import telegram_search.ingestion.importer as module

    def fail(*args):
        raise RuntimeError("synthetic outbox failure")

    monkeypatch.setattr(module, "invalidate_segments", fail)
    bad = importer.run(importer.prepare(str(export(tmp_path / "c", [message(3)]))))
    assert bad["state"] == "failed" and bad["processed"] == 0
    with db.connect() as conn:
        assert conn.execute("SELECT revision FROM chats").fetchone()[0] == revision
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    assert ContextService(db).get_context(first["chat_id"], 1)[0]["text"] == "новая"


def test_conflict_choice_has_compare_and_swap_and_no_counter_rewrite(importer, db, tmp_path):
    first = load(
        importer, export(tmp_path / "a", [message(text="актуальная", edited_unixtime="1750000030")])
    )
    older = load(
        importer,
        export(tmp_path / "b", [message(text="старая", edited_unixtime="1750000020")]),
        policy="prefer_imported",
    )
    item = importer.conflicts.list(older["id"])["results"][0]
    load(
        importer, export(tmp_path / "c", [message(text="ещё новее", edited_unixtime="1750000040")])
    )
    with pytest.raises(UserError, match="версия изменилась"):
        importer.conflicts.resolve(older["id"], 1, "use_imported", item["current_version"])
    fresh = importer.conflicts.list(older["id"])["results"][0]
    importer.conflicts.resolve(older["id"], 1, "use_imported", fresh["current_version"])
    assert ContextService(db).get_context(first["chat_id"], 1)[0]["text"] == "старая"
    assert importer.get(older["id"])["pending_conflicts"] == 0
    assert importer.get(older["id"])["updated"] == 0
    assert importer.get(older["id"])["conflicts"] == 1
    with pytest.raises(UserError, match="уже разрешён"):
        importer.conflicts.resolve(older["id"], 1, "use_imported", fresh["current_version"])
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM index_work WHERE reason='conflict_resolution'"
            ).fetchone()[0]
            == 1
        )


def test_conflict_choice_rolls_back_atomically(importer, db, tmp_path, monkeypatch):
    first = load(importer, export(tmp_path / "a", [message(text="текущая")]))
    conflict = load(importer, export(tmp_path / "b", [message(text="предложенная")]))
    item = importer.conflicts.list(conflict["id"])["results"][0]
    original = importer._apply_batch_unlocked

    def fail(*args):
        original(*args)
        raise RuntimeError("synthetic transaction failure")

    monkeypatch.setattr(importer, "_apply_batch_unlocked", fail)
    with pytest.raises(RuntimeError):
        importer.conflicts.resolve(conflict["id"], 1, "use_imported", item["current_version"])
    assert ContextService(db).get_context(first["chat_id"], 1)[0]["text"] == "текущая"
    assert importer.get(conflict["id"])["pending_conflicts"] == 1


def test_v1_database_migrates_in_place_and_preserves_fts(tmp_path):
    db = Database(tmp_path / "workspace")
    db.path.parent.mkdir(parents=True)
    schema = Path(__file__).resolve().parents[1] / "src/telegram_search/storage/schema.sql"
    with db.connect() as conn:
        execute_sql(conn, schema.read_text())
        conn.execute(
            "INSERT INTO chats VALUES('synthetic','default','100','Тест','personal_chat',0)"
        )
        conn.execute(
            "INSERT INTO messages(chat_id,message_id,timestamp,author,kind,text,"
            "text_normalized,content_hash,raw_json) VALUES('synthetic',1,1750000000,"
            "'Автор','message','велосипед','велосипед','synthetic','{}')"
        )
    db.initialize()
    db.initialize()
    with db.connect() as conn:
        assert (
            conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == SCHEMA_VERSION
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM message_fts WHERE message_fts MATCH 'велосипед'"
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT target_generation FROM index_segments").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM index_work WHERE state='pending'").fetchone()[0] == 1
        )


def test_conflict_body_and_token_use_same_read_snapshot(importer, db, tmp_path, monkeypatch):
    load(
        importer, export(tmp_path / "a", [message(text="показанная", edited_unixtime="1750000010")])
    )
    conflict = load(importer, export(tmp_path / "b", [message(text="предложенная")]))
    newer = export(tmp_path / "c", [message(text="скрытая новая", edited_unixtime="1750000020")])
    original = ContextService.serialize_message
    changed = False

    def serialize(conn, row, *args):
        nonlocal changed
        body = original(conn, row, *args)
        if not changed:
            changed = True
            load(importer, newer)
        return body

    monkeypatch.setattr(ContextService, "serialize_message", staticmethod(serialize))
    item = importer.conflicts.list(conflict["id"])["results"][0]
    assert item["current"]["text"] == "показанная"
    with pytest.raises(UserError, match="версия изменилась"):
        importer.conflicts.resolve(conflict["id"], 1, "use_imported", item["current_version"])


def test_changed_media_rejects_frozen_import_and_repair_enqueues_day(importer, db, tmp_path):
    root = tmp_path / "photos"
    path = export(root, [message(photo="photo.png")])
    missing = load(importer, path)
    with db.connect() as conn:
        before = conn.execute("SELECT target_generation FROM index_segments").fetchone()[0]
    Image.new("RGB", (10, 10), "red").save(root / "photo.png")
    assert load(importer, path)["unchanged"] == 1
    with db.connect() as conn:
        assert (
            conn.execute("SELECT target_generation FROM index_segments").fetchone()[0] == before + 1
        )
    report = preview(importer, path)
    Image.new("RGB", (10, 10), "blue").save(root / "photo.png")
    job_id = importer.previews.apply(report["id"])
    result = importer.futures[job_id].result(timeout=5)
    assert result["state"] == "failed" and result["processed"] == 0
    assert "Медиа изменились" in result["error"]
    assert importer.get(missing["id"])["added"] == 1
