"""Scoped local-session APIs for CPU-only noise analysis; no policy application."""

import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from telegram_search.indexing.noise_policy import NoiseDraftConflict
from telegram_search.search.chunking.noise.contracts import AnalysisOptions
from telegram_search.storage.contention import database_busy

router = APIRouter(prefix="/api/chats/{chat_id}/noise-analyses", tags=["indexing"])
JobId = Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")]
ChatId = Annotated[str, Path(min_length=1, max_length=256)]


class StartNoiseAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_messages: int = Field(default=100000, strict=True, ge=1, le=1000000)
    max_forms: int = Field(default=50000, strict=True, ge=1, le=50000)
    time_limit_seconds: int = Field(default=300, strict=True, ge=1, le=3600)


class DraftRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(strict=True, ge=0, le=2**31 - 1)


class DraftOverrides(DraftRevision):
    overrides: dict[Annotated[str, Field(min_length=1, max_length=64)], Literal["keep", "omit"]] = (
        Field(max_length=1000)
    )


def invoke(action):
    try:
        return action()
    except KeyError:
        raise HTTPException(404, "Диалог или анализ не найден.") from None
    except NoiseDraftConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except sqlite3.Error as exc:
        if database_busy(exc):
            raise HTTPException(
                503, "База занята. Повторите запрос через несколько секунд."
            ) from None
        raise HTTPException(500, "Не удалось прочитать анализ.") from None


@router.get("")
def list_analyses(chat_id: ChatId, request: Request):
    return invoke(lambda: request.app.state.noise_analyses.list(chat_id))


@router.post("", status_code=202)
def start_analysis(chat_id: ChatId, body: StartNoiseAnalysis, request: Request):
    return invoke(
        lambda: request.app.state.noise_analyses.create(
            chat_id, AnalysisOptions(**body.model_dump())
        )
    )


@router.get("/{job_id}")
def analysis_status(chat_id: ChatId, job_id: JobId, request: Request):
    return invoke(lambda: request.app.state.noise_analyses.status(chat_id, job_id))


@router.post("/{job_id}/cancel")
def cancel_analysis(chat_id: ChatId, job_id: JobId, request: Request):
    return invoke(lambda: request.app.state.noise_analyses.cancel(chat_id, job_id))


@router.delete("/{job_id}", status_code=204)
def delete_analysis(chat_id: ChatId, job_id: JobId, request: Request):
    invoke(lambda: request.app.state.noise_analyses.delete(chat_id, job_id))


@router.get("/{job_id}/forms")
def analysis_forms(
    chat_id: ChatId,
    job_id: JobId,
    request: Request,
    decision: Literal["keep", "review", "omit"] | None = None,
    offset: Annotated[int, Query(ge=0, le=50000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    def load():
        state = request.app.state
        page = state.noise_analyses.report(
            chat_id, job_id, decision=decision, offset=offset, limit=limit
        )
        return state.noise_policies.explain_forms(chat_id, job_id, page)

    return invoke(load)


@router.get("/{job_id}/families")
def analysis_families(
    chat_id: ChatId,
    job_id: JobId,
    request: Request,
    offset: Annotated[int, Query(ge=0, le=50000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    return invoke(
        lambda: request.app.state.noise_analyses.report(
            chat_id, job_id, kind="families", offset=offset, limit=limit
        )
    )


@router.get("/{job_id}/draft")
def draft_metadata(chat_id: ChatId, job_id: JobId, request: Request):
    return invoke(lambda: request.app.state.noise_policies.metadata(chat_id, job_id))


@router.patch("/{job_id}/draft")
def save_draft(chat_id: ChatId, job_id: JobId, body: DraftOverrides, request: Request):
    return invoke(
        lambda: request.app.state.noise_policies.save_overrides(
            chat_id, job_id, body.revision, body.overrides
        )
    )


@router.post("/{job_id}/preview")
def preview_draft(chat_id: ChatId, job_id: JobId, body: DraftRevision, request: Request):
    return invoke(lambda: request.app.state.noise_policies.preview(chat_id, job_id, body.revision))


@router.post("/{job_id}/apply")
def apply_draft(chat_id: ChatId, job_id: JobId, body: DraftRevision, request: Request):
    return invoke(lambda: request.app.state.noise_policies.apply(chat_id, job_id, body.revision))
