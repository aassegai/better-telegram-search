import asyncio
import json
import os
import time
from dataclasses import replace
from unittest.mock import patch

import pytest
from conftest import export, load, message
from telegram_fake import FakeAdapter, remote

from telegram_search.search.lexical import Filters, SearchService
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import content_hash
from telegram_search.telegram_sync.coordinator import SyncCoordinator
from telegram_search.telegram_sync.models import Peer, SourceFailure, desktop_peer
from telegram_search.telegram_sync.settings import SyncSettings


@pytest.fixture
def sync(importer, db):
    fake = FakeAdapter()
    settings = SyncSettings(api_id=1, api_hash="a" * 32, min_free_mib=64)
    service = SyncCoordinator(
        db, importer.lifecycle_lock, factory=lambda *_: fake, settings=settings
    )

    async def setup():
        await service.connection.auth_start("+10000000000")
        await service.connection.auth_code(service.connection.flow.id, "12345")
        await service.connection.dialogs()

    asyncio.run(setup())
    yield service, fake
    asyncio.run(service.close())


def bind(service, fake, importer, tmp_path, records=None, *, peer=None, policy="archive"):
    peer = peer or fake.peers[0]
    fake.peers = [peer]
    service.connection.dialog_choices[peer.key] = peer
    records = records if records is not None else [message(1, date=int(time.time()))]
    job = load(
        importer, export(tmp_path / f"seed-{peer.key.replace(':', '-')}", records, chat_id=peer.id)
    )
    with service.db.connect() as conn:
        conn.execute(
            "UPDATE chats SET kind=? WHERE id=?",
            (
                {"user": "personal_chat", "chat": "private_group", "channel": "private_supergroup"}[
                    peer.kind
                ],
                job["chat_id"],
            ),
        )
    for record in records:
        fake.messages[(peer.key, record["id"])] = remote(
            record["id"],
            record["text"],
            peer=peer,
            date=int(record["date_unixtime"]),
            photo=1 if record.get("photo") else None,
        )
    preview = asyncio.run(service.bindings.preview(peer.key, job["chat_id"]))
    service.bindings.apply(preview["preview_token"], True, preview["revision"])
    binding = service.store.binding(job["chat_id"])
    if policy != "archive":
        service.bindings.settings(job["chat_id"], binding["revision"], {"deletion_policy": policy})
        binding = service.store.binding(job["chat_id"])
    return binding


def scan(service):
    claimed = service.store.claim("scan")
    assert claimed
    asyncio.run(service.scan(*claimed))
    return claimed


def test_typed_peer_ids_and_equivalent_entities():
    assert Peer.from_marked(-1000000000123) == Peer("channel", 123)
    assert Peer.from_marked(-123) == Peer("chat", 123)
    assert desktop_peer("123", "personal_chat") == Peer("user", 123)
    desktop = message(text=["😀 ", {"type": "bold", "text": "text"}], date=100)
    api = message(
        text="😀 text", date=100, text_entities=[{"type": "bold", "offset": 3, "length": 4}]
    )
    desktop["date"] = "2000-01-01T12:00:00"
    assert content_hash(desktop, []) == content_hash(api, [])


def test_overlap_and_live_high_id_never_skip_history(sync, importer, tmp_path):
    service, fake = sync
    binding = bind(service, fake, importer, tmp_path)
    fake.messages.update({mid: remote(mid) for mid in range(2, 225)})

    async def live():
        await service.receive("message", remote(1000), None)

    fake.page_hook = live
    scan(service)
    with service.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 225
        cursor = conn.execute("SELECT * FROM dialog_sync_cursors").fetchone()
        assert cursor["scanned_through_id"] == 224
        assert cursor["upper_bound"] is None
        assert (
            conn.execute("SELECT COUNT(*) FROM index_work WHERE state='pending'").fetchone()[0] == 1
        )
    before = service.store.status(binding["chat_id"])
    # OS wall clocks can move backwards; status follows durable insertion order.
    with patch(
        "telegram_search.telegram_sync.store.time.time",
        return_value=before["run"]["started_at"] - 5,
    ):
        run_id = service.store.request(binding["id"])
    scan(service)
    after = service.store.status(binding["chat_id"])
    assert after["run"]["id"] == run_id
    assert after["run"]["added"] == 0
    assert after["run"]["updated"] == 0
    assert before["cursor"]["baseline_id"] == 1


def test_cursor_and_outbox_rollback_together(sync, importer, tmp_path):
    service, fake = sync
    binding = bind(service, fake, importer, tmp_path)
    with patch(
        "telegram_search.ingestion.writer.invalidate_segments",
        side_effect=RuntimeError("synthetic"),
    ):
        with pytest.raises(RuntimeError):
            service.store.merge(binding, [remote(2)], cursor=2)
    with service.db.connect() as conn:
        assert conn.execute("SELECT scanned_through_id FROM dialog_sync_cursors").fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM messages WHERE message_id=2").fetchone()
        assert not conn.execute("SELECT 1 FROM telegram_message_provenance").fetchone()


def test_wrong_peer_page_cannot_write_to_bound_chat(sync, importer, tmp_path):
    service, fake = sync
    binding = bind(service, fake, importer, tmp_path)
    with pytest.raises(SourceFailure, match="unexpected"):
        service.store.merge(binding, [remote(2, peer=Peer("channel", 100))], cursor=2)
    assert service.cursor(binding)["scanned_through_id"] == 1


def test_partial_budget_and_restart_resume_from_committed_cursor(sync, importer, tmp_path):
    service, fake = sync
    bind(service, fake, importer, tmp_path)
    service.settings = replace(service.settings, pages_per_run=1)
    fake.messages.update({mid: remote(mid) for mid in range(2, 250)})
    scan(service)
    assert (
        service.cursor(service.store.binding(iter_chat_ids(service)[0]))["scanned_through_id"]
        == 101
    )
    service.store.recover()
    with service.db.connect() as conn:
        conn.execute("UPDATE telegram_jobs SET available_at=0 WHERE kind='scan'")
    scan(service)
    assert fake.page_calls[1][0] == 101


def iter_chat_ids(service):
    with service.db.connect() as conn:
        return [row[0] for row in conn.execute("SELECT id FROM chats")]


def test_old_edits_and_forced_old_exports_cannot_overwrite_api(sync, importer, tmp_path):
    service, fake = sync
    binding = bind(service, fake, importer, tmp_path)
    now = int(time.time())
    assert service.store.merge(binding, [remote(1, "new", edited=now + 2)])["updated"] == 1
    assert service.store.merge(binding, [remote(1, "old", edited=now + 1)])["conflicts"] == 1
    old = export(tmp_path / "late", [message(1, "old", date=now)], chat_id=100)
    result = load(importer, old, policy="prefer_imported")
    assert result["conflicts"] == 1
    with service.db.connect() as conn:
        assert conn.execute("SELECT text FROM messages").fetchone()[0] == "new"


def test_same_hash_has_no_new_generation(sync, importer, tmp_path):
    service, fake = sync
    binding = bind(service, fake, importer, tmp_path)
    with service.db.connect() as conn:
        before = conn.execute("SELECT target_generation FROM index_segments").fetchone()[0]
    result = service.store.merge(binding, [next(iter(fake.messages.values()))])
    assert result["unchanged"] == 1
    with service.db.connect() as conn:
        assert conn.execute("SELECT target_generation FROM index_segments").fetchone()[0] == before


@pytest.mark.parametrize("policy", ["archive", "mirror"])
def test_deletion_policy_and_old_json_tombstone(sync, importer, tmp_path, policy):
    service, fake = sync
    bind(service, fake, importer, tmp_path, policy=policy)
    assert service.store.delete_event(999, None, [1]) == 1
    with service.db.connect() as conn:
        row = conn.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        assert (row is None) == (policy == "mirror")
        if row:
            assert row["remote_deleted"] == 1
    original = next((tmp_path / "seed-user-100").glob("*.json"))
    result = load(importer, original, policy="prefer_imported")
    assert result["conflicts"] == 1
    conflicts = importer.conflicts.list(result["id"])
    assert conflicts["results"][0]["reason"] == "remote_deleted"
    assert conflicts["results"][0]["current_version"]
    assert not SearchService(service.db).search("Синтетический", Filters(exclude_deleted=True))[
        "results"
    ]
    assert original.exists()


def test_peerless_delete_never_deletes_channel_or_ambiguous_peer(sync, importer, tmp_path):
    service, fake = sync
    user = bind(service, fake, importer, tmp_path, peer=Peer("user", 100, 123), policy="mirror")
    # Different dataset scope avoids sharing the synthetic export chat UUID.
    channel = bind(
        service, fake, importer, tmp_path, peer=Peer("channel", 101, 123), policy="mirror"
    )
    assert service.store.delete_event(888, None, [1]) == 0
    assert service.store.delete_event(999, None, [1]) == 1
    with service.db.connect() as conn:
        assert not conn.execute(
            "SELECT 1 FROM messages WHERE chat_id=?", (user["chat_id"],)
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM messages WHERE chat_id=?", (channel["chat_id"],)
        ).fetchone()


def test_binding_mismatch_and_cas_rejected(sync, importer, tmp_path):
    service, fake = sync
    now = int(time.time())
    job = load(importer, export(tmp_path / "seed", [message(1, date=now)]))
    fake.messages[1] = remote(1, "different", date=now)
    preview = asyncio.run(service.bindings.preview(fake.peers[0].key, job["chat_id"]))
    assert preview["mismatches"] == 1 and not preview["can_bind"]
    with pytest.raises(UserError):
        service.bindings.apply(preview["preview_token"], True, preview["revision"])
    fake.messages[1] = remote(1, message()["text"], date=now)
    preview = asyncio.run(service.bindings.preview(fake.peers[0].key, job["chat_id"]))
    with service.db.connect() as conn:
        conn.execute("UPDATE chats SET revision=revision+1")
    with pytest.raises(UserError):
        service.bindings.apply(preview["preview_token"], True, preview["revision"])


def test_update_now_is_idempotent_pause_and_deleted_binding_fence_writes(sync, importer, tmp_path):
    service, fake = sync
    binding = bind(service, fake, importer, tmp_path)
    assert service.store.request(binding["id"]) == service.store.request(binding["id"])
    job, snapshot = service.store.claim("scan")
    service.bindings.settings(binding["chat_id"], binding["revision"], {"enabled": False})
    assert service.store.merge(snapshot, [remote(10)], cursor=10) is None
    binding = service.store.binding(binding["chat_id"])
    service.bindings.remove(binding["chat_id"], binding["revision"])
    assert service.store.merge(snapshot, [remote(11)]) is None
    with service.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM telegram_jobs").fetchone()


def test_flood_wait_persisted_and_auth_revoke_stops_all_jobs(sync, importer, tmp_path):
    service, fake = sync
    bind(service, fake, importer, tmp_path)
    job, snapshot = service.store.claim("scan")
    asyncio.run(service.failed(job, snapshot, SourceFailure("waiting_rate_limit", 90)))
    assert service.connection.status()["retry_after"] >= time.time() + 88
    assert service.store.claim("scan") is None
    with service.db.connect() as conn:
        conn.execute("UPDATE telegram_connections SET retry_after=0")
        conn.execute("UPDATE telegram_jobs SET available_at=0")
    job, snapshot = service.store.claim("scan")
    asyncio.run(service.failed(job, snapshot, SourceFailure("auth_required")))
    assert service.connection.status()["state"] == "auth_required"
    assert service.connection.client is None
    assert service.store.claim("scan") is None


def test_new_image_download_is_validated_atomic_and_reused(sync, importer, tmp_path):
    service, fake = sync
    binding = bind(service, fake, importer, tmp_path)
    service.store.merge(binding, [remote(2, photo=20), remote(3, photo=30)])
    while claimed := service.store.claim("media"):
        asyncio.run(service.media.run(*claimed, fake))
    with service.db.connect() as conn:
        paths = [
            row[0]
            for row in conn.execute("SELECT relative_path FROM media_refs WHERE status='ready'")
        ]
        assert len(paths) == 2 and paths[0] == paths[1]
        assert conn.execute("SELECT COUNT(*) FROM media_blobs").fetchone()[0] == 1
    service.store.delete_event(999, Peer("user", 100), [2])
    assert service.media.collect_orphans(0) == 0
    assert not list((service.db.workspace / "data/media/telegram").rglob("*.part"))


def test_interrupted_download_cleans_part_and_preserves_cursor(sync, importer, tmp_path):
    service, fake = sync
    binding = bind(service, fake, importer, tmp_path)
    service.store.merge(binding, [remote(2, photo=2)])
    fake.download_error = SourceFailure("network")
    claimed = service.store.claim("media")
    with pytest.raises(SourceFailure):
        asyncio.run(service.media.run(*claimed, fake))
    assert not list((service.db.workspace / "data/media/telegram").rglob("*.part"))
    assert service.cursor(binding)["scanned_through_id"] == 1
    with service.db.connect() as conn:
        assert conn.execute("SELECT reserved_bytes FROM telegram_assets").fetchone()[0] == 0
        assert not conn.execute("SELECT 1 FROM media_refs WHERE status='ready'").fetchone()


def test_image_quota_and_decode_errors_are_finite(sync, importer, tmp_path):
    service, fake = sync
    binding = bind(service, fake, importer, tmp_path)
    service.store.merge(binding, [remote(2, photo=2)])
    claimed = service.store.claim("media")
    with patch("telegram_search.telegram_sync.media.shutil.disk_usage") as usage:
        usage.return_value.free = 10
        with pytest.raises(SourceFailure, match="disk_full"):
            asyncio.run(service.media.run(*claimed, fake))
    fake.photo_bytes = b"not an image"
    with pytest.raises(SourceFailure, match="media_invalid"):
        asyncio.run(service.media.run(*claimed, fake))


def test_old_missing_photos_are_not_scheduled(sync, importer, tmp_path):
    service, fake = sync
    binding = bind(
        service,
        fake,
        importer,
        tmp_path,
        records=[message(1, date=int(time.time()), photo="photos/missing.jpg")],
    )
    result = service.store.merge(binding, [remote(1, message()["text"], photo=1)])
    assert result["unchanged"] == 1
    with service.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM telegram_assets").fetchone()


def test_auth_ttl_private_session_and_no_payload_in_status(sync):
    service, fake = sync

    async def flow():
        await service.connection.disconnect()
        fake.authenticated = False
        fake.need_password = True
        started = await service.connection.auth_start("+10000000000")
        await service.connection.auth_code(started["flow_id"], "12345")
        assert service.connection.flow.phone == service.connection.flow.code_hash == ""
        service.connection.flow.expires_at = 0
        await service.connection.expire_flow()
        assert service.connection.flow is None

    asyncio.run(flow())
    status = json.dumps(service.connection.status())
    assert (
        "12345" not in status
        and "+10000000000" not in status
        and "synthetic-code-hash" not in status
    )
    private = service.db.workspace / "private/telegram"
    if os.name != "nt":
        assert private.stat().st_mode & 0o777 == 0o700


def test_concurrent_media_publish_deduplicates_physical_files(sync, importer, tmp_path):
    service, fake = sync
    binding = bind(service, fake, importer, tmp_path)
    service.store.merge(binding, [remote(2, photo=20), remote(3, photo=30)])
    claims = [service.store.claim("media"), service.store.claim("media")]

    async def run():
        await asyncio.gather(*(service.media.run(*claim, fake) for claim in claims))

    asyncio.run(run())
    root = service.db.workspace / "data/media/telegram"
    assert len(list(root.rglob("*.png"))) == 1
    with service.db.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM telegram_assets WHERE state='ready'").fetchone()[0]
            == 2
        )


def test_expired_job_attempt_cannot_publish_or_fail_new_attempt(sync, importer, tmp_path):
    service, fake = sync
    binding = bind(service, fake, importer, tmp_path)
    service.store.merge(binding, [remote(2, photo=2)])
    first, snapshot = service.store.claim("media")
    with service.db.connect() as conn:
        conn.execute("UPDATE telegram_jobs SET lease_until=0 WHERE id=?", (first["id"],))
    current, binding = service.store.claim("media")
    assert current["attempt"] == first["attempt"] + 1
    asyncio.run(service.media.run(current, binding, fake))
    asyncio.run(service.failed(first, snapshot, SourceFailure("media_invalid")))
    service.media._cancel(first)
    with service.db.connect() as conn:
        assert conn.execute("SELECT state FROM telegram_assets").fetchone()[0] == "ready"
        assert (
            conn.execute("SELECT state FROM telegram_jobs WHERE id=?", (first["id"],)).fetchone()[0]
            == "done"
        )
    scan_job, snapshot = service.store.claim("scan")
    with service.db.connect() as conn:
        conn.execute("UPDATE telegram_jobs SET lease_until=0 WHERE id=?", (scan_job["id"],))
    newer, _ = service.store.claim("scan")
    assert newer["attempt"] > scan_job["attempt"]
    assert service.store.merge(snapshot, [remote(3)], cursor=3, job=scan_job) is None
    service._partial(scan_job, snapshot)
    assert not service._upper(scan_job, snapshot, 1000)
    with service.db.connect() as conn:
        assert (
            conn.execute("SELECT state FROM sync_runs WHERE id=?", (newer["run_id"],)).fetchone()[0]
            == "running"
        )


def test_media_pause_resume_returns_current_download_to_pending(sync, importer, tmp_path):
    service, fake = sync
    binding = bind(service, fake, importer, tmp_path)
    service.store.merge(binding, [remote(2, photo=2)])
    job, snapshot = service.store.claim("media")
    service.bindings.settings(binding["chat_id"], binding["revision"], {"download_media": False})
    asyncio.run(service.media.run(job, snapshot, fake))
    with service.db.connect() as conn:
        assert (
            conn.execute("SELECT state FROM telegram_jobs WHERE id=?", (job["id"],)).fetchone()[0]
            == "pending"
        )
        conn.execute("UPDATE telegram_jobs SET available_at=0")
    binding = service.store.binding(binding["chat_id"])
    service.bindings.settings(binding["chat_id"], binding["revision"], {"download_media": True})
    asyncio.run(service.media.run(*service.store.claim("media"), fake))
    with service.db.connect() as conn:
        assert conn.execute("SELECT state FROM telegram_assets").fetchone()[0] == "ready"


def test_shutdown_fences_delayed_thread_writes(sync, importer, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    service, fake = sync
    binding = bind(service, fake, importer, tmp_path)
    release = Event()

    def delayed_write():
        assert release.wait(5)
        return service.store.merge(binding, [remote(2)])

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(delayed_write)
        asyncio.run(service.close())
        release.set()
        with pytest.raises(SourceFailure, match="stopped"):
            future.result(timeout=5)
    with service.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM messages WHERE message_id=2").fetchone()


def test_gc_skips_symlinks_and_reparse_points(sync, tmp_path, monkeypatch):
    import stat
    from pathlib import Path
    from types import SimpleNamespace

    from telegram_search.telegram_sync.paths import owned_path

    service, _ = sync
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "00000000-0000-0000-0000-000000000003.png"
    secret.write_bytes(b"synthetic private file")
    root = service.db.workspace / "data/media/telegram"
    root.mkdir(parents=True)
    link = root / "00000000-0000-0000-0000-000000000001"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pass  # Windows may require administrator privileges for symlinks.
    else:
        assert service.media.collect_orphans(0) == 0 and secret.exists()
        link.unlink()
    link.mkdir()
    original = Path.lstat

    def reparse(path):
        info = original(path)
        return (
            SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            if path == link
            else info
        )

    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(SourceFailure, match="storage"):
        owned_path(service.db.workspace, link)
    assert service.media.collect_orphans(0) == 0


def test_cancel_auth_interrupts_code_rpc_before_lock(sync):
    service, fake = sync

    async def scenario():
        await service.connection.disconnect()
        fake.authenticated = False
        flow = await service.connection.auth_start("+10000000000")
        entered = asyncio.Event()

        async def blocked(*_):
            entered.set()
            await asyncio.Event().wait()

        fake.login_code = blocked
        auth = asyncio.create_task(service.connection.auth_code(flow["flow_id"], "12345"))
        await entered.wait()
        await asyncio.wait_for(service.connection.cancel_auth(flow["flow_id"]), timeout=2)
        with pytest.raises(UserError, match="отменён"):
            await auth
        assert service.connection.flow is None and service.connection.client is None
        assert not service.connection.status()["connected"]

    asyncio.run(scenario())


def test_disconnect_cancels_pending_network_rpc(sync):
    service, fake = sync

    async def scenario():
        entered = asyncio.Event()

        async def blocked(*_):
            entered.set()
            await asyncio.Event().wait()

        fake.dialogs = blocked
        request = asyncio.create_task(service.connection.dialogs())
        await entered.wait()
        await asyncio.wait_for(service.connection.disconnect(), timeout=2)
        with pytest.raises(UserError):
            await request
        assert not service.connection.status()["connected"]

    asyncio.run(scenario())


def test_archive_delete_filter_omits_context_and_unknown_peer_is_ignored(sync, importer, tmp_path):
    from telegram_search.search.lexical import ContextService

    service, fake = sync
    binding = bind(service, fake, importer, tmp_path)
    service.store.merge(binding, [remote(2), remote(3)])
    assert service.store.delete_event(999, Peer("user", 9999), [2]) == 0
    assert service.store.delete_event(999, Peer("user", 100), [2]) == 1
    filters = Filters(exclude_deleted=True)
    context = ContextService(service.db)
    assert [
        m["message_id"] for m in context.get_context(binding["chat_id"], 1, filters=filters)
    ] == [1, 3]
    with pytest.raises(UserError):
        context.get_context(binding["chat_id"], 2, filters=filters)


def test_corrupt_shared_copy_is_repaired_before_dedup_publication(sync, importer, tmp_path):
    import hashlib

    service, fake = sync
    binding = bind(service, fake, importer, tmp_path)
    service.store.merge(binding, [remote(2, photo=2)])
    asyncio.run(service.media.run(*service.store.claim("media"), fake))
    with service.db.connect() as conn:
        row = conn.execute(
            "SELECT r.relative_path,s.relative_path root,r.sha256 FROM media_refs r "
            "JOIN source_roots s ON s.id=r.source_root_id WHERE r.status='ready'"
        ).fetchone()
    path = service.db.workspace / row["root"] / row["relative_path"]
    corrupt = bytearray(path.read_bytes())
    corrupt[-1] ^= 1
    path.write_bytes(corrupt)
    service.store.merge(binding, [remote(3, photo=3)])
    asyncio.run(service.media.run(*service.store.claim("media"), fake))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]


def test_disconnect_during_restore_does_not_cancel_periodic_scheduler(sync):
    service, fake = sync

    async def scenario():
        entered, completed = asyncio.Event(), asyncio.Event()
        original = fake.authorized

        async def blocked():
            entered.set()
            await asyncio.Event().wait()

        fake.authorized = blocked
        service.connection.update("connecting", reconnect=True)

        async def scheduler():
            await service.connection.restore()
            completed.set()

        task = asyncio.create_task(scheduler())
        await entered.wait()
        await asyncio.wait_for(service.connection.disconnect(), timeout=2)
        await asyncio.wait_for(task, timeout=2)
        assert completed.is_set() and not task.cancelled()
        fake.authorized = original
        service.connection.update("connecting", reconnect=True)
        assert await service.connection.restore()

    asyncio.run(scenario())
