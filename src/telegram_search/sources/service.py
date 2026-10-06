import os
from dataclasses import asdict, replace
from pathlib import Path

from telegram_search.inference.bundles import checksum
from telegram_search.security.paths import safe_media_path
from telegram_search.shared.errors import UserError
from telegram_search.storage.generations import bump_revision, invalidate_segments, utc_day


class WorkspaceService:
    def __init__(self, db, importer, semantic, media):
        self.db, self.importer, self.semantic, self.media = db, importer, semantic, media
        self.lock = importer.lifecycle_lock

    def sources(self):
        with self.db.connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT s.*,c.name AS chat_name,COUNT(r.id) AS media_refs,"
                    "SUM(CASE WHEN r.status='ready' THEN 1 ELSE 0 END) AS ready_refs "
                    "FROM source_roots s JOIN chats c ON c.id=s.chat_id LEFT JOIN media_refs r "
                    "ON r.source_root_id=s.id GROUP BY s.id ORDER BY s.id"
                )
            ]
        for row in rows:
            row["path"] = str(self.db.source_path(row.pop("relative_path")))
            row["available"] = Path(row["path"]).is_dir()
        return rows

    def probe(self, source_id, *, new_path=None, expected_path=None):
        with self.lock, self.db.connect() as conn:
            source = conn.execute(
                "SELECT s.*,c.revision FROM source_roots s JOIN chats c "
                "ON c.id=s.chat_id WHERE s.id=?",
                (source_id,),
            ).fetchone()
            if not source:
                raise UserError("Источник не найден.")
            old_root = self.db.source_path(source["relative_path"])
            if new_path is not None and expected_path != str(old_root):
                raise UserError("Источник уже изменился. Обновите список.")
            if self.importer.is_active(source["chat_id"]):
                raise UserError("Дождитесь остановки импорта или проверки этого источника.")
            root = Path(new_path).expanduser().resolve() if new_path is not None else old_root
            if not root.is_dir():
                raise UserError("Папка источника не найдена.")
        results, counts, days = [], {"ready": 0, "missing": 0, "changed": 0, "unverified": 0}, set()
        # A read snapshot permits bounded streaming without holding the writer during hashing.
        with self.db.connect() as reader:
            reader.execute("BEGIN")
            for ref in reader.execute(
                "SELECT r.*,m.timestamp FROM media_refs r JOIN messages m "
                "ON m.chat_id=r.chat_id AND m.message_id=r.message_id "
                "WHERE source_root_id=? ORDER BY r.id",
                (source_id,),
            ):
                try:
                    path = safe_media_path(root, ref["relative_path"])
                    if not path.is_file():
                        status = "missing"
                    elif ref["sha256"] is None:
                        status = "unverified"
                    else:
                        status = "ready" if checksum(path) == ref["sha256"] else "changed"
                except (OSError, UserError):
                    status = "missing"
                counts[status] += 1
                # Only changed statuses need staging; roots normally have a small media subset.
                if status != ref["status"]:
                    results.append((status, ref["id"]))
                    days.add(utc_day(ref["timestamp"]))
        if new_path is not None and (counts["changed"] or counts["unverified"]):
            raise UserError(
                "Новая папка содержит изменённые или непроверенные файлы. "
                "Выполните повторный импорт."
            )
        with self.lock, self.db.connect() as conn:
            current = conn.execute(
                "SELECT s.relative_path,c.revision FROM source_roots s JOIN chats c "
                "ON c.id=s.chat_id WHERE s.id=?",
                (source_id,),
            ).fetchone()
            if (
                not current
                or current["relative_path"] != source["relative_path"]
                or current["revision"] != source["revision"]
                or self.importer.is_active(source["chat_id"])
            ):
                raise UserError("Источник изменился во время проверки. Повторите её.")
            if new_path is not None:
                # Paused import checkpoints refer to this root too;
                # never retarget their JSON file silently.
                jobs = conn.execute(
                    "SELECT 1 FROM imports WHERE source_root_id=? AND state IN "
                    "('paused','interrupted','failed') LIMIT 1",
                    (source_id,),
                ).fetchone()
                if jobs:
                    raise UserError("Отмените незавершённый импорт перед переносом источника.")
                conn.execute(
                    "UPDATE source_roots SET relative_path=? WHERE id=?",
                    (os.path.relpath(root, self.db.workspace), source_id),
                )
                conn.execute(
                    "UPDATE import_previews SET state='cancelled' WHERE chat_id=? "
                    "AND state NOT IN ('applied','cancelled')",
                    (source["chat_id"],),
                )
            conn.executemany("UPDATE media_refs SET status=? WHERE id=?", results)
            if results or new_path is not None:
                bump_revision(conn, source["chat_id"])
                if days:
                    invalidate_segments(conn, source["chat_id"], days, "source_check")
        self.media.wake.set()
        self.semantic.wake.set()
        return {"source_id": source_id, "counts": counts, "relinked": new_path is not None}

    def settings(self):
        return asdict(self.db.settings)

    def update_settings(self, values):
        display_keys = {"search_result_limit", "display_chunk_size"}
        allowed = {
            "cpu_threads",
            "embedding_batch",
            "image_batch",
            "idle_unload_seconds",
            "query_cache_entries",
            "retrieval_candidates",
            "rrf_k",
            "lexical_weight",
            "dense_weight",
            "ocr_timeout_seconds",
            "ocr_max_edge",
            "memory_limit_mib",
        } | display_keys
        if set(values) - allowed:
            raise UserError("Неизвестные или недоступные настройки.")
        with self.lock:
            candidate = replace(self.db.settings, **values)
            candidate.validate()
            if set(values) <= display_keys:
                candidate.save(self.db.workspace)
                self.db.settings = candidate
                return asdict(candidate)
            if (self.semantic.preparation and self.semantic.preparation.is_alive()) or (
                self.media.preparation and self.media.preparation.is_alive()
            ):
                raise UserError("Дождитесь завершения подготовки моделей.")
            if self.media.running:
                raise UserError("Приостановите медиа и дождитесь завершения текущей фотографии.")
            with self.db.connect() as conn:
                if conn.execute(
                    "SELECT 1 FROM index_work WHERE state='running' LIMIT 1"
                ).fetchone():
                    raise UserError("Приостановите текстовую индексацию перед изменением настроек.")
            new_ocr = self.media._new_ocr(candidate) if self.media.ocr else None
            if new_ocr:
                new_ocr.verify()
            candidate.save(self.db.workspace)
            self.db.settings = candidate
            if self.semantic.encoder:
                self.semantic.encoder.suspend()
                self.semantic.encoder.threads = candidate.cpu_threads
                self.semantic.encoder.resume()
            self.semantic.query_cache.clear()
            if self.media.clip:
                from telegram_search.inference.resources import compute_lock

                with compute_lock:
                    self.media.clip.unload()
                    self.media.clip.threads = candidate.cpu_threads
            if self.media.ocr:
                self.media.ocr = new_ocr
        self.media.wake.set()
        return self.settings()

    def sizes(self):
        sizes = {"database_bytes": 0, "vectors_bytes": 0, "models_bytes": 0, "cache_bytes": 0}
        for name, directory in (
            ("database_bytes", "data"),
            ("models_bytes", "models"),
            ("cache_bytes", "cache"),
        ):
            folder = self.db.workspace / directory
            for base, directories, filenames in os.walk(folder, followlinks=False):
                directories[:] = [
                    name for name in directories if not (Path(base) / name).is_symlink()
                ]
                for filename in filenames:
                    path = Path(base) / filename
                    if path.is_symlink():
                        continue
                    try:
                        count = path.stat().st_size
                    except OSError:
                        continue
                    key = (
                        "vectors_bytes"
                        if directory == "data" and path.is_relative_to(folder / "vectors")
                        else name
                    )
                    sizes[key] += count
        with self.db.connect() as conn:
            sizes["sqlite_free_bytes"] = (
                conn.execute("PRAGMA freelist_count").fetchone()[0]
                * conn.execute("PRAGMA page_size").fetchone()[0]
            )
            sizes["ocr_text_bytes"] = conn.execute(
                "SELECT COALESCE(SUM(length(CAST(text AS BLOB))),0) FROM ocr_cache"
            ).fetchone()[0]
        sizes["total_bytes"] = sum(
            sizes[key] for key in ("database_bytes", "vectors_bytes", "models_bytes", "cache_bytes")
        )
        sizes["reclaim_estimate_bytes"] = sizes["sqlite_free_bytes"]
        sizes["estimate_note"] = (
            "Оценка освобождения SQLite; место в Lance уточняется после уплотнения. "
            "Исходные экспорты не входят в размер приложения."
        )
        return sizes

    def deletion_estimate(self, chat_id):
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*),COALESCE(SUM(length(CAST(text AS BLOB))),0) "
                "FROM messages WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
            shared = conn.execute(
                "SELECT COUNT(DISTINCT r.sha256) FROM media_refs r WHERE r.chat_id=? AND EXISTS "
                "(SELECT 1 FROM media_refs other WHERE other.sha256=r.sha256 AND other.chat_id<>?)",
                (chat_id, chat_id),
            ).fetchone()[0]
            exclusive = conn.execute(
                "SELECT COUNT(DISTINCT r.sha256) FROM media_refs r "
                "WHERE r.chat_id=? AND NOT EXISTS "
                "(SELECT 1 FROM media_refs other WHERE other.sha256=r.sha256 AND other.chat_id<>?)",
                (chat_id, chat_id),
            ).fetchone()[0]
        return {
            "messages": row[0],
            "message_text_bytes": row[1],
            "exclusive_media": exclusive,
            "shared_media": shared,
            "source_files_preserved": True,
            "estimate_note": (
                "Логический объём текста; индексы и страницы SQLite уплотняются отдельно. "
                "Общие OCR/CLIP кэши сохраняются для других диалогов."
            ),
        }
