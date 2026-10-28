"""Audio metadata and legacy attachment compatibility, without decoding or models."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from conftest import export, load, message

from telegram_search.audio.classification import desktop_file_kind, telegram_audio_kind
from telegram_search.ingestion.importer import inspect_media
from telegram_search.search.lexical import ContextService
from telegram_search.shared.text import content_hash
from telegram_search.telegram_sync.adapter import normalize_message


@pytest.mark.parametrize(
    ("metadata", "kind"),
    [
        ({"media_type": "voice_message", "mime_type": "audio/ogg"}, "voice"),
        ({"media_type": "audio_file", "mime_type": "application/octet-stream"}, "audio"),
        ({"mime_type": " AuDio/MPEG; charset=binary "}, "audio"),
        ({"media_type": "video_message", "mime_type": "audio/ogg"}, "attachment"),
        ({"media_type": "video_file", "mime_type": "audio/mp4"}, "attachment"),
        ({"media_type": "animation", "mime_type": "audio/mp4"}, "attachment"),
        ({"file": "misleading.mp3", "mime_type": "application/pdf"}, "attachment"),
        ({"file": "unknown.m4a"}, "attachment"),
        ({"media_type": [], "mime_type": None}, "attachment"),
    ],
)
def test_desktop_audio_uses_metadata_not_filename(metadata, kind):
    assert desktop_file_kind(metadata) == kind


def attribute(name, **values):
    return type(name, (), values)()


@pytest.mark.parametrize(
    ("mime", "attributes", "kind"),
    [
        ("application/octet-stream", [attribute("DocumentAttributeAudio", voice=True)], "voice"),
        ("application/octet-stream", [attribute("DocumentAttributeAudio", voice=False)], "audio"),
        ("audio/mpeg", [], "audio"),
        ("audio/ogg", [attribute("DocumentAttributeVideo")], None),
        ("video/mp4", [attribute("DocumentAttributeAudio", voice=True)], None),
        ("application/pdf", [attribute("DocumentAttributeFilename", file_name="file.mp3")], None),
    ],
)
def test_telegram_document_attributes_and_mime(mime, attributes, kind):
    document = SimpleNamespace(id=1, size=4, mime_type=mime, attributes=attributes)
    assert telegram_audio_kind(document) == kind
    peer = attribute("PeerUser", user_id=100)
    remote = SimpleNamespace(
        id=1,
        peer_id=peer,
        from_id=peer,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        document=document,
    )
    normalized = normalize_message(remote)
    assert normalized.media_identity == "document:1"
    assert not normalized.downloadable  # Audio download requires its own opt-in policy.
    if kind:
        assert (
            normalized.data["media_type"] == {"voice": "voice_message", "audio": "audio_file"}[kind]
        )


def test_audio_blob_identity_is_compatible_with_old_attachment_hashes():
    record = message(file="voice.ogg", media_type="voice_message")
    generic = [{"kind": "attachment", "sha256": "a" * 64, "relative_path": "voice.ogg"}]
    for kind in ("voice", "audio"):
        assert content_hash(record, generic) == content_hash(record, [{**generic[0], "kind": kind}])
    assert content_hash(record, generic) != content_hash(record, [{**generic[0], "kind": "photo"}])


def test_legacy_refinement_preserves_message_and_text_generation(
    db, importer, tmp_path, monkeypatch
):
    import telegram_search.ingestion.importer as module

    root = tmp_path / "source"
    root.mkdir()
    (root / "voice.ogg").write_bytes(b"synthetic file; decoding belongs to ASR")
    path = export(
        root, [message(file="voice.ogg", media_type="voice_message", mime_type="audio/ogg")]
    )

    def old_inspection(root, record):
        return [{**item, "kind": "attachment"} for item in inspect_media(root, record)]

    with monkeypatch.context() as legacy:
        legacy.setattr(module, "inspect_media", old_inspection)
        chat = load(importer, path)["chat_id"]
    with db.connect() as conn:
        before = tuple(conn.execute("SELECT rowid,content_hash,raw_json FROM messages").fetchone())
        generation = conn.execute("SELECT target_generation FROM index_segments").fetchone()[0]
        revision = conn.execute("SELECT revision FROM chats WHERE id=?", (chat,)).fetchone()[0]
        ref = conn.execute("SELECT id,sha256 FROM media_refs").fetchone()
    result = load(importer, path)
    assert result["unchanged"] == 1 and result["conflicts"] == result["updated"] == 0
    with db.connect() as conn:
        assert (
            tuple(conn.execute("SELECT rowid,content_hash,raw_json FROM messages").fetchone())
            == before
        )
        assert (
            conn.execute("SELECT target_generation FROM index_segments").fetchone()[0] == generation
        )
        assert (
            conn.execute("SELECT revision FROM chats WHERE id=?", (chat,)).fetchone()[0] > revision
        )
        assert tuple(conn.execute("SELECT id,sha256 FROM media_refs").fetchone()) == tuple(ref)
        assert conn.execute("SELECT kind FROM media_refs").fetchone()[0] == "voice"
    assert ContextService(db).get_context(chat, 1)[0]["media"][0]["kind"] == "voice"


def test_alternative_roots_and_old_metadata_do_not_downgrade_or_duplicate_audio(
    db, importer, tmp_path
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "record.bin").write_bytes(b"identical synthetic audio bytes")
    (second / "copy.bin").write_bytes((first / "record.bin").read_bytes())
    chat = load(importer, export(first, [message(file="record.bin", media_type="voice_message")]))[
        "chat_id"
    ]
    result = load(importer, export(second, [message(file="copy.bin")]))
    assert result["unchanged"] == 1 and result["conflicts"] == 0
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM media_blobs").fetchone()[0] == 1
        assert [row[0] for row in conn.execute("SELECT kind FROM media_refs")] == ["voice", "voice"]
    assert len(ContextService(db).get_context(chat, 1)[0]["media"]) == 1


def test_changed_audio_bytes_still_require_normal_conflict_resolution(db, importer, tmp_path):
    root = tmp_path / "audio"
    root.mkdir()
    payload = root / "audio.bin"
    payload.write_bytes(b"first synthetic bytes")
    path = export(root, [message(file="audio.bin", media_type="audio_file")])
    load(importer, path)
    with db.connect() as conn:
        original = conn.execute("SELECT sha256 FROM media_refs").fetchone()[0]
    payload.write_bytes(b"changed synthetic bytes")
    result = load(importer, path)
    assert result["conflicts"] == 1
    with db.connect() as conn:
        assert conn.execute("SELECT sha256 FROM media_refs").fetchone()[0] == original


@pytest.mark.parametrize("alternative_root", [False, True])
def test_mime_only_export_preserves_confirmed_voice(db, importer, tmp_path, alternative_root):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    payload = b"same voice bytes"
    (first / "voice.bin").write_bytes(payload)
    (second / "copy.bin").write_bytes(payload)
    chat = load(importer, export(first, [message(file="voice.bin", media_type="voice_message")]))[
        "chat_id"
    ]
    root, name = (second, "copy.bin") if alternative_root else (first, "voice.bin")
    result = load(importer, export(root, [message(file=name, mime_type="audio/ogg")]))
    assert result["unchanged"] == 1 and result["conflicts"] == 0
    with db.connect() as conn:
        kinds = {row[0] for row in conn.execute("SELECT kind FROM media_refs")}
    assert kinds == {"voice"}
    assert len(ContextService(db).get_context(chat, 1)[0]["media"]) == 1


def test_missing_alternative_path_keeps_type_without_inventing_bytes(db, importer, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "voice.bin").write_bytes(b"local ready voice")
    chat = load(importer, export(first, [message(file="voice.bin", media_type="voice_message")]))[
        "chat_id"
    ]
    result = load(importer, export(second, [message(file="voice.bin")]))
    assert result["unchanged"] == 1
    with db.connect() as conn:
        missing = conn.execute("SELECT kind,sha256,status FROM media_refs WHERE status='missing'")
        assert tuple(missing.fetchone()) == ("voice", None, "missing")
    media = ContextService(db).get_context(chat, 1)[0]["media"]
    assert len(media) == 1 and media[0]["status"] == "ready"
    result = load(importer, export(second, [message(file="unrelated.bin")]))
    assert result["conflicts"] == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_refs").fetchone()[0] == 2


def test_explicit_metadata_refines_all_copies(db, importer, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for root in (first, second):
        (root / "file.bin").write_bytes(b"same document")
    chat = load(importer, export(first, [message(file="file.bin", media_type="voice_message")]))[
        "chat_id"
    ]
    load(importer, export(second, [message(file="file.bin", mime_type="audio/ogg")]))
    result = load(importer, export(first, [message(file="file.bin", media_type="video_file")]))
    assert result["unchanged"] == 1
    with db.connect() as conn:
        assert {row[0] for row in conn.execute("SELECT kind FROM media_refs")} == {"attachment"}
    assert len(ContextService(db).get_context(chat, 1)[0]["media"]) == 1
