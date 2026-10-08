"""Short, atomic SQLite operations. No network or image work inside transactions."""

import time
import uuid

from telegram_search.ingestion.writer import CanonicalMessageWriter
from telegram_search.shared.text import content_hash, timestamp
from telegram_search.storage.generations import bump_revision, invalidate_segments, utc_day
from telegram_search.telegram_sync.models import ERRORS, SourceFailure


class WriteFence:
    def __init__(self, store, lock):
        self.store, self.lock = store, lock

    def __enter__(self):
        self.lock.acquire()
        if self.store.closed:
            self.lock.release()
            raise SourceFailure("stopped")
        return self

    def __exit__(self, *_):
        self.lock.release()


class SyncStore:
    def __init__(self, db, lock):
        self.db, self.writer_lock = db, lock
        self.closed = False
        self.lock = WriteFence(self, lock)
        self.writer = CanonicalMessageWriter(db)

    @staticmethod
    def current(conn, binding, *, media=False):
        row = conn.execute(
            "SELECT b.* FROM dialog_sync_bindings b JOIN telegram_connections c "
            "ON c.id=b.connection_id WHERE b.id=? AND b.generation=? AND b.enabled=1 "
            "AND c.reconnect=1" + (" AND b.download_media=1" if media else ""),
            (binding["id"], binding["generation"]),
        ).fetchone()
        return dict(row) if row else None

    def binding(self, chat_id):
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM dialog_sync_bindings WHERE chat_id=?", (chat_id,)
            ).fetchone()
            return dict(row) if row else None

    def by_peer(self, account_id, peer):
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM dialog_sync_bindings WHERE account_user_id=? AND peer_type=? "
                "AND peer_id=? AND enabled=1",
                (account_id, peer.kind, peer.id),
            ).fetchone()
            return dict(row) if row else None

    def request(self, binding_id, trigger="manual"):
        now = int(time.time())
        with self.lock, self.db.connect() as conn:
            binding = conn.execute(
                "SELECT * FROM dialog_sync_bindings WHERE id=?", (binding_id,)
            ).fetchone()
            if not binding or not binding["enabled"]:
                return None
            active = conn.execute(
                "SELECT id FROM sync_runs WHERE binding_id=? AND state IN "
                "('queued','running','partial','waiting_rate_limit')",
                (binding_id,),
            ).fetchone()
            if active:
                return active[0]
            run_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO sync_runs(id,binding_id,trigger,started_at) VALUES(?,?,?,?)",
                (run_id, binding_id, trigger, now),
            )
            conn.execute(
                "INSERT INTO telegram_jobs(id,binding_id,run_id,kind,idempotency_key) "
                "VALUES(?,?,?,'scan',?)",
                (str(uuid.uuid4()), binding_id, run_id, f"scan:{run_id}"),
            )
            return run_id

    def schedule(self, interval):
        with self.db.connect() as conn:
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM dialog_sync_bindings WHERE enabled=1 AND next_sync_at<=?",
                    (int(time.time()),),
                )
            ]
        for binding_id in ids:
            self.request(binding_id, "scheduled")
        with self.lock, self.db.connect() as conn:
            conn.execute(
                "UPDATE dialog_sync_bindings SET next_sync_at=? WHERE enabled=1 AND "
                "next_sync_at<=?",
                (int(time.time()) + interval, int(time.time())),
            )

    def recover(self):
        # ImportService's process-wide workspace lease proves the former owner is gone.
        with self.lock, self.db.connect() as conn:
            conn.execute(
                "UPDATE telegram_jobs SET state='pending',lease_until=0 WHERE state='running'"
            )
            conn.execute("UPDATE sync_runs SET state='queued' WHERE state='running'")
            conn.execute("UPDATE telegram_assets SET reserved_bytes=0")

    def claim(self, kind):
        now = int(time.time())
        with self.lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT j.* FROM telegram_jobs j JOIN dialog_sync_bindings b ON b.id=j.binding_id "
                "JOIN telegram_connections c ON c.id=b.connection_id "
                "WHERE j.kind=? AND j.available_at<=? AND c.retry_after<=? AND b.enabled=1 "
                "AND c.reconnect=1 AND (j.state='pending' OR (j.state='running' AND "
                "j.lease_until<?)) "
                + ("AND b.download_media=1 " if kind == "media" else "")
                + "ORDER BY j.available_at,j.rowid LIMIT 1",
                (kind, now, now, now),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE telegram_jobs SET state='running',attempt=attempt+1,lease_until=? WHERE "
                "id=?",
                (now + 300, row["id"]),
            )
            result = dict(row)
            result["attempt"] += 1
            binding = dict(
                conn.execute(
                    "SELECT * FROM dialog_sync_bindings WHERE id=?", (row["binding_id"],)
                ).fetchone()
            )
            if row["run_id"]:
                conn.execute(
                    "UPDATE sync_runs SET state='running',error_code=NULL WHERE id=?",
                    (row["run_id"],),
                )
            return result, binding

    @staticmethod
    def job_current(conn, job):
        return (
            conn.execute(
                "SELECT 1 FROM telegram_jobs WHERE id=? AND attempt=? AND state='running'",
                (job["id"], job["attempt"]),
            ).fetchone()
            is not None
        )

    def renew(self, job):
        with self.lock, self.db.connect() as conn:
            conn.execute(
                "UPDATE telegram_jobs SET lease_until=? "
                "WHERE id=? AND attempt=? AND state='running'",
                (int(time.time()) + 300, job["id"], job["attempt"]),
            )

    def merge(self, binding, records, *, run_id=None, cursor=None, recent_offset=None, job=None):
        if any(
            (record.peer.kind, record.peer.id) != (binding["peer_type"], binding["peer_id"])
            for record in records
        ):
            raise SourceFailure("unexpected")
        with self.lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self.current(conn, binding) or (job and not self.job_current(conn, job)):
                return None
            result = self._merge(conn, binding, records)
            if cursor is not None:
                conn.execute(
                    "UPDATE dialog_sync_cursors SET scanned_through_id=MAX(scanned_through_id,?) "
                    "WHERE binding_id=?",
                    (cursor, binding["id"]),
                )
            if recent_offset is not None:
                conn.execute(
                    "UPDATE dialog_sync_cursors SET recent_offset_id=? WHERE binding_id=?",
                    (recent_offset, binding["id"]),
                )
            if run_id:
                conn.execute(
                    "UPDATE sync_runs SET fetched=fetched+?,added=added+?,updated=updated+?,"
                    "unchanged=unchanged+?,conflicts=conflicts+? WHERE id=?",
                    (
                        len(records),
                        result["added"],
                        result["updated"],
                        result["unchanged"],
                        result["conflicts"],
                        run_id,
                    ),
                )
            return result

    def _merge(self, conn, binding, records):
        prepared, assets = [], []
        for record in records:
            if record.peer.kind != binding["peer_type"] or record.peer.id != binding["peer_id"]:
                raise ValueError("peer mismatch")
            message_id = record.data["id"]
            refs = [
                dict(row) | {"size": row["size"] or 0, "preserved": True}
                for row in conn.execute(
                    "SELECT r.*,b.size FROM media_refs r LEFT JOIN media_blobs b ON "
                    "b.sha256=r.sha256 "
                    "WHERE r.chat_id=? AND r.message_id=?",
                    (binding["chat_id"], message_id),
                )
            ]
            provenance = conn.execute(
                "SELECT * FROM telegram_message_provenance WHERE chat_id=? AND message_id=?",
                (binding["chat_id"], message_id),
            ).fetchone()
            media, asset = [], None
            if record.media_identity:
                same = provenance and provenance["remote_media_id"] == record.media_identity
                seed = not provenance and bool(refs)
                if same or seed:
                    media = refs
                if record.downloadable and not any(ref["status"] == "ready" for ref in media):
                    # Old missing export media are intentionally not backfilled on binding.
                    old = conn.execute(
                        "SELECT has_photo FROM messages WHERE chat_id=? AND message_id=?",
                        (binding["chat_id"], message_id),
                    ).fetchone()
                    if not (seed or (old and old[0] and not provenance)):
                        previous = conn.execute(
                            "SELECT * FROM telegram_assets WHERE binding_id=? AND message_id=? "
                            "AND remote_identity=?",
                            (binding["id"], message_id, record.media_identity),
                        ).fetchone()
                        asset_id = previous["id"] if previous else str(uuid.uuid4())
                        asset = (asset_id, record)
                        if not media:
                            media = [
                                {
                                    "relative_path": f"{binding['chat_id']}/{asset_id}.jpg",
                                    "kind": "photo",
                                    "sha256": None,
                                    "status": "pending",
                                    "size": 0,
                                }
                            ]
            prepared.append((record.data, media))
            assets.append(asset)
        result = self.writer.apply(
            {
                "chat_id": binding["chat_id"],
                "source_root_id": binding["source_root_id"],
                "id": binding["id"],
                "policy": "preserve",
                "record_counters": False,
                "source": "telegram",
                "reason": "telegram",
            },
            prepared,
            conn,
        )
        for record, outcome, asset in zip(records, result["outcomes"], assets, strict=True):
            if outcome == "conflict":
                continue
            now = int(time.time())
            conn.execute(
                "INSERT INTO telegram_message_provenance VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(chat_id,message_id) DO UPDATE SET "
                "remote_edit_at=MAX(COALESCE(remote_edit_at,0),COALESCE(excluded.remote_edit_at,0)),"
                "observed_at=excluded.observed_at,verified_at=excluded.verified_at,"
                "semantic_hash=excluded.semantic_hash,remote_media_id=excluded.remote_media_id",
                (
                    binding["chat_id"],
                    record.data["id"],
                    binding["id"],
                    timestamp(record.data, "edited"),
                    now,
                    now,
                    content_hash(record.data, []),
                    record.media_identity,
                ),
            )
            conn.execute(
                "DELETE FROM telegram_sync_conflicts WHERE chat_id=? AND message_id=?",
                (binding["chat_id"], record.data["id"]),
            )
            conn.execute(
                "UPDATE telegram_jobs SET state='cancelled',lease_until=0 WHERE state IN "
                "('running','pending','failed') AND asset_id IN (SELECT id FROM telegram_assets "
                "WHERE chat_id=? AND message_id=? AND remote_identity IS NOT ?)",
                (binding["chat_id"], record.data["id"], record.media_identity),
            )
            conn.execute(
                "UPDATE telegram_assets SET state='superseded',reserved_bytes=0 WHERE chat_id=? "
                "AND message_id=? AND remote_identity IS NOT ?",
                (binding["chat_id"], record.data["id"], record.media_identity),
            )
            if asset:
                asset_id, _ = asset
                conn.execute(
                    "INSERT OR IGNORE INTO telegram_assets(id,binding_id,chat_id,message_id,"
                    "remote_identity,created_at,size) VALUES(?,?,?,?,?,?,?)",
                    (
                        asset_id,
                        binding["id"],
                        binding["chat_id"],
                        record.data["id"],
                        record.media_identity,
                        now,
                        record.expected_size,
                    ),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO "
                    "telegram_jobs(id,binding_id,kind,asset_id,idempotency_key) "
                    "VALUES(?,?,'media',?,?)",
                    (str(uuid.uuid4()), binding["id"], asset_id, f"media:{asset_id}"),
                )
        return result

    def delete_event(self, account_id, peer, ids):
        changed = 0
        with self.lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for message_id in ids:
                if peer:
                    rows = conn.execute(
                        "SELECT b.* FROM dialog_sync_bindings b WHERE b.account_user_id=? "
                        "AND b.peer_type=? AND b.peer_id=? AND b.enabled=1",
                        (account_id, peer.kind, peer.id),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT b.* FROM dialog_sync_bindings b JOIN messages m ON "
                        "m.chat_id=b.chat_id "
                        "WHERE b.account_user_id=? AND b.peer_type IN ('user','chat') "
                        "AND b.enabled=1 AND m.message_id=?",
                        (account_id, message_id),
                    ).fetchall()
                if len(rows) != 1 or not self.current(conn, rows[0]):
                    continue
                binding = rows[0]
                old = conn.execute(
                    "SELECT timestamp FROM messages WHERE chat_id=? AND message_id=?",
                    (binding["chat_id"], message_id),
                ).fetchone()
                conn.execute(
                    "INSERT OR IGNORE INTO telegram_tombstones(chat_id,message_id,account_user_id,"
                    "peer_type,peer_id,observed_at,policy) VALUES(?,?,?,?,?,?,?)",
                    (
                        binding["chat_id"],
                        message_id,
                        account_id,
                        binding["peer_type"],
                        binding["peer_id"],
                        int(time.time()),
                        binding["deletion_policy"],
                    ),
                )
                if not old:
                    continue
                conn.execute(
                    "UPDATE telegram_jobs SET state='cancelled',lease_until=0 WHERE asset_id IN "
                    "(SELECT id FROM telegram_assets WHERE chat_id=? AND message_id=?)",
                    (binding["chat_id"], message_id),
                )
                if binding["deletion_policy"] == "archive":
                    conn.execute(
                        "UPDATE messages SET remote_deleted=1 WHERE chat_id=? AND message_id=?",
                        (binding["chat_id"], message_id),
                    )
                else:
                    # Remove materialized text now; delayed Lance cleanup cannot expose it.
                    conn.execute(
                        "DELETE FROM chunks WHERE chat_id=? AND utc_day=?",
                        (binding["chat_id"], utc_day(old[0])),
                    )
                    conn.execute(
                        "DELETE FROM import_conflicts WHERE message_id=? AND import_id IN "
                        "(SELECT id FROM imports WHERE chat_id=?)",
                        (message_id, binding["chat_id"]),
                    )
                    conn.execute(
                        "DELETE FROM preview_entries WHERE message_id=? AND preview_id IN "
                        "(SELECT id FROM import_previews WHERE chat_id=?)",
                        (message_id, binding["chat_id"]),
                    )
                    conn.execute(
                        "DELETE FROM messages WHERE chat_id=? AND message_id=?",
                        (binding["chat_id"], message_id),
                    )
                conn.execute(
                    "UPDATE telegram_jobs SET state='cancelled',lease_until=0 WHERE asset_id IN "
                    "(SELECT id FROM telegram_assets WHERE chat_id=? AND message_id=?)",
                    (binding["chat_id"], message_id),
                )
                invalidate_segments(conn, binding["chat_id"], {utc_day(old[0])}, "telegram")
                bump_revision(conn, binding["chat_id"])
                changed += 1
        return changed

    def status(self, chat_id):
        binding = self.binding(chat_id)
        if not binding:
            return {"binding": None}
        with self.db.connect() as conn:
            cursor = dict(
                conn.execute(
                    "SELECT * FROM dialog_sync_cursors WHERE binding_id=?", (binding["id"],)
                ).fetchone()
            )
            run = conn.execute(
                "SELECT * FROM sync_runs WHERE binding_id=? ORDER BY rowid DESC "
                "LIMIT 1",
                (binding["id"],),
            ).fetchone()
            queues = {
                row["state"]: row["n"]
                for row in conn.execute(
                    "SELECT state,COUNT(*) n FROM telegram_assets WHERE binding_id=? GROUP BY "
                    "state",
                    (binding["id"],),
                )
            }
            conflicts = conn.execute(
                "SELECT COUNT(*) FROM telegram_sync_conflicts WHERE chat_id=?", (chat_id,)
            ).fetchone()[0]
        public = {
            key: binding[key]
            for key in (
                "id",
                "chat_id",
                "peer_type",
                "peer_id",
                "enabled",
                "download_media",
                "deletion_policy",
                "reconcile_days",
                "revision",
                "last_success_at",
                "error_code",
            )
        }
        return {
            "binding": public,
            "cursor": cursor,
            "run": self.public_run(run) if run else None,
            "media": queues,
            "conflicts": conflicts,
            "error": ERRORS.get(binding["error_code"]),
        }

    @staticmethod
    def public_run(row):
        result = dict(row)
        result["error"] = ERRORS.get(result["error_code"])
        return result

    def shutdown(self):
        # Cancellation of to_thread does not stop its worker. Fence delayed writes
        # before the ImportService releases the process-wide workspace lease.
        with self.writer_lock, self.db.connect() as conn:
            self.closed = True
            conn.execute(
                "UPDATE telegram_jobs SET state='pending',lease_until=0 WHERE state='running'"
            )
            conn.execute("UPDATE sync_runs SET state='queued' WHERE state='running'")
            conn.execute("UPDATE telegram_assets SET reserved_bytes=0")
