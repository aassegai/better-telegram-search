import json

import pytest
from conftest import export, load, message
from PIL import Image

from telegram_search.errors import UserError
from telegram_search.importer import ImportService
from telegram_search.search import ContextService, SearchService


def test_streaming_entities_service_and_idempotence(importer, db, tmp_path):
    data = [
        message(
            text=["Привет ", {"type": "text_link", "text": "мир", "href": "https://example.test"}],
            text_entities=[{"type": "text_link", "text": "мир", "href": "https://example.test"}],
            reply_to_message_id=9,
            forwarded_from="Вымышленный источник",
        ),
        message(2, "", type="service", action="join_group", actor_id="actor1"),
        message(3, "длинная реплика " * 5000),
    ]
    path = export(tmp_path / "input", data)
    first = load(importer, path)
    again = load(importer, path)
    assert first["added"] == 3
    assert again["added"] == 0 and again["unchanged"] == 3
    context = ContextService(db).get_context(first["chat_id"], 1)
    assert context[0]["text"] == "Привет мир"
    assert context[0]["entities"][0]["href"] == "https://example.test"
    assert context[0]["reply_to"] == 9
    assert len(context[-1]["text"]) > 50000
    with db.connect() as conn:
        assert (
            json.loads(
                conn.execute("SELECT raw_json FROM messages WHERE message_id=2").fetchone()[0]
            )["action"]
            == "join_group"
        )


def test_overlap_earlier_messages_scope_and_absence(importer, db, tmp_path):
    first = load(importer, export(tmp_path / "a", [message(5), message(6)]))
    second = load(importer, export(tmp_path / "b", [message(1), message(5)]))
    assert second["added"] == 1 and second["unchanged"] == 1
    for cid in [200, 300]:
        load(importer, export(tmp_path / str(cid), [message(5)], cid, "Одинаковое имя"))
    account = load(importer, export(tmp_path / "account", [message(5)]), scope="another")
    assert account["chat_id"] != first["chat_id"]
    assert len(SearchService(db).chats()) == 4
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id=?", (first["chat_id"],)
            ).fetchone()[0]
            == 3
        )


def test_revision_conflicts_and_explicit_authority(importer, db, tmp_path):
    first = load(
        importer,
        export(tmp_path / "a", [message(text="первая версия", edited_unixtime="1750000010")]),
    )
    newer = load(
        importer,
        export(tmp_path / "b", [message(text="новая версия", edited_unixtime="1750000020")]),
    )
    assert newer["updated"] == 1
    older = load(
        importer,
        export(tmp_path / "c", [message(text="старая версия", edited_unixtime="1750000015")]),
    )
    assert older["conflicts"] == 1 and older["updated"] == 0
    unversioned = export(tmp_path / "d", [message(text="без даты редакции")])
    assert load(importer, unversioned)["conflicts"] == 1
    assert load(importer, unversioned, policy="prefer_imported")["updated"] == 1
    assert ContextService(db).get_context(first["chat_id"], 1)[0]["text"] == "без даты редакции"
    assert not SearchService(db).search("новая")["results"]
    assert SearchService(db).search("редакции")["results"]


def test_missing_id_requires_mapping_and_target_check(importer, tmp_path):
    path = export(tmp_path / "a", [message()], chat_id=None)
    with pytest.raises(UserError, match="Нет ID"):
        importer.prepare(str(path))
    first = load(importer, path, create_new=True)
    assert load(importer, path, target_chat_id=first["chat_id"])["unchanged"] == 1
    different = export(tmp_path / "b", [message()], chat_id=999)
    with pytest.raises(UserError, match="не совпадает"):
        importer.prepare(str(different), target_chat_id=first["chat_id"])


def test_media_dedup_rename_and_bad_files(importer, db, tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    Image.new("RGB", (20, 20), "blue").save(a / "one.png")
    (a / "broken.jpg").write_bytes(b"broken")
    path = export(
        a,
        [
            message(1, photo="one.png"),
            message(2, photo="one.png"),
            message(3, photo="absent.jpg"),
            message(4, photo="broken.jpg"),
            message(5, photo="../outside.jpg"),
        ],
    )
    first = load(importer, path)
    assert first["missing_media"] == 1 and first["invalid_media"] == 2
    b = tmp_path / "b"
    b.mkdir()
    (b / "renamed.png").write_bytes((a / "one.png").read_bytes())
    assert load(importer, export(b, [message(1, photo="renamed.png")]))["unchanged"] == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_blobs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM media_refs WHERE message_id=1").fetchone()[0] == 2
    assert (a / "one.png").exists()


def test_checkpoint_restart_does_not_double_count(importer, db, tmp_path, monkeypatch):
    path = export(tmp_path / "a", [message(i) for i in range(1, 7)])
    job = importer.prepare(str(path))
    original = importer._apply_batch
    calls = 0

    def interrupted(job, batch):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic interruption")
        original(job, batch)

    monkeypatch.setattr(importer, "_apply_batch", interrupted)
    assert importer.run(job)["state"] == "failed"
    assert importer.get(job)["processed"] == 2
    importer.shutdown()
    replacement = ImportService(db, batch_size=2)
    try:
        resumed = replacement.run(job)
        assert resumed["state"] == "completed"
        assert resumed["processed"] == resumed["added"] == 6
    finally:
        replacement.shutdown()


def test_changed_json_cannot_resume(importer, tmp_path):
    path = export(tmp_path / "a", [message()])
    job = importer.prepare(str(path))
    export(path.parent, [message(2)])
    result = importer.run(job)
    assert result["state"] == "failed" and result["processed"] == 0
    assert "JSON изменился" in result["error"]


def test_malformed_export_and_root_path(importer, tmp_path):
    path = tmp_path / "result.json"
    path.write_text('{"messages": [', encoding="utf-8")
    with pytest.raises(UserError):
        importer.prepare(str(path))
    path = export(tmp_path / "a", [message()])
    with pytest.raises(UserError):
        importer.prepare(str(path), source_root=str(tmp_path / "other"))


def test_missing_media_restoration_and_content_replacement(importer, db, tmp_path):
    root = tmp_path / "a"
    path = export(root, [message(photo="photo.png")])
    first = load(importer, path)
    assert first["missing_media"] == 1
    Image.new("RGB", (20, 20), "red").save(root / "photo.png")
    repaired = load(importer, path)
    assert repaired["unchanged"] == 1 and repaired["conflicts"] == 0
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM media_refs").fetchone()[0] == "ready"
    (root / "photo.png").unlink()
    assert load(importer, path)["unchanged"] == 1
    Image.new("RGB", (20, 20), "blue").save(root / "photo.png")
    assert load(importer, path)["conflicts"] == 1


def test_workspace_writer_lease(importer, db):
    with pytest.raises(UserError, match="другим процессом"):
        ImportService(db)


def test_missing_root_cannot_override_known_media_hash(importer, tmp_path):
    load(importer, export(tmp_path / "missing", [message(photo="photo.png")]))
    for folder, color, expected in [
        ("repaired", "red", "unchanged"),
        ("different", "blue", "conflicts"),
    ]:
        root = tmp_path / folder
        root.mkdir()
        Image.new("RGB", (20, 20), color).save(root / "photo.png")
        assert load(importer, export(root, [message(photo="photo.png")]))[expected] == 1


def test_pause_during_media_preparation_resumes_from_checkpoint(importer, tmp_path, monkeypatch):
    import telegram_search.importer as module

    path = export(tmp_path / "a", [message(i) for i in range(1, 5)])
    job = importer.prepare(str(path))
    original = module.inspect_media
    paused = False

    def inspect(root, message):
        nonlocal paused
        if not paused:
            paused = True
            importer.control(job, "pause")
        return original(root, message)

    monkeypatch.setattr(module, "inspect_media", inspect)
    result = importer.run(job)
    assert result["state"] == "paused" and result["processed"] == 0
    importer.control(job, "resume")
    result = importer.futures[job].result(timeout=5)
    assert result["state"] == "completed" and result["added"] == 4
