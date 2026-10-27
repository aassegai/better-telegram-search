"""Local adapter code is run only by an explicit validation request."""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from telegram_search.model_adapters.client import AdapterContext

router = APIRouter(prefix="/api/adapters")


class AdapterJobRequest(BaseModel):
    action: Literal["inspect", "validate"]
    role: Literal["text_embedding", "image_embedding", "reranker", "ocr", "asr"]
    manifest: str = Field(min_length=1, max_length=4096)
    python: str | None = Field(default=None, min_length=1, max_length=4096)
    expected_identity: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    device: Literal["cpu", "gpu"] = "cpu"


@router.get("")
def catalog(request: Request):
    return request.app.state.adapter_jobs.catalog()


@router.post("/jobs", status_code=202)
def create_job(body: AdapterJobRequest, request: Request):
    settings = request.app.state.db.settings
    return request.app.state.adapter_jobs.create(
        action=body.action,
        manifest=body.manifest,
        role=body.role,
        python=body.python,
        expected_identity=body.expected_identity,
        context=AdapterContext(
            device=body.device,
            cpu_threads=settings.cpu_threads,
            gpu_device_id=settings.gpu_device_id,
            gpu_memory_limit_mib=settings.gpu_memory_limit_mib,
        ),
    )


@router.get("/jobs/{job_id}")
def status(job_id: str, request: Request):
    return request.app.state.adapter_jobs.snapshot(job_id)


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: str, request: Request):
    return request.app.state.adapter_jobs.cancel(job_id)
