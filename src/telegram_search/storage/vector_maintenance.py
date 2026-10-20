"""Explicit, partition-aware vector removal. Canonical messages/OCR are retained."""

import hashlib
import os
import threading
from collections import defaultdict
from pathlib import Path

from telegram_search.security.paths import is_link_or_reparse
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize
from telegram_search.storage.generations import invalidate_segments

KINDS = {
    "text": ("semantic_state", "paused", "text_paused"),
    "images": ("media_state", "paused", "media_paused"),
    "ocr": ("media_state", "ocr_dense_paused", "ocr_dense_paused"),
}


def disk_bytes(path):
    if is_link_or_reparse(path) or not path.exists():
        return 0
    total = 0
    for base, directories, files in os.walk(path, followlinks=False):
        directories[:] = [name for name in directories if not is_link_or_reparse(Path(base) / name)]
        for name in files:
            child = Path(base) / name
            try:
                if not is_link_or_reparse(child):
                    total += child.stat().st_size
            except OSError:
                continue
    return total


class VectorMaintenance:
    def __init__(self, db, lock, semantic, media):
        self.db, self.lock, self.semantic, self.media = db, lock, semantic, media
        self.state_lock = threading.Lock()
        self.stop = threading.Event()
        self.worker = None
        self.job = {"state": "idle", "kind": None, "error": None}

    def status(self):
        with self.state_lock:
            return dict(self.job)

    def _update(self, **values):
        with self.state_lock:
            self.job.update(values)

    @property
    def busy(self):
        return self.status()["state"] in {"waiting", "clearing"}

    @staticmethod
    def _spaces(conn, kind):
        if kind == "text":
            sql = "SELECT id,dimension FROM embedding_spaces"
            args = ()
        else:
            sql = "SELECT id,dimension FROM media_spaces WHERE kind=?"
            args = ("image" if kind == "images" else "ocr",)
        return [dict(row) for row in conn.execute(sql, args)]

    @staticmethod
    def _predicate(kind):
        return "utc_day <> 'media'" if kind == "text" else "utc_day = 'media'"

    def inventory(self):
        groups = {kind: {"vectors": 0, "bytes": 0, "shared": False} for kind in KINDS}
        if not self.semantic.available:
            return {"indexes": groups}
        with self.lock, self.db.connect() as conn:
            store = self.semantic._vector_store()
            partitions = defaultdict(list)
            for kind in KINDS:
                for space in self._spaces(conn, kind):
                    partitions[space["id"]].append((kind, space["dimension"]))
            with store.lock:
                for space, entries in partitions.items():
                    size = disk_bytes(store.table_path(space))
                    table = store.table(space, entries[0][1])
                    if table is None:
                        continue
                    counts = {kind: table.count_rows(self._predicate(kind)) for kind, _ in entries}
                    total = sum(counts.values())
                    for kind, count in counts.items():
                        groups[kind]["vectors"] += count
                        groups[kind]["bytes"] += round(size * count / total) if total else size
                        groups[kind]["shared"] |= len(entries) > 1
        return {"indexes": groups}

    def start(self, kind):
        if kind not in KINDS:
            raise UserError("Неизвестный тип векторного индекса.")
        with self.lock:
            if self.busy:
                raise UserError("Дождитесь очистки векторного индекса.")
            if any(
                task and task.is_alive()
                for task in (self.semantic.preparation, self.media.preparation)
            ):
                raise UserError("Дождитесь завершения подготовки моделей.")
            if not self.semantic.available:
                raise UserError("Для очистки векторов установите runtime semantic или GPU-сборку.")
            table, column, chat_column = KINDS[kind]
            with self.db.connect() as conn:
                conn.execute(f"UPDATE {table} SET {column}=1 WHERE id=1")
                conn.execute(f"UPDATE chats SET {chat_column}=1")
            self._update(state="waiting", kind=kind, error=None, deleted_vectors=0, freed_bytes=0)
            self.worker = threading.Thread(target=self._run, args=(kind,), daemon=True)
            self.worker.start()
        self.semantic.wake.set()
        self.media.wake.set()
        return self.status()

    def _running(self, conn, kind):
        if kind == "text":
            return conn.execute("SELECT 1 FROM index_work WHERE state='running' LIMIT 1").fetchone()
        return ("clip" if kind == "images" else "ocr_dense") in self.media.active_stages

    def _reset(self, conn, kind):
        if kind == "text":
            from telegram_search.storage.chunking import reset_for_rebuild

            reset_for_rebuild(conn)
            chats = [row[0] for row in conn.execute("SELECT id FROM chats")]
            for chat in chats:
                days = {
                    r[0]
                    for r in conn.execute(
                        "SELECT utc_day FROM index_segments WHERE chat_id=?", (chat,)
                    )
                }
                invalidate_segments(conn, chat, days, "vector_clear")
            conn.execute("UPDATE index_segments SET dense_generation=0")
            conn.execute("DELETE FROM vector_segment_cleanup")
            self.semantic.query_cache.clear()
            self.semantic.estimates.cache.clear()
        else:
            sql_kind = "image" if kind == "images" else "ocr"
            conn.execute("DELETE FROM media_embeddings WHERE kind=?", (sql_kind,))
            if kind == "images":
                conn.execute(
                    "DELETE FROM media_failures WHERE space_id IN "
                    "(SELECT id FROM media_spaces WHERE kind='image')"
                )
            else:
                # OCR failures use recognition-version + text-space identities.
                versions = [r[0] for r in conn.execute("SELECT DISTINCT version FROM ocr_cache")]
                spaces = [r[0] for r in conn.execute("SELECT id FROM embedding_spaces")]
                conn.executemany(
                    "DELETE FROM media_failures WHERE space_id=?",
                    [
                        (hashlib.sha256(serialize(["ocr", version, space]).encode()).hexdigest(),)
                        for version in versions
                        for space in spaces
                    ],
                )
            self.media.metrics.reset("clip" if kind == "images" else "ocr_dense")
        # Search pagination must never return hits pointing at cleared vectors.
        conn.execute("UPDATE search_archive_epoch SET generation=generation+1 WHERE id=1")

    def _run(self, kind):
        try:
            while not self.stop.is_set():
                with self.lock:
                    with self.db.connect() as conn:
                        table, column, chat_column = KINDS[kind]
                        conn.execute(f"UPDATE {table} SET {column}=1 WHERE id=1")
                        conn.execute(f"UPDATE chats SET {chat_column}=1")
                        running = self._running(conn, kind)
                    if not running:
                        self._clear_locked(kind)
                        return
                self.stop.wait(0.1)
            self._update(state="interrupted")
        except Exception:
            self._update(
                state="failed", error="Не удалось очистить векторный индекс. Повторите очистку."
            )

    def _clear_locked(self, kind):
        self._update(state="clearing")
        with self.db.connect() as conn:
            spaces = self._spaces(conn, kind)
            # Commit invalidation BEFORE removing files. A crash or disk error
            # leaves an explicitly paused rebuild, never ready-but-missing data.
            self._reset(conn, kind)
        store = self.semantic._vector_store()
        before = sum(disk_bytes(store.table_path(s["id"])) for s in spaces)
        removed = store.clear(spaces, predicate=self._predicate(kind))
        after = sum(disk_bytes(store.table_path(s["id"])) for s in spaces)
        self._update(state="done", deleted_vectors=removed, freed_bytes=max(0, before - after))

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join()
