"""Single private session owner; short-lived login secrets stay in memory."""

import asyncio
import importlib.util
import os
import time
import uuid
from dataclasses import dataclass, field

from filelock import FileLock, Timeout

from telegram_search.shared.errors import UserError
from telegram_search.telegram_sync.adapter import TelethonAdapter, safe_failure
from telegram_search.telegram_sync.models import ERRORS, SourceFailure
from telegram_search.telegram_sync.paths import owned_path


@dataclass
class AuthFlow:
    id: str
    expires_at: float
    phone: str = field(repr=False)
    code_hash: str = field(repr=False)
    stage: str = "awaiting_code"


class TelegramConnectionManager:
    def __init__(self, db, settings, factory=None):
        self.db, self.settings = db, settings
        self.factory = factory or TelethonAdapter
        self.runtime_installed = (
            factory is not None or importlib.util.find_spec("telethon") is not None
        )
        self.client = None
        self.flow = None
        self.lease = None
        self.lock = asyncio.Lock()
        self.handler = None
        self.on_connected = None
        self.closed = False
        self.dialog_choices = {}
        self.rpc_tasks = set()
        self.configuration_error = None

    async def rpc(self, awaitable):
        task = asyncio.ensure_future(awaitable)
        self.rpc_tasks.add(task)
        try:
            return await asyncio.wait_for(task, timeout=45)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            # Cancelling a network operation must not kill its long-lived
            # scheduler owner. The caller releases its lock through normal error handling.
            raise SourceFailure("cancelled") from None
        finally:
            self.rpc_tasks.discard(task)

    async def cancel_network_operations(self):
        tasks = [task for task in self.rpc_tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def row(self):
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM telegram_connections WHERE slot=1").fetchone()
            return dict(row) if row else None

    def status(self):
        row = self.row()
        # Deliberate allowlist: no path, phone, access hash or SDK/session state.
        return {
            "configured": self.settings.configured,
            "runtime_installed": self.runtime_installed,
            "state": self.flow.stage if self.flow else (row["state"] if row else "disconnected"),
            "account_user_id": row["account_user_id"] if row else None,
            "retry_after": row["retry_after"] if row else 0,
            "error": self.configuration_error or (ERRORS.get(row["error_code"]) if row else None),
            "error_code": row["error_code"] if row else None,
            "connected": bool(self.client and self.client.is_connected() and not self.flow),
        }

    def update(self, state, error=None, retry_after=0, reconnect=None):
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE telegram_connections SET state=?,error_code=?,retry_after=?,updated_at=? "
                + (",reconnect=? " if reconnect is not None else "")
                + "WHERE slot=1",
                (
                    state,
                    error,
                    retry_after,
                    int(time.time()),
                    *([int(reconnect)] if reconnect is not None else []),
                ),
            )

    def ensure_row(self):
        with self.db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO telegram_connections(id,created_at,updated_at) "
                "VALUES(?,?,?)",
                (str(uuid.uuid4()), int(time.time()), int(time.time())),
            )
        return self.row()

    def session_path(self):
        row = self.ensure_row()
        root = owned_path(self.db.workspace, "private/telegram")
        for path in (root.parent, root):
            if path.is_symlink() or not path.resolve().is_relative_to(self.db.workspace):
                raise SourceFailure("storage")
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name != "nt":
                path.chmod(0o700)
        path = owned_path(self.db.workspace, root / f"{row['id']}.session")
        for candidate in (
            path,
            *[path.with_name(path.name + tail) for tail in ("-journal", "-wal", "-shm")],
        ):
            owned_path(self.db.workspace, candidate)
        owned_path(self.db.workspace, path.with_suffix(".owner.lock"))
        path.touch(mode=0o600, exist_ok=True)
        if os.name != "nt":
            path.chmod(0o600)
        return path

    async def _open(self):
        if self.client:
            return
        if not self.settings.configured:
            raise SourceFailure("not_configured")
        if not self.runtime_installed:
            raise SourceFailure("runtime_missing")
        path = self.session_path()
        lease = FileLock(path.with_suffix(".owner.lock"), timeout=0)
        try:
            lease.acquire()
        except Timeout as exc:
            raise SourceFailure("session_busy") from exc
        self.lease = lease
        try:
            self.client = self.factory(path, self.settings)
            if self.handler:
                self.client.set_handler(self.handler)
            await asyncio.wait_for(self.client.connect(), timeout=30)
        except BaseException:
            await self._close_client()
            raise

    async def _close_client(self):
        client, self.client = self.client, None
        try:
            if client:
                await asyncio.wait_for(client.disconnect(), timeout=15)
        except Exception:
            pass
        finally:
            if self.lease:
                self.lease.release()
                self.lease = None
            self.dialog_choices.clear()

    async def restore(self):
        async with self.lock:
            row = self.row()
            if self.closed or self.flow or not row or not row["reconnect"]:
                return False
            if row["retry_after"] > time.time():
                return False
            try:
                self.update("connecting")
                await self._open()
                if not await self.rpc(self.client.authorized()):
                    raise SourceFailure("auth_required")
                await self._ready()
                return True
            except Exception as exc:
                failure = safe_failure(exc)
                await self._close_client()
                self.update(
                    "auth_required" if failure.code == "auth_required" else "degraded",
                    failure.code,
                    int(time.time()) + max(15, failure.retry_seconds),
                    reconnect=failure.code != "auth_required",
                )
                return False

    async def _ready(self):
        account = await self.rpc(self.client.account_id())
        row = self.row()
        with self.db.connect() as conn:
            has_bindings = conn.execute("SELECT 1 FROM dialog_sync_bindings LIMIT 1").fetchone()
            if row["account_user_id"] not in (None, account) and has_bindings:
                raise SourceFailure("account_mismatch")
            conn.execute(
                "UPDATE telegram_connections SET account_user_id=?,reconnect=1 WHERE slot=1",
                (account,),
            )
        if self.flow:
            self.flow.stage = "catching_up"
        self.update("catching_up")
        await self.rpc(self.client.catch_up())
        self.flow = None
        self.update("live", reconnect=True)
        if self.on_connected:
            self.on_connected()

    async def auth_start(self, phone):
        async with self.lock:
            if self.closed:
                raise UserError("Приложение останавливается.")
            row = self.row()
            if row and row["retry_after"] > time.time():
                raise UserError(ERRORS["waiting_rate_limit"])
            if self.client and not self.flow and await self.rpc(self.client.authorized()):
                raise UserError("Telegram уже подключён.")
            self.flow = None
            await self._close_client()
            try:
                self.ensure_row()
                self.update("connecting", reconnect=False)
                await self._open()
                if await self.rpc(self.client.authorized()):
                    await self._ready()
                    return {"state": "live"}
                code_hash = await self.rpc(self.client.request_code(phone))
                self.flow = AuthFlow(uuid.uuid4().hex, time.monotonic() + 300, phone, code_hash)
                self.update("awaiting_code", reconnect=False)
                return self.flow_status()
            except Exception as exc:
                await self._auth_failure(exc)

    def flow_status(self):
        return {"flow_id": self.flow.id, "state": self.flow.stage, "expires_in": 300}

    def _validate_flow(self, flow_id, stage):
        if (
            not self.flow
            or self.flow.id != flow_id
            or self.flow.stage != stage
            or self.flow.expires_at <= time.monotonic()
        ):
            raise UserError("Вход истёк или отменён. Начните заново.")

    async def auth_code(self, flow_id, code):
        async with self.lock:
            self._validate_flow(flow_id, "awaiting_code")
            try:
                ready = await self.rpc(
                    self.client.login_code(self.flow.phone, code, self.flow.code_hash)
                )
                if not ready:
                    self.flow.stage = "awaiting_2fa"
                    self.flow.phone = self.flow.code_hash = ""
                    self.update("awaiting_2fa")
                    return self.flow_status()
                await self._ready()
                return {"state": "live"}
            except Exception as exc:
                await self._auth_failure(exc)

    async def auth_password(self, flow_id, password):
        async with self.lock:
            self._validate_flow(flow_id, "awaiting_2fa")
            try:
                await self.rpc(self.client.login_password(password))
                await self._ready()
                return {"state": "live"}
            except Exception as exc:
                await self._auth_failure(exc)

    async def _auth_failure(self, exc):
        failure = safe_failure(exc)
        if failure.code == "cancelled":
            raise UserError("Вход истёк или отменён. Начните заново.") from None
        keep = failure.code in {"invalid_code", "invalid_password"} and self.flow is not None
        if not keep:
            self.flow = None
            await self._close_client()
        self.update(
            self.flow.stage if keep else "auth_required",
            failure.code,
            int(time.time()) + failure.retry_seconds if failure.retry_seconds else 0,
            reconnect=False,
        )
        raise UserError(ERRORS.get(failure.code, ERRORS["unexpected"])) from None

    async def cancel_auth(self, flow_id):
        # Cancel the RPC before waiting for the auth lock. A late successful
        # sign-in must not turn a cancelled dialog into a persistent session.
        if self.flow and self.flow.id == flow_id:
            await self.cancel_network_operations()
        async with self.lock:
            if self.flow and self.flow.id == flow_id:
                self.flow = None
                await self._close_client()
                self._remove_session()
                self.update("disconnected", reconnect=False)
        return self.status()

    async def expire_flow(self):
        if self.flow and self.flow.expires_at <= time.monotonic():
            await self.cancel_auth(self.flow.id)

    async def disconnect(self):
        await self.cancel_network_operations()
        async with self.lock:
            self.flow = None
            self.update("disconnected", reconnect=False)
            await self._close_client()
        return self.status()

    def _remove_session(self):
        row = self.row()
        if not row:
            return
        path = owned_path(self.db.workspace, f"private/telegram/{row['id']}.session")
        owned_path(self.db.workspace, path.with_suffix(".owner.lock"))
        if not path.parent.is_dir():
            return
        try:
            with FileLock(path.with_suffix(".owner.lock"), timeout=0):
                for suffix in ("", "-journal", "-wal", "-shm"):
                    candidate = owned_path(self.db.workspace, path.with_name(path.name + suffix))
                    candidate.unlink(missing_ok=True)
        except Timeout:
            raise SourceFailure("session_busy") from None

    async def logout(self):
        await self.cancel_network_operations()
        async with self.lock:
            revoked = False
            try:
                if not self.client and self.settings.configured:
                    await self._open()
                if self.client:
                    revoked = await asyncio.wait_for(self.client.logout(), timeout=20)
            except Exception:
                pass
            self.flow = None
            await self._close_client()
            self._remove_session()
            self.update("disconnected", reconnect=False)
        return {**self.status(), "server_revocation_confirmed": revoked}

    async def require_client(self):
        if not self.client or self.flow or not self.client.is_connected():
            raise UserError("Сначала подключите Telegram.")
        row = self.row()
        if not row or row["state"] not in {"live", "catching_up", "waiting_rate_limit"}:
            raise UserError("Сначала подключите Telegram.")
        if row["retry_after"] > time.time():
            raise UserError(ERRORS["waiting_rate_limit"])
        return self.client

    async def dialogs(self, cursor="", query=""):
        client = await self.require_client()
        try:
            peers, next_cursor = await self.rpc(client.dialogs(cursor, query))
            choices = []
            for peer in peers:
                self.dialog_choices[peer.key] = peer
                choices.append(
                    {"key": peer.key, "peer_type": peer.kind, "peer_id": peer.id, "name": peer.name}
                )
            # Bounded choice cache. Previously bound peers retain access hashes in SQLite.
            while len(self.dialog_choices) > 500:
                self.dialog_choices.pop(next(iter(self.dialog_choices)))
            return {"dialogs": choices, "next_cursor": next_cursor}
        except Exception as exc:
            failure = safe_failure(exc)
            if failure.retry_seconds:
                self.update(
                    "waiting_rate_limit", failure.code, int(time.time()) + failure.retry_seconds
                )
            raise UserError(ERRORS.get(failure.code, ERRORS["unexpected"])) from None

    async def close(self):
        self.closed = True
        await self.cancel_network_operations()
        async with self.lock:
            self.flow = None
            await self._close_client()
