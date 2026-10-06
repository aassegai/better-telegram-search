import hashlib
import importlib.util
import threading
import time

import psutil

from telegram_search.inference.resources import compute_lock
from telegram_search.security.paths import safe_media_path
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import normalize_text, serialize


class MediaService:
    """Content-addressed OCR/image cache, published only while canonical refs still exist."""

    def __init__(self, db, lifecycle_lock, semantic, *, start_background=True):
        self.db, self.lock, self.semantic = db, lifecycle_lock, semantic
        self.ocr = None
        self.clip = None
        self.running = False
        self.resource_error = None
        self.ocr_staging_ids = set()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.preparation = None
        self.last_used = time.monotonic()
        with db.connect() as conn:
            conn.execute(
                "UPDATE media_state SET preparation_state='interrupted' "
                "WHERE preparation_state='preparing'"
            )
        self._load_existing()
        self.background = None
        if start_background:
            self.background = threading.Thread(target=self._loop, name="media-index", daemon=True)
            self.background.start()

    def _new_ocr(self, settings=None):
        from telegram_search.inference.ocr import OcrEngine

        settings = settings or self.db.settings
        return OcrEngine(
            self.db.workspace,
            threads=settings.cpu_threads,
            max_edge=settings.ocr_max_edge,
            timeout=settings.ocr_timeout_seconds,
        )

    def _new_clip(self):
        try:
            from telegram_search.inference.clip import ClipEncoder
        except ImportError as exc:
            raise UserError("Установите CPU runtime: uv sync --locked --extra semantic.") from exc

        return ClipEncoder(self.db.workspace, threads=self.db.settings.cpu_threads)

    def _load_existing(self):
        with self.db.connect() as conn:
            state = dict(conn.execute("SELECT * FROM media_state WHERE id=1").fetchone())
        try:
            if state["ocr_enabled"]:
                engine = self._new_ocr()
                engine.verify()
                self.ocr = engine
            if state["images_enabled"]:
                self.clip = self._new_clip()
        except UserError as exc:
            with self.db.connect() as conn:
                conn.execute("UPDATE media_state SET error=? WHERE id=1", (str(exc),))

    def prepare(self, kind, *, offline=False):
        if kind not in {"ocr", "images"}:
            raise UserError("Неизвестная модель медиа.")
        with self.lock:
            if self.preparation and self.preparation.is_alive():
                raise UserError("Подготовка медиа уже выполняется.")
            if self.semantic.preparation and self.semantic.preparation.is_alive():
                raise UserError("Дождитесь подготовки текстовой модели.")
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE media_state SET preparation_state='preparing',error=NULL WHERE id=1"
                )
            self.preparation = threading.Thread(
                target=self._prepare, args=(kind, offline), name="media-model-prepare", daemon=True
            )
            self.preparation.start()
        return self.status()

    def _prepare(self, kind, offline):
        engine = None
        try:
            if kind == "ocr":
                engine = self._new_ocr()
                engine.prepare(offline=offline)
            else:
                from telegram_search.config.model_registry import media_registry
                from telegram_search.inference.bundles import BundleStore

                for spec in media_registry().values():
                    BundleStore(self.db.workspace).prepare(spec, offline=offline, repair=True)
                engine = self._new_clip()
                engine.check_contract()
            with self.lock:
                with self.db.connect() as conn:
                    if kind == "ocr":
                        conn.execute("UPDATE media_state SET ocr_enabled=1 WHERE id=1")
                    else:
                        conn.execute("UPDATE media_state SET images_enabled=1 WHERE id=1")
                    conn.execute(
                        "UPDATE media_state SET preparation_state='ready',error=NULL WHERE id=1"
                    )
                if kind == "ocr":
                    self.ocr = engine
                else:
                    old = self.clip
                    self.clip = engine
                    if old:
                        old.unload()
        except Exception:
            if kind == "images" and engine and engine is not self.clip:
                engine.unload()
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE media_state SET preparation_state='failed',error=? WHERE id=1",
                    (
                        "Не удалось подготовить медиа. Проверьте runtime, свободное место "
                        "и доступ к закреплённым моделям.",
                    ),
                )
        finally:
            self.wake.set()

    def control(self, action):
        with self.lock, self.db.connect() as conn:
            if action in {"pause", "resume"}:
                conn.execute("UPDATE media_state SET paused=? WHERE id=1", (action == "pause",))
            elif action == "retry":
                conn.execute("DELETE FROM ocr_cache WHERE state='failed'")
                conn.execute("DELETE FROM media_failures")
                conn.execute("UPDATE media_state SET error=NULL WHERE id=1")
            else:
                raise UserError("Неизвестное действие медиа.")
        self.wake.set()
        return self.status()

    def status(self):
        with self.lock, self.db.connect() as conn:
            state = dict(conn.execute("SELECT * FROM media_state WHERE id=1").fetchone())
            total = conn.execute(
                "SELECT COUNT(DISTINCT sha256) FROM media_refs "
                "WHERE kind='photo' AND status='ready'"
            ).fetchone()[0]
            ready = failed = images = 0
            if self.ocr:
                counts = {
                    row[0]: row[1]
                    for row in conn.execute(
                        "SELECT o.state,COUNT(*) FROM ocr_cache o WHERE o.version=? AND EXISTS "
                        "(SELECT 1 FROM media_refs r WHERE r.sha256=o.sha256 AND r.status='ready' "
                        "AND r.kind='photo') GROUP BY o.state",
                        (self.ocr.version,),
                    )
                }
                ready, failed = counts.get("ready", 0), counts.get("failed", 0)
            nonempty = conn.execute(
                "SELECT COUNT(*) FROM ocr_cache o WHERE version=? AND state='ready' "
                "AND text<>'' AND EXISTS (SELECT 1 FROM media_refs r WHERE r.sha256=o.sha256 "
                "AND r.status='ready' AND r.kind='photo')",
                (self.ocr.version if self.ocr else "",),
            ).fetchone()[0]
            if self.clip:
                images = conn.execute(
                    "SELECT COUNT(*) FROM media_embeddings e WHERE kind='image' "
                    "AND space_id=? AND EXISTS (SELECT 1 FROM media_refs r "
                    "WHERE r.sha256=e.sha256 AND r.status='ready' AND r.kind='photo')",
                    (self.clip.space_id,),
                ).fetchone()[0]
            missing = conn.execute(
                "SELECT COUNT(*) FROM media_refs WHERE kind='photo' AND status<>'ready'"
            ).fetchone()[0]
            ocr_dense = conn.execute(
                "SELECT COUNT(DISTINCT sha256) FROM media_embeddings "
                "WHERE kind='ocr' AND space_id=? AND ocr_version=?",
                (
                    self.semantic.encoder.space_id if self.semantic.encoder else "",
                    self.ocr.version if self.ocr else "",
                ),
            ).fetchone()[0]
        return {
            **state,
            "total_photos": total,
            "ocr_ready": ready,
            "ocr_failed": failed,
            "ocr_dense_ready": ocr_dense,
            "ocr_nonempty_ready": nonempty,
            "images_ready": images,
            "missing_refs": missing,
            "ocr_available": self.ocr is not None,
            "images_available": self.clip is not None,
            "ocr_runtime_installed": importlib.util.find_spec("tesserocr") is not None,
            "running": self.running,
            "resource_error": self.resource_error,
            "device": "cpu",
        }

    def _read_photo(self, sha):
        with self.db.connect() as conn:
            refs = conn.execute(
                "SELECT r.relative_path,s.relative_path AS root FROM media_refs r "
                "JOIN source_roots s ON s.id=r.source_root_id WHERE r.sha256=? "
                "AND r.kind='photo' AND r.status='ready' ORDER BY r.id",
                (sha,),
            ).fetchall()
        for ref in refs:
            try:
                path = safe_media_path(self.db.source_path(ref["root"]), ref["relative_path"])
                with path.open("rb") as stream:
                    data = stream.read(32 * 1024 * 1024 + 1)
                if len(data) <= 32 * 1024 * 1024 and hashlib.sha256(data).hexdigest() == sha:
                    return data
            except (OSError, UserError):
                continue
        raise UserError("Файл недоступен или изменился. Проверьте источник и повторите импорт.")

    def _current(self, conn, sha):
        state = conn.execute("SELECT paused FROM media_state WHERE id=1").fetchone()
        return (
            not self.stop.is_set()
            and not state[0]
            and conn.execute(
                "SELECT 1 FROM media_refs WHERE sha256=? "
                "AND status='ready' AND kind='photo' LIMIT 1",
                (sha,),
            ).fetchone()
            is not None
        )

    def _ocr_one(self):
        if not self.ocr:
            return False
        engine = self.ocr
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT r.sha256 FROM media_refs r WHERE r.kind='photo' "
                "AND r.status='ready' AND NOT EXISTS (SELECT 1 FROM ocr_cache o "
                "WHERE o.sha256=r.sha256 AND o.version=?) LIMIT 1",
                (engine.version,),
            ).fetchone()
        if not row:
            return False
        sha = row[0]
        try:
            data = self._read_photo(sha)
            with compute_lock:
                result = engine.recognize(data)
            state, error = "ready", None
        except UserError as exc:
            result = {"text": "", "confidence": None}
            state, error = "failed", str(exc)
        with self.lock, self.db.connect() as conn:
            if self.ocr is engine and self._current(conn, sha):
                conn.execute(
                    "INSERT INTO ocr_cache(sha256,version,state,text,"
                    "text_normalized,confidence,error) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(sha256,version) DO UPDATE SET state=excluded.state,"
                    "text=excluded.text,text_normalized=excluded.text_normalized,"
                    "confidence=excluded.confidence,error=excluded.error",
                    (
                        sha,
                        engine.version,
                        state,
                        result["text"],
                        normalize_text(result["text"]),
                        result["confidence"],
                        error,
                    ),
                )
        return True

    def _loop(self):
        while not self.stop.is_set():
            try:
                self.cleanup()
                with self.lock, self.db.connect() as conn:
                    paused = conn.execute("SELECT paused FROM media_state WHERE id=1").fetchone()[0]
                    busy = self.preparation and self.preparation.is_alive()
                    self.running = not paused and not busy
                if self.running:
                    if (
                        psutil.Process().memory_info().rss
                        > self.db.settings.memory_limit_mib * 1024**2
                    ):
                        self.resource_error = (
                            "Достигнут предел RAM: медиа ожидает освобождения памяти."
                        )
                    else:
                        self.resource_error = None
                        worked = self._ocr_one()
                        worked |= self._image_batch()
                        worked |= self._ocr_embeddings()
                        if worked:
                            continue
            except Exception:
                self.resource_error = (
                    "Медиа-индекс остановился на ошибке. Проверьте источник и повторите."
                )
            finally:
                self.running = False
            if (
                self.clip
                and time.monotonic() - self.last_used > self.db.settings.idle_unload_seconds
            ):
                self.clip.unload()
            self.wake.wait(1)
            self.wake.clear()

    def _image_batch(self):
        if not self.clip:
            return False
        encoder = self.clip
        with self.db.connect() as conn:
            photos = conn.execute(
                "SELECT DISTINCT r.sha256 FROM media_refs r WHERE r.kind='photo' "
                "AND r.status='ready' AND NOT EXISTS (SELECT 1 FROM media_embeddings e "
                "WHERE e.sha256=r.sha256 AND e.space_id=? AND e.kind='image') "
                "AND NOT EXISTS (SELECT 1 FROM media_failures f WHERE f.sha256=r.sha256 "
                "AND f.space_id=?) LIMIT ?",
                (encoder.space_id, encoder.space_id, self.db.settings.image_batch),
            ).fetchall()
        if not photos:
            return False
        good, data = [], []
        for row in photos:
            try:
                data.append(self._read_photo(row[0]))
                good.append(row[0])
            except UserError as exc:
                self._failure(row[0], encoder.space_id, str(exc))
        if not good:
            return True
        try:
            embeddings = encoder.encode_images(data)
        except Exception:
            # Retry independently to isolate a malformed image from the rest of its batch.
            if len(good) > 1:
                for sha, value in zip(good, data, strict=True):
                    try:
                        self._publish(
                            encoder.space_id, 512, "image", [sha], encoder.encode_images([value])
                        )
                    except Exception:
                        self._failure(sha, encoder.space_id, "CLIP не смог обработать фотографию.")
            else:
                self._failure(good[0], encoder.space_id, "CLIP не смог обработать фотографию.")
        else:
            self._publish(encoder.space_id, 512, "image", good, embeddings)
        self.last_used = time.monotonic()
        return True

    def _ocr_embeddings(self):
        encoder = self.semantic.encoder
        engine = self.ocr
        if not encoder or not engine or self.semantic.preparing_encoder.is_set():
            return False
        failure_identity = hashlib.sha256(
            serialize(["ocr", engine.version, encoder.space_id]).encode()
        ).hexdigest()
        with self.db.connect() as conn:
            if conn.execute("SELECT paused FROM semantic_state WHERE id=1").fetchone()[0]:
                return False
            row = conn.execute(
                "SELECT o.sha256,o.text FROM ocr_cache o WHERE o.version=? AND o.state='ready' "
                "AND o.text<>'' AND EXISTS (SELECT 1 FROM media_refs r WHERE r.sha256=o.sha256 "
                "AND r.kind='photo' AND r.status='ready') AND NOT EXISTS "
                "(SELECT 1 FROM media_embeddings e WHERE e.sha256=o.sha256 AND e.kind='ocr' "
                "AND e.space_id=? AND e.ocr_version=?) AND NOT EXISTS "
                "(SELECT 1 FROM media_failures f WHERE f.sha256=o.sha256 AND f.space_id=?) LIMIT 1",
                (engine.version, encoder.space_id, engine.version, failure_identity),
            ).fetchone()
        if not row:
            return False
        from telegram_search.search.chunks import ChunkBuilder, SourceMessage

        chunks = list(
            ChunkBuilder(encoder.tokenizer, max_messages=1, overlap=0).build(
                [SourceMessage(row["sha256"], 1, 0, "OCR", row["text"])], 1, encoder.space_id
            )
        )
        ids = {
            chunk.id: hashlib.sha256(serialize([engine.version, chunk.id]).encode()).hexdigest()
            for chunk in chunks
        }
        self._register_space(encoder.space_id, encoder.spec.dimension, "ocr")
        with self.lock:
            self.ocr_staging_ids.update(ids.values())
        try:
            for start in range(0, len(chunks), self.db.settings.embedding_batch):
                batch = chunks[start : start + self.db.settings.embedding_batch]
                with self.lock, self.db.connect() as conn:
                    if not self._ocr_guard(conn, row["sha256"], encoder, engine):
                        return True
                embeddings = encoder.encode_text([chunk.text for chunk in batch], "passage")
                # Publish all parts together: no searchable partial OCR on a failed batch.
                with self.lock, self.db.connect() as conn:
                    if not self._ocr_guard(conn, row["sha256"], encoder, engine):
                        return True
                    self.semantic._vector_store().upsert(
                        encoder.space_id,
                        encoder.spec.dimension,
                        [
                            {
                                "id": ids[chunk.id],
                                "chat_id": row["sha256"],
                                "utc_day": "media",
                                "generation": 1,
                            }
                            for chunk in batch
                        ],
                        embeddings,
                    )
            with self.lock, self.db.connect() as conn:
                if self._ocr_guard(conn, row["sha256"], encoder, engine):
                    conn.execute(
                        "INSERT OR IGNORE INTO media_spaces VALUES(?,?,?)",
                        (encoder.space_id, encoder.spec.dimension, "ocr"),
                    )
                    conn.executemany(
                        "INSERT OR IGNORE INTO media_embeddings"
                        "(id,sha256,space_id,kind,ocr_version,ordinal,char_start,char_end) "
                        "VALUES(?,?,?,'ocr',?,?,?,?)",
                        [
                            (
                                ids[chunk.id],
                                row["sha256"],
                                encoder.space_id,
                                engine.version,
                                ordinal,
                                chunk.parts[0].start,
                                chunk.parts[0].end,
                            )
                            for ordinal, chunk in enumerate(chunks)
                        ],
                    )
        except Exception:
            with self.lock, self.db.connect() as conn:
                current = self._ocr_guard(conn, row["sha256"], encoder, engine)
            if current:
                self._failure(
                    row["sha256"], failure_identity, "Не удалось построить смысловой OCR-индекс."
                )
        finally:
            with self.lock:
                self.ocr_staging_ids.difference_update(ids.values())
        return True

    def _ocr_guard(self, conn, sha, encoder, engine):
        state = conn.execute(
            "SELECT paused,enabled,active_space_id FROM semantic_state WHERE id=1"
        ).fetchone()
        return (
            self.semantic.encoder is encoder
            and self.ocr is engine
            and self._current(conn, sha)
            and not self.semantic.preparing_encoder.is_set()
            and state["enabled"]
            and not state["paused"]
            and state["active_space_id"] == encoder.space_id
        )

    def _failure(self, sha, space, error):
        with self.lock, self.db.connect() as conn:
            if self._current(conn, sha):
                conn.execute(
                    "INSERT INTO media_failures VALUES(?,?,?) ON CONFLICT DO NOTHING",
                    (sha, space, error),
                )

    def _publish(self, space, dimension, kind, hashes, embeddings):
        self._register_space(space, dimension, kind)
        with self.lock, self.db.connect() as conn:
            chosen = [i for i, sha in enumerate(hashes) if self._current(conn, sha)]
            rows = [
                {
                    "id": hashlib.sha256(serialize([space, kind, hashes[i]]).encode()).hexdigest(),
                    "chat_id": hashes[i],
                    "utc_day": "media",
                    "generation": 1,
                }
                for i in chosen
            ]
            if not rows:
                return
            self.semantic._vector_store().upsert(space, dimension, rows, embeddings[chosen])
            conn.execute(
                "INSERT OR IGNORE INTO media_spaces VALUES(?,?,?)", (space, dimension, kind)
            )
            conn.executemany(
                "INSERT OR IGNORE INTO media_embeddings(id,sha256,space_id,kind) VALUES(?,?,?,?)",
                [(row["id"], row["chat_id"], space, kind) for row in rows],
            )

    def cleanup(self, *, compact=False):
        with self.lock, self.db.connect() as conn:
            spaces = [dict(row) for row in conn.execute("SELECT * FROM media_spaces")]
            if spaces and not self.semantic.available:
                raise UserError("Для очистки медиа-векторов установите CPU runtime semantic.")
            tombstones = [
                row[0] for row in conn.execute("SELECT sha256 FROM media_vector_cleanup LIMIT 100")
            ]
            for sha in tombstones:
                if (
                    spaces
                    and not conn.execute(
                        "SELECT 1 FROM media_blobs WHERE sha256=?", (sha,)
                    ).fetchone()
                ):
                    self.semantic._vector_store().delete_chat(spaces, sha)
                conn.execute("DELETE FROM media_vector_cleanup WHERE sha256=?", (sha,))
            if compact and spaces:
                if self.ocr:
                    conn.execute("DELETE FROM ocr_cache WHERE version<>?", (self.ocr.version,))
                    conn.execute(
                        "DELETE FROM media_embeddings WHERE kind='ocr' AND ocr_version<>?",
                        (self.ocr.version,),
                    )
                if self.clip:
                    conn.execute(
                        "DELETE FROM media_embeddings WHERE kind='image' AND space_id<>?",
                        (self.clip.space_id,),
                    )
                if self.semantic.encoder:
                    conn.execute(
                        "DELETE FROM media_embeddings WHERE kind='ocr' AND space_id<>?",
                        (self.semantic.encoder.space_id,),
                    )

                def eligible(ids):
                    placeholders = ",".join("?" for _ in ids)
                    return self.ocr_staging_ids | {
                        row[0]
                        for row in conn.execute(
                            f"SELECT id FROM media_embeddings WHERE id IN ({placeholders})", ids
                        )
                    }

                for space in spaces:
                    self.semantic._vector_store().prune_media(
                        space["id"], space["dimension"], eligible
                    )
                self.semantic._vector_store().compact(spaces)

    def _register_space(self, space, dimension, kind):
        # Commit recovery metadata before any non-transactional Lance write.
        with self.lock, self.db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO media_spaces VALUES(?,?,?)", (space, dimension, kind)
            )

    def shutdown(self):
        self.stop.set()
        self.wake.set()
        if self.background:
            self.background.join()
        if self.preparation:
            self.preparation.join()
        if self.clip:
            self.clip.unload()
