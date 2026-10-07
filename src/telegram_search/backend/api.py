import hashlib
import io
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from telegram_search.config.diagnostics import doctor
from telegram_search.config.runtime import frontend_directory, frozen
from telegram_search.indexing.media import MediaService
from telegram_search.indexing.service import SemanticService
from telegram_search.ingestion.importer import ImportService
from telegram_search.search.hybrid import HybridSearch
from telegram_search.search.lexical import ContextService, Filters, SearchService, date_bound
from telegram_search.search.media import MediaSearch, UnifiedSearch
from telegram_search.search.presentation import search_options
from telegram_search.security.paths import safe_media_path
from telegram_search.shared.errors import UserError
from telegram_search.sources.service import WorkspaceService
from telegram_search.storage.database import Database
from telegram_search.updates.service import UpdateService


class ImportRequest(BaseModel):
    json_path: str = Field(min_length=1, max_length=4096)
    source_root: str | None = Field(default=None, max_length=4096)
    scope: str = Field(default="default", min_length=1, max_length=256)
    target_chat_id: str | None = None
    policy: Literal["preserve", "prefer_imported"] = "preserve"
    create_new: bool = False


class ConflictResolution(BaseModel):
    choice: Literal["keep_current", "use_imported"]
    expected_version: str = Field(pattern=r"^[a-f0-9]{64}$")


class ModelRequest(BaseModel):
    profile: Literal["small", "base"] = "small"
    reindex: bool = False
    offline: bool = False
    repair: bool = False
    local_bundle: str | None = Field(default=None, max_length=4096)


class MediaModelRequest(BaseModel):
    kind: Literal["ocr", "images"]
    offline: bool = False


class SourceRelinkRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    expected_path: str = Field(min_length=1, max_length=4096)


class DeviceRequest(BaseModel):
    device: Literal["cpu", "auto", "gpu"]
    search_device: Literal["cpu", "auto", "gpu"] = "cpu"
    gpu_device_id: int = Field(default=0, ge=0, le=15, strict=True)
    gpu_memory_limit_mib: int = Field(default=4096, ge=512, le=65536, strict=True)
    reindex: bool = False


class UpdateCheckRequest(BaseModel):
    variant: Literal["cpu", "gpu"] | None = None


class UpdateConfirmation(BaseModel):
    nonce: str = Field(pattern=r"^[a-f0-9]{32}$")


def create_app(workspace: Path, frontend_dir: Path | None = None) -> FastAPI:
    db = Database(workspace)
    db.initialize()
    session_token = secrets.token_urlsafe(32)
    verification_nonce = os.environ.get("BTS_UPDATE_HEALTHCHECK", "")
    search = SearchService(db)
    context = ContextService(db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        importer = semantic = media = updates = None
        try:
            updates = UpdateService(db.workspace, getattr(app.state, "update_shutdown", None))
            app.state.updates = updates
            hold_background = bool(verification_nonce) or updates.recovery_required
            importer = ImportService(db)
            app.state.importer = importer
            semantic = SemanticService(
                db, importer.lifecycle_lock, start_background=not hold_background
            )
            app.state.semantic = semantic
            media = MediaService(
                db, importer.lifecycle_lock, semantic, start_background=not hold_background
            )
            app.state.media = media
            app.state.workspace = WorkspaceService(db, importer, semantic, media)
            app.state.search = UnifiedSearch(
                HybridSearch(db, semantic, importer.lifecycle_lock),
                MediaSearch(db, media, semantic, importer.lifecycle_lock),
            )
            if (
                frozen()
                and not verification_nonce
                and not updates.recovery_required
                and not os.environ.get("BTS_DISABLE_UPDATE_CHECK")
            ):
                updates.check()
            yield
        finally:
            if updates:
                updates.close()
            if media:
                media.shutdown()
            if semantic:
                semantic.shutdown()
            if importer:
                importer.shutdown()

    app = FastAPI(title="Telegram Search", lifespan=lifespan)
    app.state.db = db
    app.state.update_verifying = bool(verification_nonce)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if origin != f"{request.url.scheme}://{request.headers.get('host', '')}" or (
                parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            ):
                return JSONResponse({"detail": "Недопустимый origin."}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse(
                {"detail": "Доступ разрешён только из локального приложения."}, status_code=403
            )
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied = request.headers.get("x-session-token", "")
            if not secrets.compare_digest(supplied, session_token):
                return JSONResponse(
                    {"detail": "Необходим токен локальной сессии."}, status_code=403
                )
            if app.state.update_verifying and request.url.path != "/api/updates/confirm":
                return JSONResponse(
                    {"detail": "Приложение проверяется после обновления."}, status_code=503
                )
            updates = getattr(app.state, "updates", None)
            if updates and updates.recovery_required:
                return JSONResponse({"detail": updates.status()["error"]}, status_code=503)
            if (
                updates
                and updates.status()["state"] == "installing"
                and not (app.state.update_verifying and request.url.path == "/api/updates/confirm")
            ):
                return JSONResponse(
                    {"detail": "Приложение перезапускается для обновления."}, status_code=503
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "sandbox; default-src 'none'"
            if request.url.path.startswith("/api/media/")
            else "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.exception_handler(UserError)
    async def user_error(_request: Request, exc: UserError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/session")
    def session():
        return {
            "token": session_token,
            "device": db.settings.device,
            "search_backend": "bm25_messages",
            "instance_id": verification_nonce,
        }

    @app.get("/api/chats")
    def chats():
        return search.chats()

    @app.get("/api/authors")
    def authors(chat_id: Annotated[list[str] | None, Query()] = None):
        return search.authors(chat_id)

    @app.get("/api/search")
    def perform_search(
        q: Annotated[str, Query(max_length=2000)],
        chat_id: Annotated[list[str] | None, Query()] = None,
        author_id: Annotated[list[str] | None, Query()] = None,
        date_from: str | None = None,
        date_to: str | None = None,
        content_type: Literal["all", "text", "photo", "service"] = "all",
        exact: bool = False,
        mode: Literal["words", "meaning", "hybrid"] = "words",
        tab: Literal["all", "text", "images", "ocr"] = "text",
        modality: Annotated[
            list[Literal["text", "images", "ocr"]] | None, Query(min_length=1, max_length=3)
        ] = None,
        limit: Annotated[int | None, Query(ge=1, le=100)] = None,
        chunk_size: Annotated[int | None, Query(ge=1, le=100)] = None,
    ):
        filters = Filters(
            chat_id or [],
            author_id or [],
            date_bound(date_from),
            date_bound(date_to, end=True),
            content_type,
        )
        if (
            filters.date_from is not None
            and filters.date_to is not None
            and filters.date_from >= filters.date_to
        ):
            raise UserError("Начало периода должно быть раньше конца.")
        limit, chunk_size = search_options(db.settings, limit, chunk_size)
        result = app.state.search.search(
            q, filters, exact, limit, mode, tab, chunk_size, modalities=modality
        )
        return {**result, "limit": limit, "chunk_size": chunk_size}

    @app.get("/api/media-index")
    def media_status():
        return app.state.media.status()

    @app.post("/api/media-index/prepare", status_code=202)
    def prepare_media_model(body: MediaModelRequest):
        return app.state.media.prepare(body.kind, offline=body.offline)

    @app.post("/api/media-index/{action}")
    def media_control(action: Literal["pause", "resume", "retry"]):
        return app.state.media.control(action)

    @app.get("/api/settings")
    def workspace_settings():
        return app.state.workspace.settings()

    @app.patch("/api/settings")
    def update_settings(body: dict):
        return app.state.workspace.update_settings(body)

    @app.post("/api/device")
    def change_device(body: DeviceRequest):
        return app.state.workspace.change_device(**body.model_dump())

    @app.get("/api/updates")
    def update_status():
        return app.state.updates.status()

    @app.post("/api/updates/check", status_code=202)
    def check_update(body: UpdateCheckRequest):
        return app.state.updates.check(body.variant)

    @app.post("/api/updates/download", status_code=202)
    def download_update():
        return app.state.updates.download()

    @app.post("/api/updates/install", status_code=202)
    def install_update():
        return app.state.updates.install(getattr(app.state, "update_port", 8765))

    @app.post("/api/updates/confirm")
    def confirm_update(body: UpdateConfirmation):
        if not verification_nonce or not secrets.compare_digest(body.nonce, verification_nonce):
            raise UserError("Недопустимое подтверждение обновления.")
        from telegram_search.updates.installer import commit_update

        with app.state.importer.lifecycle_lock:
            commit_update(db.workspace, body.nonce)
            with app.state.updates.lock:
                app.state.updates.data.update(state="updated", error=None, commit_nonce=body.nonce)
            # Durable commit precedes any background mutation. A lost HTTP reply
            # can be retried and cannot trigger rollback over live indexing.
            app.state.update_verifying = False
            app.state.semantic.start_background()
            app.state.media.start_background()
        return {"confirmed": True}

    @app.get("/api/sources")
    def sources():
        return app.state.workspace.sources()

    @app.post("/api/sources/{source_id}/check")
    def check_source(source_id: int):
        return app.state.workspace.probe(source_id)

    @app.post("/api/sources/{source_id}/relink")
    def relink_source(source_id: int, body: SourceRelinkRequest):
        return app.state.workspace.probe(
            source_id, new_path=body.path, expected_path=body.expected_path
        )

    @app.get("/api/storage")
    def storage_sizes():
        return app.state.workspace.sizes()

    @app.post("/api/storage/compact")
    def compact_storage():
        db.compact()
        app.state.semantic.cleanup(compact=True)
        app.state.media.cleanup(compact=True)
        return app.state.workspace.sizes()

    @app.get("/api/chats/{chat_id}/deletion-estimate")
    def deletion_estimate(chat_id: str):
        return app.state.workspace.deletion_estimate(chat_id)

    @app.get("/api/semantic")
    def semantic_status():
        return app.state.semantic.status()

    @app.post("/api/semantic/prepare", status_code=202)
    def prepare_model(body: ModelRequest):
        return app.state.semantic.prepare(**body.model_dump())

    @app.post("/api/semantic/{action}")
    def control_semantic(action: Literal["pause", "resume", "retry", "compact"]):
        if action == "compact":
            app.state.semantic.cleanup(compact=True)
            return app.state.semantic.status()
        return app.state.semantic.control(action)

    @app.get("/api/chats/{chat_id}/context/{message_id}")
    def get_context(
        chat_id: str,
        message_id: int,
        before: Annotated[int, Query(ge=0, le=100)] = 10,
        after: Annotated[int, Query(ge=0, le=100)] = 10,
        author_id: Annotated[list[str] | None, Query()] = None,
        date_from: str | None = None,
        date_to: str | None = None,
        content_type: Literal["all", "text", "photo", "service"] = "all",
    ):
        filters = Filters(
            [chat_id],
            author_id or [],
            date_bound(date_from),
            date_bound(date_to, end=True),
            content_type,
        )
        return {"messages": context.get_context(chat_id, message_id, before, after, filters)}

    @app.get("/api/media/{media_id}")
    def media(media_id: int):
        with db.connect() as conn:
            ref = conn.execute(
                "SELECT m.*,s.relative_path AS root FROM media_refs m "
                "JOIN source_roots s ON s.id=m.source_root_id WHERE m.id=?",
                (media_id,),
            ).fetchone()
        if not ref or ref["kind"] != "photo" or ref["status"] != "ready":
            raise HTTPException(status_code=404, detail="Изображение недоступно.")
        try:
            path = safe_media_path(db.source_path(ref["root"]), ref["relative_path"])
        except UserError as exc:
            raise HTTPException(status_code=404, detail="Изображение недоступно.") from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Файл источника отсутствует.")
        # Detect the actual raster format; an export may name a polyglot image .html or .js.
        mime_types = {
            "JPEG": "image/jpeg",
            "PNG": "image/png",
            "GIF": "image/gif",
            "WEBP": "image/webp",
            "BMP": "image/bmp",
            "TIFF": "image/tiff",
            "ICO": "image/x-icon",
            "AVIF": "image/avif",
        }
        try:
            with path.open("rb") as stream:
                data = stream.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Изображение больше 32 МиБ.")
            if hashlib.sha256(data).hexdigest() != ref["sha256"]:
                raise HTTPException(status_code=404, detail="Файл изменился. Повторите импорт.")
            with Image.open(io.BytesIO(data)) as image:
                mime = mime_types.get(image.format)
                image.verify()
            if not mime:
                raise ValueError("unsupported raster format")
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise HTTPException(status_code=404, detail="Изображение недоступно.") from exc
        return Response(data, media_type=mime)

    @app.post("/api/imports", status_code=202)
    def import_export(body: ImportRequest):
        with app.state.importer.lifecycle_lock:
            job_id = app.state.importer.prepare(**body.model_dump())
            app.state.importer.submit(job_id)
        return app.state.importer.get(job_id)

    @app.post("/api/import-previews", status_code=202)
    def preview_export(body: ImportRequest):
        service = app.state.importer.previews
        with app.state.importer.lifecycle_lock:
            preview_id = service.create(**body.model_dump())
            service.submit(preview_id)
        return service.get(preview_id)

    @app.get("/api/import-previews")
    def list_previews():
        with db.connect() as conn:
            ids = conn.execute(
                "SELECT id FROM import_previews WHERE state NOT IN ('applied','cancelled') "
                "ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
        return [app.state.importer.previews.get(row[0]) for row in ids]

    @app.get("/api/import-previews/{preview_id}")
    def get_preview(preview_id: str):
        return app.state.importer.previews.get(preview_id)

    @app.post("/api/import-previews/{preview_id}/apply", status_code=202)
    def apply_preview(preview_id: str):
        job_id = app.state.importer.previews.apply(preview_id)
        return app.state.importer.get(job_id)

    @app.post("/api/import-previews/{preview_id}/{action}")
    def control_preview(preview_id: str, action: Literal["pause", "resume", "cancel"]):
        return app.state.importer.previews.control(preview_id, action)

    @app.get("/api/imports/{job_id}/conflicts")
    def conflicts(job_id: str, after: int = -1, limit: Annotated[int, Query(ge=1, le=100)] = 30):
        return app.state.importer.conflicts.list(job_id, after, limit)

    @app.post("/api/imports/{job_id}/conflicts/{message_id}")
    def resolve_conflict(job_id: str, message_id: int, body: ConflictResolution):
        return app.state.importer.conflicts.resolve(
            job_id, message_id, body.choice, body.expected_version
        )

    @app.get("/api/imports")
    def imports():
        with db.connect() as conn:
            return [
                {**app.state.importer.get(row["id"]), "chat_name": row["chat_name"]}
                for row in conn.execute(
                    "SELECT i.*,c.name AS chat_name FROM imports i JOIN chats c ON c.id=i.chat_id "
                    "ORDER BY i.started_at DESC,i.rowid DESC LIMIT 50"
                )
            ]

    @app.post("/api/imports/{job_id}/{action}")
    def control(job_id: str, action: Literal["pause", "resume", "cancel"]):
        return app.state.importer.control(job_id, action)

    @app.delete("/api/chats/{chat_id}")
    def delete_chat(chat_id: str):
        with app.state.importer.lifecycle_lock:
            result = delete_chat_locked(chat_id)
        # A chunk builder may hold a WAL read snapshot while waiting to stage.
        # Release lifecycle_lock so it can observe the deletion and close its reader.
        db.compact()
        app.state.semantic.wake.set()
        app.state.media.wake.set()
        return result

    def delete_chat_locked(chat_id: str):
        if app.state.importer.is_active(chat_id):
            raise UserError("Дождитесь завершения или остановки фоновой задачи этого диалога.")
        with db.connect() as conn:
            active = conn.execute(
                "SELECT 1 FROM imports WHERE chat_id=? AND state IN ('queued','running') "
                "UNION ALL SELECT 1 FROM import_previews WHERE chat_id=? "
                "AND state IN ('queued','running') LIMIT 1",
                (chat_id, chat_id),
            ).fetchone()
            if active:
                raise UserError("Сначала приостановите или отмените импорт этого диалога.")
            if not conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
                raise HTTPException(status_code=404, detail="Диалог не найден.")
            # Paused tasks have no further writes; cascade also removes their checkpoints.
            conn.execute(
                "INSERT INTO vector_deletions(chat_id,created_at) VALUES(?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET created_at=excluded.created_at,error=NULL",
                (chat_id, int(time.time())),
            )
            conn.execute("DELETE FROM vector_segment_cleanup WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM import_previews WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        return {"deleted": True, "source_files_preserved": True}

    @app.get("/api/doctor")
    def diagnostics():
        status = app.state.semantic.status()
        media_backend = app.state.media.status().get("backend")
        backend = status["backend"] or media_backend
        result = doctor(db)
        result["hardware"]["selected_provider"] = backend.get("provider") if backend else None
        return {
            **result,
            "device": backend.get("device", db.settings.device) if backend else db.settings.device,
            "semantic": status,
            "dense_available": status["dense_available"],
            "models_loaded": int(bool(status["backend"] and status["backend"]["loaded"])),
        }

    @app.post("/api/rebuild")
    def rebuild():
        with app.state.importer.lifecycle_lock:
            db.rebuild()
        return {"rebuilt": "fts5"}

    frontend_dir = frontend_dir or frontend_directory()
    if (frontend_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=frontend_dir / "assets"), name="assets")

    @app.get("/")
    def index():
        if not (frontend_dir / "index.html").is_file():
            return JSONResponse(
                {"detail": "Соберите интерфейс: cd frontend && npm ci && npm run build"},
                status_code=503,
            )
        return FileResponse(frontend_dir / "index.html")

    return app
