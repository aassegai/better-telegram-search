"""Local tab persistence; jobs still share the bounded search scheduler."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from telegram_search.backend.history import FavoriteRequest
from telegram_search.search.jobs import SearchQueueFull
from telegram_search.search.spec import SearchSpec
from telegram_search.search.tabs import SearchDraft, TabConflict, TabPatch, TabView

router = APIRouter(prefix="/api/search-tabs", tags=["search"])


class NewTab(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft: SearchDraft | None = None
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=64)
    view: TabView | None = None


def invoke(request, method, *args, **kwargs):
    try:
        return getattr(request.app.state.search_tabs, method)(*args, **kwargs)
    except KeyError:
        raise HTTPException(404, "Вкладка или сохранённая выдача не найдена.") from None
    except TabConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except SearchQueueFull as exc:
        raise HTTPException(429, str(exc)) from None


@router.get("")
def list_tabs(request: Request):
    return invoke(request, "list")


@router.post("", status_code=201)
def create_tab(body: NewTab, request: Request):
    return invoke(request, "create", body.draft, snapshot_id=body.snapshot_id, view=body.view)


@router.patch("/{tab_id}")
def patch_tab(tab_id: str, body: TabPatch, request: Request):
    return invoke(request, "patch", tab_id, body)


@router.get("/{tab_id}")
def tab_status(tab_id: str, request: Request):
    return invoke(request, "metadata", tab_id)


@router.post("/{tab_id}/activate", status_code=204)
def activate_tab(tab_id: str, request: Request):
    invoke(request, "activate", tab_id)


@router.delete("/{tab_id}", status_code=204)
def close_tab(tab_id: str, request: Request):
    invoke(request, "close", tab_id)


@router.post("/{tab_id}/jobs", status_code=202)
def search(tab_id: str, body: SearchSpec, request: Request):
    return invoke(request, "start", tab_id, body)


@router.get("/{tab_id}/result")
def tab_result(
    tab_id: str,
    request: Request,
    offset: Annotated[int, Query(ge=0, le=100)] = 0,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    cursor: Annotated[str | None, Query(max_length=4096)] = None,
    snapshot_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
):
    return invoke(
        request,
        "result",
        tab_id,
        offset=offset,
        limit=limit,
        cursor=cursor,
        snapshot_id=snapshot_id,
    )


@router.post("/{tab_id}/favorite")
def favorite(
    tab_id: str,
    body: FavoriteRequest,
    request: Request,
    snapshot_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
):
    return invoke(request, "favorite", tab_id, body.favorite, snapshot_id=snapshot_id)
