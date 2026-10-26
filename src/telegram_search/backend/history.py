"""Local-session APIs for persistent searches; restore never invokes inference."""

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/search-history", tags=["search"])


class FavoriteRequest(BaseModel):
    favorite: bool = Field(strict=True)


@router.get("")
def list_history(
    request: Request,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    return request.app.state.search_history.list(offset=offset, limit=limit)


@router.delete("/collections/{kind}", status_code=204)
def clear_collection(kind: Literal["history", "favorites", "cache"], request: Request):
    state = request.app.state
    with state.importer.lifecycle_lock:
        if kind == "cache":
            state.search_jobs.clear_results()
            state.search_cache.clear()
        state.search_history.clear(kind)


@router.get("/{history_id}")
def restore_history(
    history_id: str,
    request: Request,
    offset: Annotated[int, Query(ge=0, le=100)] = 0,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    cursor: Annotated[str | None, Query(max_length=4096)] = None,
    snapshot_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
):
    try:
        return request.app.state.search_history.get(
            history_id, offset=offset, limit=limit, cursor=cursor, snapshot_id=snapshot_id
        )
    except KeyError:
        raise HTTPException(404, "Сохранённый поиск не найден.") from None


@router.post("/{history_id}/favorite", status_code=204)
def favorite(history_id: str, body: FavoriteRequest, request: Request):
    try:
        request.app.state.search_history.favorite(history_id, body.favorite)
    except KeyError:
        raise HTTPException(404, "Сохранённый поиск не найден.") from None


@router.get("/{history_id}/status")
def history_status(
    history_id: str,
    request: Request,
    snapshot_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
):
    try:
        return request.app.state.search_history.metadata(history_id, snapshot_id=snapshot_id)
    except KeyError:
        raise HTTPException(404, "Сохранённый поиск не найден.") from None
