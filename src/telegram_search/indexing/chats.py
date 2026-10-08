"""Per-dialog queues over shared, compatible model and media caches."""

from telegram_search.indexing.exclusions import update_authors, validate_authors
from telegram_search.search.chunking.policy import DEFAULT_POLICY, LEGACY_POLICY_ID
from telegram_search.shared.errors import UserError
from telegram_search.storage.generations import bump_revision, invalidate_segments, utc_day


class ChatIndexing:
    def __init__(self, db, semantic, media, lock):
        self.db, self.semantic, self.media, self.lock = db, semantic, media, lock

    def status(self, chat_id):
        with self.db.connect() as conn:
            chat = conn.execute(
                "SELECT text_batch,image_batch,ocr_batch,ocr_region_batch FROM chats WHERE id=?",
                (chat_id,),
            ).fetchone()
            excluded = [
                row[0]
                for row in conn.execute(
                    "SELECT author_id FROM index_excluded_authors WHERE "
                    "chat_id=? ORDER BY author_id",
                    (chat_id,),
                )
            ]
            excluded_messages = conn.execute(
                "SELECT COUNT(*) FROM messages m JOIN index_excluded_authors x "
                "ON x.chat_id=m.chat_id AND x.author_id=m.author_id WHERE m.chat_id=?",
                (chat_id,),
            ).fetchone()[0]
            policy = conn.execute(
                "SELECT policy_id FROM chat_chunking WHERE chat_id=?", (chat_id,)
            ).fetchone()
            policy_id = policy[0] if policy else LEGACY_POLICY_ID
            skipped = conn.execute(
                "SELECT coalesce(sum(w.skipped_messages),0) FROM index_work w "
                "JOIN index_segments s ON s.chat_id=w.chat_id AND s.utc_day=w.utc_day "
                "AND s.target_generation=w.generation WHERE w.chat_id=? AND w.state='done'",
                (chat_id,),
            ).fetchone()[0]
        if chat is None:
            raise UserError("Диалог не найден.")
        return {
            "chunking": {
                "profile": "legacy" if policy_id == LEGACY_POLICY_ID else "episodes",
                "policy_id": policy_id,
                "max_messages": 8,
                "max_tokens": 480,
                "skipped_messages": skipped,
            },
            "excluded_author_ids": excluded,
            "excluded_messages": excluded_messages,
            "semantic": {
                **self.semantic.status(chat_id),
                "batch_size": chat[0] or self.db.settings.embedding_batch,
            },
            "media": {
                **self.media.status(chat_id),
                "batch_size": chat[1] or self.db.settings.image_batch,
                "ocr_batch_size": chat[2] or self.db.settings.ocr_batch_size,
                "ocr_region_batch_size": chat[3]
                or self.db.settings.ocr_region_batch_size
                or (32 if self.db.settings.ocr_device in {"gpu", "hybrid"} else 8),
            },
        }

    def settings(self, chat_id, values):
        fields = {
            "embedding_batch": ("text_batch", 128),
            "image_batch": ("image_batch", 32),
            "ocr_batch_size": ("ocr_batch", 4),
            "ocr_region_batch_size": ("ocr_region_batch", 32),
        }
        if not values or set(values) - (
            fields.keys() | {"excluded_author_ids", "chunking_profile"}
        ):
            raise UserError("Неизвестные настройки индексации.")
        authors = (
            validate_authors(values["excluded_author_ids"])
            if "excluded_author_ids" in values
            else None
        )
        if "chunking_profile" in values and (
            type(values["chunking_profile"]) is not str
            or values["chunking_profile"] not in {"legacy", "episodes"}
        ):
            raise UserError("Неизвестная политика чанкинга.")
        for key, value in values.items():
            if key in {"excluded_author_ids", "chunking_profile"}:
                continue
            if type(value) is not int or not 1 <= value <= fields[key][1]:
                raise UserError("Недопустимый размер батча индексации.")
        with self.lock, self.db.connect() as conn:
            if not conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
                raise UserError("Диалог не найден.")
            for key, value in values.items():
                if key in {"excluded_author_ids", "chunking_profile"}:
                    continue
                conn.execute(f"UPDATE chats SET {fields[key][0]}=? WHERE id=?", (value, chat_id))
            if "chunking_profile" in values:
                selected = values["chunking_profile"]
                if selected not in {"legacy", "episodes"}:
                    raise UserError("Неизвестная политика чанкинга.")
                policy_id = LEGACY_POLICY_ID if selected == "legacy" else DEFAULT_POLICY.identity
                current = conn.execute(
                    "SELECT policy_id FROM chat_chunking WHERE chat_id=?", (chat_id,)
                ).fetchone()
                if (current[0] if current else LEGACY_POLICY_ID) != policy_id:
                    conn.execute(
                        "INSERT OR IGNORE INTO chunking_policies VALUES(?,?)",
                        (DEFAULT_POLICY.identity, DEFAULT_POLICY.json),
                    )
                    conn.execute(
                        "INSERT INTO chat_chunking VALUES(?,?) ON CONFLICT(chat_id) "
                        "DO UPDATE SET policy_id=excluded.policy_id",
                        (chat_id, policy_id),
                    )
                    days = {
                        utc_day(row[0])
                        for row in conn.execute(
                            "SELECT DISTINCT timestamp FROM messages WHERE chat_id=?", (chat_id,)
                        )
                    }
                    invalidate_segments(conn, chat_id, days, "chunking_policy")
                    bump_revision(conn, chat_id)
                    self.semantic.estimates.cache.clear()
            if authors is not None and update_authors(conn, chat_id, authors):
                self.semantic.estimates.cache.clear()
            if {"ocr_batch_size", "ocr_region_batch_size"} & values.keys():
                conn.execute("DELETE FROM index_rates WHERE kind='ocr'")
                self.media.metrics.reset("ocr")
        if "embedding_batch" in values and self.semantic.encoder:
            self.semantic.encoder.index_batch_limit = None
        if "image_batch" in values:
            with self.lock:
                self.media.image_limits = {
                    key: limit
                    for key, limit in self.media.image_limits.items()
                    if key[1] != chat_id
                }
        self.semantic.wake.set()
        self.media.wake.set()
        return self.status(chat_id)

    def control(self, chat_id, kind, action):
        queues = {
            "text": [("text_paused", "semantic_state", "paused")],
            "images": [("media_paused", "media_state", "paused")],
            "ocr": [("ocr_paused", "media_state", "ocr_paused")],
            "ocr_dense": [("ocr_dense_paused", "media_state", "ocr_dense_paused")],
            "media": [
                ("media_paused", "media_state", "paused"),
                ("ocr_paused", "media_state", "ocr_paused"),
                ("ocr_dense_paused", "media_state", "ocr_dense_paused"),
            ],
        }
        if kind not in queues:
            raise UserError("Неизвестное действие индексации.")
        with self.lock, self.db.connect() as conn:
            if not conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
                raise UserError("Диалог не найден.")
            if action == "compact":
                pass
            elif action in {"pause", "resume", "retry"}:
                for column, table, global_column in queues[kind]:
                    if action != "pause":
                        # Transfer each legacy global pause independently, preserving other chats.
                        if conn.execute(
                            f"SELECT {global_column} FROM {table} WHERE id=1"
                        ).fetchone()[0]:
                            conn.execute(f"UPDATE chats SET {column}=1")
                            conn.execute(f"UPDATE {table} SET {global_column}=0 WHERE id=1")
                    conn.execute(
                        f"UPDATE chats SET {column}=? WHERE id=?", (action == "pause", chat_id)
                    )
                if action == "retry":
                    if kind == "text":
                        conn.execute("UPDATE semantic_state SET error=NULL WHERE id=1")
                        conn.execute(
                            "UPDATE index_work SET state='pending',error=NULL "
                            "WHERE chat_id=? AND state='failed'",
                            (chat_id,),
                        )
                    else:
                        if kind in {"media", "ocr"}:
                            conn.execute(
                                "DELETE FROM ocr_cache WHERE state='failed' AND sha256 IN "
                                "(SELECT sha256 FROM media_refs WHERE chat_id=?)",
                                (chat_id,),
                            )
                        failure_space = None
                        if kind == "images" and self.media.clip:
                            failure_space = self.media.clip.space_id
                        elif kind == "ocr_dense" and self.media.ocr and self.semantic.encoder:
                            import hashlib

                            from telegram_search.shared.text import serialize

                            failure_space = hashlib.sha256(
                                serialize(
                                    ["ocr", self.media.ocr.version, self.semantic.encoder.space_id]
                                ).encode()
                            ).hexdigest()
                        if failure_space is not None or kind == "media":
                            conn.execute(
                                "DELETE FROM media_failures WHERE sha256 IN "
                                "(SELECT sha256 FROM media_refs WHERE chat_id=?)"
                                + (" AND space_id=?" if failure_space else ""),
                                (chat_id, failure_space) if failure_space else (chat_id,),
                            )
            else:
                raise UserError("Неизвестное действие индексации.")
        if action == "compact":
            self.semantic.cleanup(compact=True)
            self.media.cleanup(compact=True)
        self.semantic.wake.set()
        self.media.wake.set()
        return self.status(chat_id)

    def initialize(self, kind, **options):
        """Prepare shared models without starting every dialog's queue."""
        with self.lock:
            if kind == "text":
                service, column = self.semantic, "text_paused"
                first = not service.status()["enabled"]
                service.prepare(**options)
            else:
                service = self.media
                column = "ocr_paused" if kind == "ocr" else "media_paused"
                state = service.status()
                first = not state["ocr_enabled" if kind == "ocr" else "images_enabled"]
                service.prepare(kind, offline=options.get("offline", False))
            if first:
                with self.db.connect() as conn:
                    conn.execute(f"UPDATE chats SET {column}=1")
            return service.status()

    def prepare(self, chat_id, kind, **options):
        with self.lock:
            self.status(chat_id)  # Validate before starting any shared preparation.
            if kind == "text":
                first = not self.semantic.status()["enabled"]
                self.semantic.prepare(**options)
                if first:
                    with self.db.connect() as conn:
                        conn.execute("UPDATE chats SET text_paused=1 WHERE id<>?", (chat_id,))
            else:
                state = self.media.status()
                self.media.prepare(kind, offline=options.get("offline", False))
                if not state["ocr_enabled" if kind == "ocr" else "images_enabled"]:
                    with self.db.connect() as conn:
                        column = "ocr_paused" if kind == "ocr" else "media_paused"
                        conn.execute(f"UPDATE chats SET {column}=1 WHERE id<>?", (chat_id,))
            return self.control(chat_id, kind, "resume")
