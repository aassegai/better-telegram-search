import hashlib
import itertools
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import ijson
from filelock import FileLock, Timeout
from PIL import Image, UnidentifiedImageError

from telegram_search.ingestion.conflicts import ConflictService
from telegram_search.ingestion.previews import PreviewService
from telegram_search.ingestion.writer import CanonicalMessageWriter
from telegram_search.security.paths import relative_root, safe_media_path
from telegram_search.security.privacy import repository_warning
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import (
    serialize,
)
from telegram_search.storage.database import Database
from telegram_search.storage.revisions import message_version


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_metadata(path: Path) -> dict:
    metadata = {}
    has_messages = False
    try:
        # Only keep scalar header values, including headers appearing after messages.
        with path.open("rb") as stream:
            for prefix, event, value in ijson.parse(stream, use_float=True):
                if prefix in {"id", "name", "type"} and event in {"string", "number", "null"}:
                    metadata[prefix] = value
                if prefix == "messages" and event == "start_array":
                    has_messages = True
    except (OSError, ValueError, ijson.JSONError) as exc:
        raise UserError("Не удалось прочитать JSON экспорта Telegram.") from exc
    if not has_messages:
        raise UserError("Нужен JSON одного диалога из Telegram Desktop с массивом messages.")
    return metadata


def inspect_media(root: Path, message: dict) -> list[dict]:
    media = []
    for field, kind in (("photo", "photo"), ("file", "attachment")):
        relative = message.get(field)
        if not isinstance(relative, str) or not relative:
            continue
        item = {
            "relative_path": relative,
            "kind": kind,
            "sha256": None,
            "status": "missing",
            "size": 0,
        }
        try:
            path = safe_media_path(root, relative)
            if path.is_file():
                if kind == "photo":
                    with Image.open(path) as image:
                        image.verify()
                item.update(sha256=file_hash(path), size=path.stat().st_size, status="ready")
        except UserError:
            item["status"] = "unsafe"
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
            item["status"] = "corrupt"
        media.append(item)
    return media


class ImportService:
    """One bounded streaming writer; every batch and its checkpoint commit together."""

    def __init__(self, db: Database, batch_size: int = 100):
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.db = db
        self.writer = CanonicalMessageWriter(db)
        self.lease = FileLock(db.workspace / ".writer.lock", timeout=0)
        try:
            self.lease.acquire()
        except Timeout as exc:
            raise UserError("Workspace уже используется другим процессом приложения.") from exc
        self.lifecycle_lock = threading.RLock()
        self.closed = False
        self.source_warnings = {}
        self.batch_size = batch_size
        self.stopping = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telegram-import")
        self.futures = {}
        self.previews = PreviewService(self)
        self.conflicts = ConflictService(self)
        with db.connect() as conn:
            conn.execute(
                "UPDATE imports SET state='interrupted' WHERE state IN ('running','queued')"
            )
            conn.execute(
                "UPDATE import_previews SET state='interrupted' WHERE state IN ('running','queued')"
            )

    def prepare(self, *args, **kwargs) -> str:
        with self.lifecycle_lock:
            return self._prepare(*args, **kwargs)

    def _prepare(
        self,
        json_path: str,
        source_root: str | None = None,
        scope: str = "default",
        target_chat_id: str | None = None,
        policy: str = "preserve",
        create_new: bool = False,
    ) -> str:
        if policy not in {"preserve", "prefer_imported"}:
            raise UserError("Неизвестный режим обработки конфликтов.")
        if not scope.strip():
            raise UserError("Укажите область аккаунта.")
        path = Path(json_path).expanduser().resolve()
        root = Path(source_root).expanduser().resolve() if source_root else path.parent
        if not root.is_dir() or not path.is_file() or not path.is_relative_to(root):
            raise UserError("JSON должен находиться внутри существующей папки экспорта.")
        metadata = read_metadata(path)
        external_id = str(metadata["id"]) if metadata.get("id") is not None else None
        fingerprint = file_hash(path)
        with self.db.connect() as conn:
            if target_chat_id:
                chat = conn.execute("SELECT * FROM chats WHERE id=?", (target_chat_id,)).fetchone()
                if not chat:
                    raise UserError("Выбранный диалог не найден.")
                if chat["scope"] != scope or (
                    external_id is not None and chat["external_id"] != external_id
                ):
                    raise UserError(
                        "ID диалога или область аккаунта не совпадает с выбранной целью."
                    )
                chat_id = chat["id"]
            elif external_id is not None:
                chat_id = str(uuid.uuid5(uuid.NAMESPACE_URL, serialize([scope, external_id])))
            elif create_new:
                chat_id = str(uuid.uuid4())
            else:
                raise UserError(
                    "Нет ID диалога: выберите существующий диалог или явно создайте новый."
                )
            conn.execute(
                "INSERT INTO chats(id,scope,external_id,name,kind,created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(id) DO NOTHING",
                (
                    chat_id,
                    scope,
                    external_id,
                    metadata.get("name") or "Без названия",
                    metadata.get("type") or "unknown",
                    int(time.time()),
                ),
            )
            root_relative = relative_root(self.db.workspace, root)
            conn.execute(
                "INSERT OR IGNORE INTO source_roots(chat_id,relative_path) VALUES(?,?)",
                (chat_id, root_relative),
            )
            root_id = conn.execute(
                "SELECT id FROM source_roots WHERE chat_id=? AND relative_path=?",
                (chat_id, root_relative),
            ).fetchone()[0]
            job_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO imports(id,chat_id,source_root_id,json_relative_path,file_sha256,"
                "state,policy,started_at) VALUES(?,?,?,?,?,'queued',?,?)",
                (
                    job_id,
                    chat_id,
                    root_id,
                    path.relative_to(root).as_posix(),
                    fingerprint,
                    policy,
                    int(time.time()),
                ),
            )
        return job_id

    def submit(self, job_id: str):
        with self.lifecycle_lock:
            future = self.executor.submit(self.run, job_id)
            self.futures[job_id] = future
            return future

    def is_active(self, chat_id: str) -> bool:
        with self.db.connect() as conn:
            jobs = conn.execute("SELECT id FROM imports WHERE chat_id=?", (chat_id,)).fetchall()
            jobs += conn.execute(
                "SELECT id FROM import_previews WHERE chat_id=?", (chat_id,)
            ).fetchall()
        return any(row["id"] in self.futures and not self.futures[row["id"]].done() for row in jobs)

    def get(self, job_id: str) -> dict:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM imports WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise UserError("Задача импорта не найдена.")
            result = dict(row)
            root = conn.execute(
                "SELECT relative_path FROM source_roots WHERE id=?", (row["source_root_id"],)
            ).fetchone()
            if root[0] not in self.source_warnings:
                self.source_warnings[root[0]] = repository_warning(self.db.source_path(root[0]))
            warning = self.source_warnings[root[0]]
            result["warnings"] = [warning] if warning else []
            result["pending_conflicts"] = conn.execute(
                "SELECT COUNT(*) FROM import_conflicts WHERE import_id=? AND state='pending'",
                (job_id,),
            ).fetchone()[0]
            return result

    def control(self, job_id: str, action: str) -> dict:
        with self.lifecycle_lock:
            return self._control(job_id, action)

    def _control(self, job_id: str, action: str) -> dict:
        job = self.get(job_id)
        if action == "resume":
            if job["state"] not in {"paused", "interrupted", "failed"}:
                raise UserError("Эту задачу нельзя возобновить в текущем состоянии.")
            with self.db.connect() as conn:
                conn.execute("UPDATE imports SET state='queued',error=NULL WHERE id=?", (job_id,))
            self.submit(job_id)
        elif action in {"pause", "cancel"}:
            if job["state"] not in {"queued", "running", "paused", "interrupted"}:
                raise UserError("Эта задача уже завершена.")
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE imports SET state=? WHERE id=?",
                    ("paused" if action == "pause" else "cancelled", job_id),
                )
        else:
            raise UserError("Неизвестное действие с задачей.")
        return self.get(job_id)

    def run(self, job_id: str) -> dict:
        job = self.get(job_id)
        if job["state"] not in {"queued", "running", "interrupted", "failed"}:
            return job
        try:
            with self.db.connect() as conn:
                source = conn.execute(
                    "SELECT * FROM source_roots WHERE id=?", (job["source_root_id"],)
                ).fetchone()
            root = self.db.source_path(source["relative_path"])
            path = safe_media_path(root, job["json_relative_path"])
            if file_hash(path) != job["file_sha256"]:
                raise UserError("JSON изменился после начала импорта. Создайте новую задачу.")
            with self.db.connect() as conn:
                # A queued task may be paused while its fingerprint is checked.
                conn.execute(
                    "UPDATE imports SET state='running',error=NULL WHERE id=? "
                    "AND state IN ('queued','running','interrupted','failed')",
                    (job_id,),
                )
            if job["preview_id"] and job["processed"] == 0:
                preview = self.previews.get(job["preview_id"])
                with self.db.connect() as conn:
                    revision = conn.execute(
                        "SELECT revision FROM chats WHERE id=?", (job["chat_id"],)
                    ).fetchone()[0]
                    if revision != preview["base_revision"]:
                        raise UserError("Диалог изменился после проверки. Создайте новую проверку.")
            for prepared in self._prepared_batches(job, root, path):
                current = self.get(job_id)
                if current["state"] in {"paused", "cancelled"}:
                    return current
                if self.stopping.is_set():
                    with self.db.connect() as conn:
                        conn.execute("UPDATE imports SET state='interrupted' WHERE id=?", (job_id,))
                    return self.get(job_id)
                if self._apply_batch(job, prepared) is False:
                    return self.get(job_id)
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE imports SET state='completed',finished_at=? WHERE id=? "
                    "AND state='running'",
                    (int(time.time()), job_id),
                )
        except Exception as exc:
            error = (
                str(exc)
                if isinstance(exc, UserError)
                else (
                    "Импорт остановлен из-за ошибки чтения или формата. "
                    "Исправьте источник и повторите."
                )
            )
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE imports SET state='failed',error=? WHERE id=?", (error, job_id)
                )
        return self.get(job_id)

    def _prepared_batches(self, job: dict, root: Path, path: Path):
        if job["preview_id"]:
            for prepared in self.previews.batches(job):
                for entry in prepared:
                    self.validate_media_snapshot(root, entry[0], entry[1])
                yield prepared
            return
        with path.open("rb") as stream:
            iterator = ijson.items(stream, "messages.item", use_float=True)
            for _ in itertools.islice(iterator, job["processed"]):
                pass
            while batch := list(itertools.islice(iterator, self.batch_size)):
                yield [(message, inspect_media(root, message)) for message in batch]

    @staticmethod
    def validate_media_snapshot(root: Path, message: dict, expected: list[dict]) -> None:
        if inspect_media(root, message) != expected:
            raise UserError("Медиа изменились после проверки. Создайте новый отчёт экспорта.")

    def _apply_batch(self, job: dict, prepared: list) -> bool:
        with self.lifecycle_lock:
            if self.get(job["id"])["state"] != "running":
                return False
            if job["preview_id"]:
                with self.db.connect() as conn:
                    for message, media, expected, reason, version in prepared:
                        if message_version(
                            conn, job["chat_id"], message["id"]
                        ) != version or self.classify(
                            conn, job["chat_id"], message, media, job["policy"]
                        ) != (expected, reason):
                            raise UserError(
                                "Сообщение изменилось после проверки. Пересчитайте отчёт."
                            )
            self._apply_batch_unlocked(job, [item[:2] for item in prepared])
            return True

    def _apply_batch_unlocked(self, job: dict, prepared: list, connection=None) -> None:
        self.writer.apply(job, prepared, connection)

    def classify(self, conn, chat_id, message, media, policy):
        return self.writer.classify(conn, chat_id, message, media, policy)

    def shutdown(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.stopping.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE imports SET state='interrupted' WHERE state IN ('queued','running')"
            )
            conn.execute(
                "UPDATE import_previews SET state='interrupted' WHERE state IN ('queued','running')"
            )
        self.db.checkpoint()
        self.lease.release()
