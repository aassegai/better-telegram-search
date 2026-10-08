"""Local, CSRF-protected controls; login payloads are never echoed in validation errors."""

import asyncio
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from telegram_search.shared.errors import UserError

router = APIRouter()


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginStart(StrictBody):
    phone: str = Field(pattern=r"^\+[0-9]{6,15}$", max_length=16, repr=False)


class LoginCode(StrictBody):
    code: str = Field(min_length=1, max_length=16, repr=False)


class LoginPassword(StrictBody):
    password: str = Field(min_length=1, max_length=256, repr=False)


class BindingPreview(StrictBody):
    peer_key: str = Field(pattern=r"^(user|chat|channel):[1-9][0-9]{0,19}$")
    chat_id: str | None = Field(default=None, max_length=64)
    start_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class BindingApply(StrictBody):
    preview_token: str = Field(min_length=24, max_length=128)
    confirm_account: bool
    expected_revision: int | None = Field(default=None, ge=0, strict=True)


class SyncSettingsRequest(StrictBody):
    expected_revision: int = Field(ge=1, strict=True)
    enabled: bool = True
    download_media: bool = True
    deletion_policy: Literal["archive", "mirror"] = "archive"
    reconcile_days: int = Field(default=7, ge=1, le=30, strict=True)


class BindingRemove(StrictBody):
    expected_revision: int = Field(ge=1, strict=True)


def sync(request):
    return request.app.state.telegram


@router.get("/api/telegram/connection")
def connection(request: Request):
    return sync(request).connection.status()


@router.post("/api/telegram/auth/start")
async def auth_start(request: Request, body: LoginStart):
    return await sync(request).connection.auth_start(body.phone)


@router.post("/api/telegram/auth/{flow_id}/code")
async def auth_code(request: Request, flow_id: str, body: LoginCode):
    return await sync(request).connection.auth_code(flow_id, body.code)


@router.post("/api/telegram/auth/{flow_id}/password")
async def auth_password(request: Request, flow_id: str, body: LoginPassword):
    return await sync(request).connection.auth_password(flow_id, body.password)


@router.delete("/api/telegram/auth/{flow_id}")
async def cancel_auth(request: Request, flow_id: str):
    return await sync(request).connection.cancel_auth(flow_id)


@router.post("/api/telegram/connect")
async def connect(request: Request):
    manager = sync(request).connection
    if manager.row():
        manager.update("connecting", reconnect=True)
        await manager.restore()
    return manager.status()


@router.post("/api/telegram/disconnect")
async def disconnect(request: Request):
    return await sync(request).connection.disconnect()


@router.post("/api/telegram/logout")
async def logout(request: Request):
    return await sync(request).connection.logout()


@router.get("/api/telegram/dialogs")
async def dialogs(request: Request, cursor: str = "", q: str = ""):
    if len(cursor) > 128 or len(q) > 200:
        raise UserError("Слишком длинный фильтр списка диалогов.")
    return await sync(request).connection.dialogs(cursor, q)


@router.post("/api/telegram/bindings/preview")
async def binding_preview(request: Request, body: BindingPreview):
    return await sync(request).bindings.preview(body.peer_key, body.chat_id, body.start_date)


@router.post("/api/telegram/bindings")
async def binding_apply(request: Request, body: BindingApply):
    service = sync(request)
    result = await asyncio.to_thread(
        service.bindings.apply, body.preview_token, body.confirm_account, body.expected_revision
    )
    service.wake.set()
    return result


@router.get("/api/chats/{chat_id}/telegram")
def chat_status(request: Request, chat_id: str):
    service = sync(request)
    return {
        **service.store.status(chat_id),
        "connection_state": service.connection.status()["state"],
    }


@router.patch("/api/chats/{chat_id}/sync-settings")
async def sync_settings(request: Request, chat_id: str, body: SyncSettingsRequest):
    service = sync(request)
    changes = body.model_dump(exclude_unset=True, exclude={"expected_revision"})
    result = await asyncio.to_thread(
        service.bindings.settings, chat_id, body.expected_revision, changes
    )
    service.wake.set()
    return result


@router.delete("/api/chats/{chat_id}/telegram-binding")
async def remove_binding(request: Request, chat_id: str, body: BindingRemove):
    return await asyncio.to_thread(sync(request).bindings.remove, chat_id, body.expected_revision)


@router.post("/api/chats/{chat_id}/sync", status_code=202)
async def update_now(request: Request, chat_id: str):
    service = sync(request)
    binding = await asyncio.to_thread(service.store.binding, chat_id)
    if not binding or not binding["enabled"]:
        raise UserError("Сначала включите синхронизацию этого диалога.")
    run_id = await asyncio.to_thread(service.store.request, binding["id"])
    service.wake.set()
    return {"run_id": run_id}


@router.post("/api/chats/{chat_id}/sync/retry", status_code=202)
async def retry(request: Request, chat_id: str):
    service = sync(request)
    run_id = await asyncio.to_thread(service.retry, chat_id)
    service.wake.set()
    return {"run_id": run_id}


@router.get("/api/sync/runs/{run_id}")
def run_status(request: Request, run_id: str):
    service = sync(request)
    with service.db.connect() as conn:
        row = conn.execute("SELECT * FROM sync_runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise UserError("Задача синхронизации не найдена.")
    return service.store.public_run(row)
