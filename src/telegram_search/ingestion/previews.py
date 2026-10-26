import itertools
import json
import time
import uuid
from pathlib import Path

import ijson

from telegram_search.ingestion.inspection import checking_media, scan_export
from telegram_search.ingestion.progress import (
    ImportInterrupted,
    ImportProgress,
    freeze_progress,
    progress_snapshot,
)
from telegram_search.security.paths import relative_root, safe_media_path
from telegram_search.security.privacy import repository_warning
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize
from telegram_search.storage.revisions import message_version


class PreviewService:
    """Persist a bounded import preflight without modifying canonical messages."""

    def __init__(self, importer):
        self.importer = importer
        self.db = importer.db

    @staticmethod
    def _target(conn, metadata, scope, target_chat_id, create_new):
        external_id = str(metadata["id"]) if metadata.get("id") is not None else None
        if target_chat_id:
            chat = conn.execute("SELECT * FROM chats WHERE id=?", (target_chat_id,)).fetchone()
            if (
                not chat
                or chat["scope"] != scope
                or (external_id is not None and chat["external_id"] != external_id)
            ):
                raise UserError("ID диалога или область аккаунта не совпадает с выбранной целью.")
            return target_chat_id, chat
        if external_id is not None:
            chat_id = str(uuid.uuid5(uuid.NAMESPACE_URL, serialize([scope, external_id])))
            return chat_id, conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        if create_new:
            return str(uuid.uuid4()), None
        raise UserError("Нет ID диалога: выберите существующий диалог или явно создайте новый.")

    def create(
        self,
        json_path: str,
        source_root: str | None = None,
        scope: str = "default",
        target_chat_id: str | None = None,
        policy: str = "preserve",
        create_new: bool = False,
        *,
        defer=False,
    ) -> str:
        if not scope.strip() or policy not in {"preserve", "prefer_imported"}:
            raise UserError("Проверьте область аккаунта и режим конфликтов.")
        path = Path(json_path).expanduser().resolve()
        root = Path(source_root).expanduser().resolve() if source_root else path.parent
        if not root.is_dir() or not path.is_file() or not path.is_relative_to(root):
            raise UserError("JSON должен находиться внутри существующей папки экспорта.")
        preview_id = str(uuid.uuid4())
        metadata, fingerprint, total = (
            ({}, "", None) if defer else scan_export(path, ImportProgress(self.importer, "preview"))
        )
        request = (
            serialize({"target_chat_id": target_chat_id, "create_new": create_new})
            if defer
            else None
        )
        with self.importer.lifecycle_lock, self.db.connect() as conn:
            if defer:
                # This provisional ID belongs only to the preview. No canonical
                # chat or source root is created before metadata validation.
                chat_id, chat = target_chat_id or "pending:" + preview_id, None
            else:
                chat_id, chat = self._target(conn, metadata, scope, target_chat_id, create_new)
            conn.execute(
                "INSERT INTO import_previews(id,chat_id,scope,external_id,chat_name,chat_kind,"
                "target_existed,base_revision,root_relative_path,json_relative_path,file_sha256,"
                "state,policy,created_at,total_messages,preparation_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,?)",
                (
                    preview_id,
                    chat_id,
                    scope,
                    str(metadata["id"]) if metadata.get("id") is not None else None,
                    metadata.get("name") or ("" if defer else "Без названия"),
                    metadata.get("type") or "unknown",
                    int(chat is not None),
                    chat["revision"] if chat else 0,
                    relative_root(self.db.workspace, root),
                    path.relative_to(root).as_posix(),
                    fingerprint,
                    policy,
                    int(time.time()),
                    total,
                    request,
                ),
            )
        return preview_id

    def _prepare_pending(self, preview, path, progress):
        with self.db.connect() as conn:
            request = json.loads(
                conn.execute(
                    "SELECT preparation_json FROM import_previews WHERE id=?", (preview["id"],)
                ).fetchone()[0]
            )
        metadata, fingerprint, total = scan_export(path, progress)
        with self.importer.lifecycle_lock, self.db.connect() as conn:
            progress.check(force=True)
            chat_id, chat = self._target(conn, metadata, preview["scope"], **request)
            conn.execute(
                "UPDATE import_previews SET chat_id=?,external_id=?,chat_name=?,chat_kind=?,"
                "target_existed=?,base_revision=?,file_sha256=?,total_messages=?,"
                "preparation_json=NULL WHERE id=? AND state='running'",
                (
                    chat_id,
                    str(metadata["id"]) if metadata.get("id") is not None else None,
                    metadata.get("name") or "Без названия",
                    metadata.get("type") or "unknown",
                    int(chat is not None),
                    chat["revision"] if chat else 0,
                    fingerprint,
                    total,
                    preview["id"],
                ),
            )
        return self.get(preview["id"])

    def get(self, preview_id: str) -> dict:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM import_previews WHERE id=?", (preview_id,)).fetchone()
        if row is None:
            raise UserError("Проверка экспорта не найдена.")
        result = dict(row)
        result["progress"] = progress_snapshot(
            result.pop("progress_json"), result["state"], processed=result["processed"]
        )
        result["preparing"] = result.pop("preparation_json") is not None
        warning = repository_warning(self.db.source_path(row["root_relative_path"]))
        result["warnings"] = [warning] if warning else []
        return result

    @staticmethod
    def assert_target(conn, preview: dict) -> None:
        chat = conn.execute(
            "SELECT revision FROM chats WHERE id=?", (preview["chat_id"],)
        ).fetchone()
        if bool(chat) != bool(preview["target_existed"]) or (
            chat is not None and chat[0] != preview["base_revision"]
        ):
            raise UserError("Диалог изменился после проверки. Пересчитайте отчёт перед импортом.")

    def submit(self, preview_id: str):
        with self.importer.lifecycle_lock:
            future = self.importer.executor.submit(self.run, preview_id)
            self.importer.futures[preview_id] = future
        return future

    def run(self, preview_id: str) -> dict:
        from telegram_search.ingestion.importer import file_hash, inspect_media

        with self.importer.lifecycle_lock, self.db.connect() as conn:
            preview = self.get(preview_id)
            if preview["state"] not in {"queued", "running", "interrupted", "failed"}:
                return preview
            conn.execute(
                "UPDATE import_previews SET state='running',error=NULL,finished_at=NULL WHERE id=?",
                (preview_id,),
            )
        progress = ImportProgress(self.importer, "preview", preview_id)
        root = self.db.source_path(preview["root_relative_path"])
        try:
            path = safe_media_path(root, preview["json_relative_path"])
            if preview["operation"] == "apply":
                self._apply(preview_id, progress=progress, expected_state="running")
                return self.get(preview_id)
            if preview["preparing"]:
                preview = self._prepare_pending(preview, path, progress)
            if file_hash(path, progress) != preview["file_sha256"]:
                raise UserError("JSON изменился. Создайте новую проверку экспорта.")
            with self.db.connect() as conn:
                self.assert_target(conn, preview)
            progress.check(force=True)
            with path.open("rb") as stream:
                iterator = ijson.items(stream, "messages.item", use_float=True)
                if preview["processed"]:
                    progress.phase("resume_scan", total=preview["processed"])
                    for ordinal, _ in enumerate(
                        itertools.islice(iterator, preview["processed"]), 1
                    ):
                        progress.update(ordinal)
                processed = preview["processed"]
                progress.phase(
                    "checking_messages", total=preview["total_messages"], completed=processed
                )
                while True:
                    progress.check(force=True)
                    batch = list(itertools.islice(iterator, self.importer.batch_size))
                    if not batch:
                        break
                    with checking_media(progress):
                        prepared = []
                        for message in batch:
                            progress.check()
                            prepared.append((message, inspect_media(root, message)))
                    with self.importer.lifecycle_lock, self.db.connect() as conn:
                        self.assert_target(conn, preview)
                        live = conn.execute(
                            "SELECT state,processed FROM import_previews WHERE id=?", (preview_id,)
                        ).fetchone()
                        if live["state"] != "running":
                            return self.get(preview_id)
                        counters = dict.fromkeys(
                            (
                                "processed",
                                "added",
                                "unchanged",
                                "updated",
                                "conflicts",
                                "missing_media",
                                "invalid_media",
                            ),
                            0,
                        )
                        for message, media in prepared:
                            classification, reason = self.importer.classify(
                                conn, preview["chat_id"], message, media, preview["policy"]
                            )
                            counters["processed"] += 1
                            counters[classification] += 1
                            counters["missing_media"] += sum(
                                m["status"] == "missing" for m in media
                            )
                            counters["invalid_media"] += sum(
                                m["status"] in {"unsafe", "corrupt"} for m in media
                            )
                            conn.execute(
                                "INSERT INTO preview_entries VALUES(?,?,?,?,?,?,?,?)",
                                (
                                    preview_id,
                                    live["processed"] + counters["processed"],
                                    message["id"],
                                    classification,
                                    reason,
                                    message_version(conn, preview["chat_id"], message["id"]),
                                    serialize(message),
                                    serialize(media),
                                ),
                            )
                        assignments = ",".join(f"{key}={key}+?" for key in counters)
                        conn.execute(
                            f"UPDATE import_previews SET {assignments} WHERE id=?",
                            (*counters.values(), preview_id),
                        )
                    processed += len(batch)
                    progress.update(processed)
            progress.finish("preview_ready", processed)
            with self.importer.lifecycle_lock, self.db.connect() as conn:
                self.assert_target(conn, preview)
                freeze_progress(conn, "import_previews", preview_id)
                conn.execute(
                    "UPDATE import_previews SET state='ready',finished_at=? WHERE id=? "
                    "AND state='running'",
                    (int(time.time()), preview_id),
                )
        except ImportInterrupted:
            with self.db.connect() as conn:
                if self.importer.stopping.is_set():
                    freeze_progress(conn, "import_previews", preview_id)
                    conn.execute(
                        "UPDATE import_previews SET state='interrupted' "
                        "WHERE id=? AND state='running'",
                        (preview_id,),
                    )
        except Exception as exc:
            error = (
                str(exc)
                if isinstance(exc, UserError)
                else ("Проверка остановлена из-за ошибки формата или повторяющихся ID сообщений.")
            )
            with self.db.connect() as conn:
                freeze_progress(conn, "import_previews", preview_id)
                conn.execute(
                    "UPDATE import_previews SET state='failed',error=? "
                    "WHERE id=? AND state='running'",
                    (error, preview_id),
                )
        return self.get(preview_id)

    def apply(self, preview_id: str) -> str:
        return self._apply(preview_id)

    def enqueue_apply(self, preview_id):
        with self.importer.lifecycle_lock, self.db.connect() as conn:
            preview = self.get(preview_id)
            if preview["state"] != "ready":
                raise UserError("Сначала дождитесь готового отчёта проверки.")
            freeze_progress(conn, "import_previews", preview_id)
            conn.execute(
                "UPDATE import_previews SET operation='apply',state='queued',error=NULL,"
                "progress_json='{}' WHERE id=?",
                (preview_id,),
            )
        self.submit(preview_id)
        return self.get(preview_id)

    def _apply(self, preview_id, *, progress=None, expected_state="ready"):
        from telegram_search.ingestion.importer import file_hash

        preview = self.get(preview_id)
        if preview["state"] != expected_state:
            raise UserError("Сначала дождитесь готового отчёта проверки.")
        root = self.db.source_path(preview["root_relative_path"])
        path = safe_media_path(root, preview["json_relative_path"])
        if file_hash(path, progress) != preview["file_sha256"]:
            raise UserError("JSON изменился после проверки. Пересчитайте отчёт.")
        if progress:
            progress.phase("import_queued", total=preview["total_messages"])
        with self.importer.lifecycle_lock:
            preview = self.get(preview_id)
            if preview["state"] != expected_state:
                raise UserError("Сначала дождитесь готового отчёта проверки.")
            with self.db.connect() as conn:
                self.assert_target(conn, preview)
                conn.execute(
                    "INSERT INTO chats(id,scope,external_id,name,kind,created_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                    (
                        preview["chat_id"],
                        preview["scope"],
                        preview["external_id"],
                        preview["chat_name"],
                        preview["chat_kind"],
                        int(time.time()),
                    ),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO source_roots(chat_id,relative_path) VALUES(?,?)",
                    (preview["chat_id"], preview["root_relative_path"]),
                )
                root_id = conn.execute(
                    "SELECT id FROM source_roots WHERE chat_id=? AND relative_path=?",
                    (preview["chat_id"], preview["root_relative_path"]),
                ).fetchone()[0]
                job_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO imports(id,chat_id,source_root_id,json_relative_path,file_sha256,"
                    "state,policy,started_at,preview_id,total_messages) "
                    "VALUES(?,?,?,?,?,'queued',?,?,?,?)",
                    (
                        job_id,
                        preview["chat_id"],
                        root_id,
                        preview["json_relative_path"],
                        preview["file_sha256"],
                        preview["policy"],
                        int(time.time()),
                        preview_id,
                        preview["total_messages"],
                    ),
                )
                conn.execute(
                    "UPDATE import_previews SET state='applied',applied_import_id=? WHERE id=?",
                    (job_id, preview_id),
                )
            self.importer.submit(job_id)
            return job_id

    def batches(self, job: dict):
        position = job["processed"]
        while True:
            with self.db.connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM preview_entries WHERE preview_id=? AND ordinal>? "
                    "ORDER BY ordinal LIMIT ?",
                    (job["preview_id"], position, self.importer.batch_size),
                ).fetchall()
            if not rows:
                return
            position = rows[-1]["ordinal"]
            yield [
                (
                    json.loads(row["incoming_json"]),
                    json.loads(row["media_json"]),
                    row["classification"],
                    row["reason"],
                    row["base_version"],
                )
                for row in rows
            ]

    def control(self, preview_id: str, action: str) -> dict:
        with self.importer.lifecycle_lock:
            with self.db.connect() as conn:
                preview = self.get(preview_id)
                freeze_progress(conn, "import_previews", preview_id)
                if action == "resume" and preview["state"] in {"paused", "interrupted", "failed"}:
                    conn.execute(
                        "UPDATE import_previews SET state='queued',error=NULL WHERE id=?",
                        (preview_id,),
                    )
                elif action in {"pause", "cancel"} and preview["state"] in {
                    "running",
                    "queued",
                    "paused",
                    "interrupted",
                    "ready",
                    "failed",
                }:
                    conn.execute(
                        "UPDATE import_previews SET state=? WHERE id=?",
                        ("paused" if action == "pause" else "cancelled", preview_id),
                    )
                else:
                    raise UserError("Действие недоступно для этой проверки экспорта.")
            if action == "resume":
                self.submit(preview_id)
            return self.get(preview_id)
