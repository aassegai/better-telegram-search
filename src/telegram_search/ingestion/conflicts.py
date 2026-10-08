import json
import time

from telegram_search.search.lexical import ContextService
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import flatten_text, meaningful_content, timestamp
from telegram_search.storage.revisions import message_version


class ConflictService:
    def __init__(self, importer):
        self.importer = importer
        self.db = importer.db

    def list(self, import_id: str, after: int = -1, limit: int = 30) -> dict:
        if not 1 <= limit <= 100:
            raise UserError("Лимит списка должен быть от 1 до 100.")
        job = self.importer.get(import_id)
        results = []
        with self.db.connect() as conn:
            # The body and its CAS token must describe the same SQLite snapshot.
            conn.execute("BEGIN")
            pending = conn.execute(
                "SELECT COUNT(*) FROM import_conflicts WHERE import_id=? AND state='pending'",
                (import_id,),
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM import_conflicts WHERE import_id=? AND message_id>? "
                "AND state='pending' ORDER BY message_id LIMIT ?",
                (import_id, after, limit + 1),
            ).fetchall()
            for row in rows[:limit]:
                current = conn.execute(
                    "SELECT * FROM messages WHERE chat_id=? AND message_id=?",
                    (job["chat_id"], row["message_id"]),
                ).fetchone()
                incoming = json.loads(row["incoming_json"])
                current_media = [
                    dict(ref)
                    for ref in conn.execute(
                        "SELECT kind,sha256,relative_path FROM media_refs "
                        "WHERE chat_id=? AND message_id=? ORDER BY kind,sha256",
                        (job["chat_id"], row["message_id"]),
                    )
                ]
                incoming_media = json.loads(row["incoming_media_json"] or "[]")
                results.append(
                    {
                        "message_id": row["message_id"],
                        "reason": row["reason"],
                        "current": ContextService.serialize_message(conn, current)
                        if current
                        else None,
                        "current_version": message_version(conn, job["chat_id"], row["message_id"]),
                        "current_metadata": meaningful_content(
                            json.loads(current["raw_json"]), current_media
                        )
                        if current
                        else {},
                        "incoming": {
                            "text": flatten_text(incoming.get("text")),
                            "author": incoming.get("from") or incoming.get("actor") or "",
                            "timestamp": timestamp(incoming),
                            "edited_timestamp": timestamp(incoming, "edited"),
                            "metadata": meaningful_content(incoming, incoming_media),
                        },
                    }
                )
        return {
            "results": results,
            "has_more": len(rows) > limit,
            "pending": pending,
        }

    def resolve(self, import_id: str, message_id: int, choice: str, expected_version: str) -> dict:
        if choice not in {"keep_current", "use_imported"}:
            raise UserError("Неизвестный выбор версии сообщения.")
        with self.importer.lifecycle_lock, self.db.connect() as conn:
            job = self.importer.get(import_id)
            if self.importer.is_active(job["chat_id"]):
                raise UserError("Сначала дождитесь завершения или остановки импорта.")
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM import_conflicts WHERE import_id=? AND message_id=?",
                (import_id, message_id),
            ).fetchone()
            if row is None or row["state"] != "pending":
                raise UserError("Конфликт уже разрешён или не найден.")
            if message_version(conn, job["chat_id"], message_id) != expected_version:
                raise UserError("Текущая версия изменилась. Обновите список конфликтов.")
            if choice == "use_imported":
                if row["reason"] == "remote_deleted":
                    raise UserError(
                        "Сообщение удалено в Telegram. Старый экспорт не восстановит его."
                    )
                if conn.execute(
                    "SELECT 1 FROM telegram_message_provenance WHERE chat_id=? AND message_id=?",
                    (job["chat_id"], message_id),
                ).fetchone():
                    raise UserError("Свежая версия Telegram защищена от старого экспорта.")
                incoming = json.loads(row["incoming_json"])
                if row["incoming_media_json"] is None:
                    from telegram_search.ingestion.importer import inspect_media

                    root = conn.execute(
                        "SELECT relative_path FROM source_roots WHERE id=?",
                        (job["source_root_id"],),
                    ).fetchone()[0]
                    media = inspect_media(self.db.source_path(root), incoming)
                else:
                    media = json.loads(row["incoming_media_json"])
                    root = conn.execute(
                        "SELECT relative_path FROM source_roots WHERE id=?",
                        (job["source_root_id"],),
                    ).fetchone()[0]
                    self.importer.validate_media_snapshot(
                        self.db.source_path(root), incoming, media
                    )
                authoritative = {
                    **job,
                    "policy": "prefer_imported",
                    "allow_older": True,
                    "record_counters": False,
                    "reason": "conflict_resolution",
                }
                self.importer._apply_batch_unlocked(authoritative, [(incoming, media)], conn)
            state = "kept" if choice == "keep_current" else "applied"
            conn.execute(
                "UPDATE import_conflicts SET state=?,resolved_at=? "
                "WHERE import_id=? AND message_id=?",
                (state, int(time.time()), import_id, message_id),
            )
        return {"resolved": True, "choice": choice}
