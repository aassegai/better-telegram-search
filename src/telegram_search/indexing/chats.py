"""Per-dialog queues over shared, compatible model and media caches."""

from telegram_search.shared.errors import UserError


class ChatIndexing:
    def __init__(self, db, semantic, media, lock):
        self.db, self.semantic, self.media, self.lock = db, semantic, media, lock

    def status(self, chat_id):
        with self.db.connect() as conn:
            chat = conn.execute(
                "SELECT text_batch,image_batch FROM chats WHERE id=?", (chat_id,)
            ).fetchone()
        if chat is None:
            raise UserError("Диалог не найден.")
        return {
            "semantic": {
                **self.semantic.status(chat_id),
                "batch_size": chat[0] or self.db.settings.embedding_batch,
            },
            "media": {
                **self.media.status(chat_id),
                "batch_size": chat[1] or self.db.settings.image_batch,
            },
        }

    def settings(self, chat_id, values):
        fields = {"embedding_batch": ("text_batch", 128), "image_batch": ("image_batch", 32)}
        if not values or set(values) - fields.keys():
            raise UserError("Неизвестные настройки индексации.")
        for key, value in values.items():
            if type(value) is not int or not 1 <= value <= fields[key][1]:
                raise UserError("Недопустимый размер батча индексации.")
        with self.lock, self.db.connect() as conn:
            if not conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
                raise UserError("Диалог не найден.")
            for key, value in values.items():
                conn.execute(f"UPDATE chats SET {fields[key][0]}=? WHERE id=?", (value, chat_id))
        if "image_batch" in values:
            with self.lock:
                self.media.image_limits = {
                    key: limit
                    for key, limit in self.media.image_limits.items()
                    if key[1] != chat_id
                }
        return self.status(chat_id)

    def control(self, chat_id, kind, action):
        column, table = (
            ("text_paused", "semantic_state") if kind == "text" else ("media_paused", "media_state")
        )
        with self.lock, self.db.connect() as conn:
            if not conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
                raise UserError("Диалог не найден.")
            if action == "compact":
                pass
            elif action in {"pause", "resume", "retry"}:
                if action != "pause":
                    # Transfer a legacy workspace-wide pause to every dialog before
                    # resuming this one. Other paused queues must remain paused.
                    if conn.execute(f"SELECT paused FROM {table} WHERE id=1").fetchone()[0]:
                        conn.execute(f"UPDATE chats SET {column}=1")
                        conn.execute(f"UPDATE {table} SET paused=0 WHERE id=1")
                conn.execute(
                    f"UPDATE chats SET {column}=? WHERE id=?", (action == "pause", chat_id)
                )
                if action == "retry":
                    if kind == "text":
                        conn.execute(
                            "UPDATE index_work SET state='pending',error=NULL "
                            "WHERE chat_id=? AND state='failed'",
                            (chat_id,),
                        )
                    else:
                        for target in ("ocr_cache", "media_failures"):
                            condition = "state='failed' AND " if target == "ocr_cache" else ""
                            conn.execute(
                                f"DELETE FROM {target} WHERE {condition}sha256 IN "
                                "(SELECT sha256 FROM media_refs WHERE chat_id=?)",
                                (chat_id,),
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
                service, column = self.media, "media_paused"
                state = service.status()
                first = not state["ocr_enabled"] and not state["images_enabled"]
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
                if not state["ocr_enabled"] and not state["images_enabled"]:
                    with self.db.connect() as conn:
                        conn.execute("UPDATE chats SET media_paused=1 WHERE id<>?", (chat_id,))
            return self.control(chat_id, "text" if kind == "text" else "media", "resume")
