"""Bounded scanners and durable leases; live events never move history coverage."""

import asyncio
import random
import time

from telegram_search.shared.errors import UserError
from telegram_search.telegram_sync.adapter import normalize_message, peer_of, safe_failure
from telegram_search.telegram_sync.bindings import DialogBindingService
from telegram_search.telegram_sync.connection import TelegramConnectionManager
from telegram_search.telegram_sync.media import MediaDownloadWorker
from telegram_search.telegram_sync.models import Peer, SourceFailure
from telegram_search.telegram_sync.settings import SyncSettings
from telegram_search.telegram_sync.store import SyncStore


class SyncCoordinator:
    def __init__(self, db, lifecycle_lock, *, factory=None, settings=None):
        self.db = db
        configuration_error = None
        try:
            self.settings = settings or SyncSettings.load(db.workspace)
        except UserError as exc:
            self.settings = SyncSettings()
            configuration_error = str(exc)
        self.store = SyncStore(db, lifecycle_lock)
        self.connection = TelegramConnectionManager(db, self.settings, factory)
        self.connection.configuration_error = configuration_error
        self.connection.handler = self.receive
        self.connection.on_connected = self.connected
        self.bindings = DialogBindingService(self.store, self.connection)
        self.media = MediaDownloadWorker(self.store, self.settings)
        self.tasks = []
        self.wake = asyncio.Event()
        self.closed = False
        self.degraded = False
        self.loop = None

    def start(self):
        if self.tasks or self.closed:
            return
        self.loop = asyncio.get_running_loop()
        self.store.recover()
        self.tasks = [
            asyncio.create_task(self._schedule(), name="telegram-schedule"),
            asyncio.create_task(self._worker("scan"), name="telegram-scanner"),
        ]
        self.tasks += [
            asyncio.create_task(self._worker("media"), name=f"telegram-media-{number}")
            for number in range(self.settings.media_download_concurrency)
        ]

    def start_threadsafe(self):
        if self.loop and not self.closed:
            self.loop.call_soon_threadsafe(self.start)

    def connected(self):
        self.degraded = False
        with self.db.connect() as conn:
            ids = [
                row[0]
                for row in conn.execute("SELECT id FROM dialog_sync_bindings WHERE enabled=1")
            ]
        for binding_id in ids:
            self.store.request(binding_id, "reconnect")
        self.wake.set()

    async def _schedule(self):
        next_gc = 0
        while not self.closed:
            try:
                await self.connection.expire_flow()
                row = self.connection.row()
                if row and row["reconnect"] and not self.connection.flow:
                    if not self.connection.client or not self.connection.client.is_connected():
                        await self.connection.restore()
                    if (
                        self.connection.client
                        and self.connection.client.is_connected()
                        and row["retry_after"] <= time.time()
                        and not self.degraded
                    ):
                        interval = self.settings.reconcile_interval_minutes * 60
                        await asyncio.to_thread(
                            self.store.schedule, interval + random.randint(0, 30)
                        )
                if time.time() >= next_gc:
                    await asyncio.to_thread(self.media.collect_orphans)
                    next_gc = time.time() + 3600
            except asyncio.CancelledError:
                raise
            except Exception:
                # No raw SDK/SQLite exceptions or private payloads in logs/statuses.
                await self._degrade("storage")
            await self._wait(1)

    async def _wait(self, seconds):
        try:
            await asyncio.wait_for(self.wake.wait(), timeout=seconds)
            self.wake.clear()
        except TimeoutError:
            pass

    async def _worker(self, kind):
        while not self.closed:
            claimed = None
            try:
                row = self.connection.row()
                if (
                    self.connection.client
                    and self.connection.client.is_connected()
                    and not self.connection.flow
                    and not self.degraded
                    and row
                    and row["state"] in {"live", "catching_up", "waiting_rate_limit"}
                    and row["retry_after"] <= time.time()
                ):
                    claimed = await asyncio.to_thread(self.store.claim, kind)
                if not claimed:
                    await self._wait(1)
                    continue
                job, binding = claimed
                if kind == "scan":
                    await self.scan(job, binding)
                else:
                    await self.media.run(job, binding, self.connection.client)
                self.wake.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if claimed:
                    await self.failed(*claimed, exc)
                else:
                    await self._degrade("storage")
                await self._wait(1)

    async def receive(self, kind, value, ids):
        if (
            self.closed
            or self.degraded
            or (self.connection.flow and self.connection.flow.stage != "catching_up")
        ):
            return
        try:
            row = self.connection.row()
            if not row or not row["reconnect"] or row["account_user_id"] is None:
                return
            if kind in {"message", "raw_message"}:
                peer = value.peer if kind == "message" else peer_of(value.peer_id)
                binding = await asyncio.to_thread(self.store.by_peer, row["account_user_id"], peer)
                if binding:
                    record = value if kind == "message" else normalize_message(value)
                    await asyncio.to_thread(self.store.merge, binding, [record])
            elif kind == "delete":
                await asyncio.to_thread(self.store.delete_event, row["account_user_id"], value, ids)
            self.wake.set()
        except Exception:
            # Stop reception on a failed durable write. SDK acknowledgements alone are not coverage.
            self.degraded = True
            task = asyncio.create_task(self._degrade("storage"))
            self.tasks.append(task)

    async def _degrade(self, code):
        self.degraded = True
        try:
            self.connection.update("degraded", code, int(time.time()) + 15)
        except Exception:
            pass
        async with self.connection.lock:
            await self.connection._close_client()

    async def _call(self, awaitable):
        return await asyncio.wait_for(awaitable, timeout=45)

    def cursor(self, binding):
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM dialog_sync_cursors WHERE binding_id=?", (binding["id"],)
            ).fetchone()
            return dict(row) if row else None

    def _upper(self, job, binding, upper):
        with self.store.lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self.store.current(conn, binding) or not self.store.job_current(conn, job):
                return False
            conn.execute(
                "UPDATE dialog_sync_cursors SET upper_bound=? WHERE binding_id=? AND upper_bound "
                "IS NULL",
                (upper, binding["id"]),
            )
        return True

    def _tail(self, job, binding, upper):
        with self.store.lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self.store.current(conn, binding) or not self.store.job_current(conn, job):
                return False
            conn.execute(
                "UPDATE dialog_sync_cursors SET "
                "recent_upper_bound=?,recent_since=?,recent_offset_id=0 "
                "WHERE binding_id=? AND recent_upper_bound IS NULL",
                (upper, int(time.time()) - binding["reconcile_days"] * 86400, binding["id"]),
            )
        return True

    async def scan(self, job, binding):
        client, peer = self.connection.client, Peer.from_binding(binding)
        if client is None:
            return await asyncio.to_thread(self._partial, job, binding)
        cursor = await asyncio.to_thread(self.cursor, binding)
        if not cursor:
            return
        if cursor["upper_bound"] is None:
            upper = await self._call(client.latest(peer))
            if not await asyncio.to_thread(self._upper, job, binding, upper):
                return await asyncio.to_thread(self._partial, job, binding)
            cursor = await asyncio.to_thread(self.cursor, binding)
        upper = cursor["upper_bound"]
        # Small point overlap also catches edits around a committed restart boundary.
        with self.db.connect() as conn:
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT message_id FROM messages WHERE chat_id=? AND message_id<=? "
                    "ORDER BY message_id DESC LIMIT 20",
                    (binding["chat_id"], cursor["scanned_through_id"]),
                )
            ]
        if ids:
            overlap = await self._call(client.get(peer, ids))
            if (
                await asyncio.to_thread(
                    self.store.merge, binding, overlap, run_id=job["run_id"], job=job
                )
                is None
            ):
                return await asyncio.to_thread(self._partial, job, binding)
        budget = self.settings.pages_per_run
        while cursor["scanned_through_id"] < upper and budget:
            await asyncio.to_thread(self.store.renew, job)
            records = await self._call(client.page(peer, cursor["scanned_through_id"], upper, 100))
            ids = [record.data["id"] for record in records]
            if ids != sorted(set(ids)) or any(
                not cursor["scanned_through_id"] < mid <= upper for mid in ids
            ):
                raise SourceFailure("unexpected")
            through = upper if len(records) < 100 else ids[-1]
            result = await asyncio.to_thread(
                self.store.merge, binding, records, run_id=job["run_id"], cursor=through, job=job
            )
            if result is None:
                return await asyncio.to_thread(self._partial, job, binding)
            cursor["scanned_through_id"] = through
            budget -= 1
            await asyncio.sleep(0.2)
        if cursor["scanned_through_id"] < upper:
            return await asyncio.to_thread(self._partial, job, binding)
        if not await asyncio.to_thread(self._tail, job, binding, upper):
            return await asyncio.to_thread(self._partial, job, binding)
        cursor = await asyncio.to_thread(self.cursor, binding)
        while budget:
            await asyncio.to_thread(self.store.renew, job)
            records = await self._call(
                client.recent(
                    peer,
                    cursor["recent_offset_id"],
                    cursor["recent_since"],
                    cursor["recent_upper_bound"],
                    100,
                )
            )
            ids = [record.data["id"] for record in records]
            if ids != sorted(set(ids), reverse=True) or any(
                mid > cursor["recent_upper_bound"]
                or (cursor["recent_offset_id"] and mid >= cursor["recent_offset_id"])
                for mid in ids
            ):
                raise SourceFailure("unexpected")
            offset = ids[-1] if ids else cursor["recent_offset_id"]
            result = await asyncio.to_thread(
                self.store.merge,
                binding,
                records,
                run_id=job["run_id"],
                recent_offset=offset,
                job=job,
            )
            if result is None:
                return await asyncio.to_thread(self._partial, job, binding)
            if len(records) < 100:
                return await asyncio.to_thread(self._complete, job, binding)
            cursor["recent_offset_id"] = offset
            budget -= 1
            await asyncio.sleep(0.2)
        await asyncio.to_thread(self._partial, job, binding)

    def _partial(self, job, binding):
        with self.store.lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self.store.job_current(conn, job):
                return
            conn.execute(
                "UPDATE telegram_jobs SET state='pending',available_at=?,lease_until=0 "
                "WHERE id=? AND attempt=? AND state='running'",
                (int(time.time()) + 5, job["id"], job["attempt"]),
            )
            conn.execute(
                "UPDATE sync_runs SET state='partial' WHERE id=? AND state='running'",
                (job["run_id"],),
            )

    def _complete(self, job, binding):
        with self.store.lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self.store.job_current(conn, job):
                return
            if not self.store.current(conn, binding):
                conn.execute(
                    "UPDATE telegram_jobs SET state='pending',lease_until=0 WHERE id=?",
                    (job["id"],),
                )
                conn.execute(
                    "UPDATE sync_runs SET state='partial' WHERE id=? AND state='running'",
                    (job["run_id"],),
                )
                return
            now = int(time.time())
            conn.execute(
                "UPDATE dialog_sync_cursors SET upper_bound=NULL,recent_upper_bound=NULL,"
                "recent_offset_id=NULL,recent_since=NULL,reconciled_at=? WHERE binding_id=?",
                (now, binding["id"]),
            )
            conn.execute(
                "UPDATE dialog_sync_bindings SET last_success_at=?,error_code=NULL,next_sync_at=? "
                "WHERE id=?",
                (
                    now,
                    now + self.settings.reconcile_interval_minutes * 60 + random.randint(0, 30),
                    binding["id"],
                ),
            )
            conn.execute(
                "UPDATE sync_runs SET "
                "state='completed',finished_at=?,error_code=NULL,retry_after=0 WHERE id=?",
                (now, job["run_id"]),
            )
            conn.execute(
                "UPDATE telegram_jobs SET state='done',lease_until=0,error_code=NULL WHERE id=?",
                (job["id"],),
            )

    async def failed(self, job, binding, exc):
        failure = safe_failure(exc)
        code = failure.code
        if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
            code = "disk_full"
        elif type(exc).__module__ == "sqlite3":
            code = "storage"
        now = int(time.time())
        retry = code in {"network", "waiting_rate_limit"} and (
            job["attempt"] < 8 or code == "waiting_rate_limit"
        )
        delay = failure.retry_seconds or min(300, 2 ** min(job["attempt"], 8)) + random.randint(
            0, 3
        )
        try:
            with self.store.lock, self.db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if not self.store.job_current(conn, job):
                    return
                if not self.store.current(conn, binding):
                    conn.execute(
                        "UPDATE telegram_jobs SET state='pending',lease_until=0 WHERE id=?",
                        (job["id"],),
                    )
                    return
                conn.execute(
                    "UPDATE telegram_jobs SET state=?,available_at=?,lease_until=0,error_code=? "
                    "WHERE id=? AND state='running'",
                    ("pending" if retry else "failed", now + delay, code, job["id"]),
                )
                conn.execute(
                    "UPDATE dialog_sync_bindings SET error_code=? WHERE id=?", (code, binding["id"])
                )
                if job["run_id"]:
                    conn.execute(
                        "UPDATE sync_runs SET state=?,error_code=?,retry_after=? WHERE id=? "
                        "AND state='running'",
                        (
                            "waiting_rate_limit" if retry else "failed",
                            code,
                            now + delay if retry else 0,
                            job["run_id"],
                        ),
                    )
                if job["asset_id"]:
                    conn.execute(
                        "UPDATE telegram_assets SET state=?,error_code=? WHERE id=?",
                        ("pending" if retry else "failed", code, job["asset_id"]),
                    )
            if code == "waiting_rate_limit":
                self.connection.update("waiting_rate_limit", code, now + delay)
            elif code == "auth_required":
                self.connection.update("auth_required", code, reconnect=False)
                async with self.connection.lock:
                    await self.connection._close_client()
            elif code == "storage":
                await self._degrade(code)
        except Exception:
            await self._degrade("storage")

    def retry(self, chat_id):
        binding = self.store.binding(chat_id)
        if not binding:
            return None
        with self.store.lock, self.db.connect() as conn:
            conn.execute(
                "UPDATE telegram_jobs SET state='pending',attempt=0,available_at=0,error_code=NULL "
                "WHERE binding_id=? AND kind='media' AND state='failed'",
                (binding["id"],),
            )
            conn.execute(
                "UPDATE telegram_assets SET state='pending',error_code=NULL WHERE binding_id=? "
                "AND state='failed'",
                (binding["id"],),
            )
            conn.execute(
                "UPDATE dialog_sync_bindings SET error_code=NULL,next_sync_at=0 WHERE id=?",
                (binding["id"],),
            )
        return self.store.request(binding["id"], "retry")

    async def close(self):
        self.closed = True
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.connection.close()
        # In-flight writes ran under lifecycle_lock and finish before releasing the workspace lease.
        await asyncio.to_thread(self.store.shutdown)
