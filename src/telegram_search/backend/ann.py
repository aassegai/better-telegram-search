"""Nonblocking ANN status and local-session-controlled preparation."""

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/api/ann-indexes", tags=["search"])


class PrepareAnn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    space_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,200}$")


@router.get("")
async def ann_status(request: Request):
    return request.app.state.ann_indexes.status()


@router.post("/prepare", status_code=202)
def prepare_ann(body: PrepareAnn, request: Request):
    return request.app.state.ann_indexes.prepare(body.space_id)


@router.post("/cancel")
def cancel_ann(request: Request):
    # Persist disabling automatic retries as part of the explicit stop action.
    # This setting is live and does not acquire the archive/index lifecycle lock.
    request.app.state.workspace.update_settings({"ann_indexing_enabled": False})
    return request.app.state.ann_indexes.status()
