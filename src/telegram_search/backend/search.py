"""Search jobs use the same local-session mutation guard as the other routers."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from telegram_search.search.jobs import SearchNotReady, SearchQueueFull, SearchSnapshotExpired
from telegram_search.search.spec import SearchSpec

router = APIRouter(prefix="/api/search-jobs", tags=["search"])


@router.post("", status_code=202)
def create_search(body: SearchSpec, request: Request):
    try:
        return request.app.state.search_jobs.create(body)
    except SearchQueueFull as exc:
        raise HTTPException(429, str(exc)) from None


@router.get("/{job_id}")
def search_status(job_id: str, request: Request):
    try:
        return request.app.state.search_jobs.status(job_id)
    except KeyError:
        raise HTTPException(404, "Поиск не найден.") from None


@router.post("/{job_id}/cancel")
def cancel_search(job_id: str, request: Request):
    try:
        return request.app.state.search_jobs.cancel(job_id)
    except KeyError:
        raise HTTPException(404, "Поиск не найден.") from None


@router.get("/{job_id}/result")
def search_result(
    job_id: str,
    request: Request,
    offset: Annotated[int, Query(ge=0, le=100)] = 0,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    cursor: Annotated[str | None, Query(max_length=4096)] = None,
):
    try:
        return request.app.state.search_jobs.result(
            job_id, offset=offset, limit=limit, cursor=cursor
        )
    except KeyError:
        raise HTTPException(404, "Поиск не найден.") from None
    except SearchNotReady as exc:
        raise HTTPException(409, str(exc)) from None
    except SearchSnapshotExpired as exc:
        raise HTTPException(410, str(exc)) from None
