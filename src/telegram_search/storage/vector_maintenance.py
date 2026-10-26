"""Explicit, partition-aware vector removal. Canonical messages/OCR are retained."""

import hashlib
import os
import threading
from collections import defaultdict
from pathlib import Path

from telegram_search.search.vectors import sql_literal
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
        self.job = {"state": "idle", "kind": None, "chat_id": None, "error": None}

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

    @staticmethod
    def _chat(conn, chat_id):
        if (
            chat_id is not None
            and not conn.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone()
        ):
            raise UserError("Диалог не найден.")

    @staticmethod
    def _blob_scope(chat_id, *, exclusive=False):
        sql = (
            "SELECT DISTINCT r.sha256 FROM media_refs r WHERE r.chat_id=? AND r.sha256 IS NOT NULL"
        )
        if exclusive:
            sql += (
                " AND NOT EXISTS(SELECT 1 FROM media_refs peer "
                "WHERE peer.sha256=r.sha256 AND peer.chat_id<>?)"
            )
        return sql, (chat_id, chat_id) if exclusive else (chat_id,)

    def _partitions(self, conn, kind, chat_id, *, exclusive=False):
        base = self._predicate(kind)
        if chat_id is None:
            return [base]
        if kind == "text":
            return [base + f" AND chat_id={sql_literal(chat_id)}"]
        sql, params = self._blob_scope(chat_id, exclusive=exclusive)
        cursor = conn.execute(sql, params)
        predicates = []
        while rows := cursor.fetchmany(512):
            predicates.append(
                base + " AND chat_id IN (" + ",".join(sql_literal(row[0]) for row in rows) + ")"
            )
        return predicates

    def inventory(self, chat_id=None):
        groups = {
            kind: {
                "vectors": 0,
                "bytes": 0,
                "shared": False,
                "reclaimable_vectors": 0,
                "shared_vectors": 0,
            }
            for kind in KINDS
        }
        with self.db.connect() as conn:
            self._chat(conn, chat_id)
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
                    counts = {
                        kind: sum(
                            table.count_rows(p) for p in self._partitions(conn, kind, chat_id)
                        )
                        for kind, _ in entries
                    }
                    total = table.count_rows()
                    for kind, count in counts.items():
                        exclusive = sum(
                            table.count_rows(p)
                            for p in self._partitions(conn, kind, chat_id, exclusive=True)
                        )
                        groups[kind]["vectors"] += count
                        groups[kind]["reclaimable_vectors"] += exclusive
                        groups[kind]["shared_vectors"] += count - exclusive
                        groups[kind]["bytes"] += (
                            round(size * exclusive / total)
                            if total
                            else (size if chat_id is None else 0)
                        )
                        groups[kind]["shared"] |= len(entries) > 1 or chat_id is not None
        return {"indexes": groups}

    def _pause(self, conn, kind, chat_id):
        table, column, chat_column = KINDS[kind]
        self._chat(conn, chat_id)
        if chat_id is None:
            conn.execute(f"UPDATE {table} SET {column}=1 WHERE id=1")
        conn.execute(
            f"UPDATE chats SET {chat_column}=1" + (" WHERE id=?" if chat_id is not None else ""),
            (chat_id,) if chat_id is not None else (),
        )

    def start(self, kind, chat_id=None):
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
            with self.db.connect() as conn:
                self._pause(conn, kind, chat_id)
            self._update(
                state="waiting",
                kind=kind,
                chat_id=chat_id,
                error=None,
                deleted_vectors=0,
                freed_bytes=0,
            )
            self.worker = threading.Thread(target=self._run, args=(kind, chat_id), daemon=True)
            self.worker.start()
        self.semantic.wake.set()
        self.media.wake.set()
        return self.status()

    def _running(self, conn, kind):
        if kind == "text":
            return conn.execute("SELECT 1 FROM index_work WHERE state='running' LIMIT 1").fetchone()
        return ("clip" if kind == "images" else "ocr_dense") in self.media.active_stages

    def _reset(self, conn, kind, chat_id=None):
        if kind == "text":
            from telegram_search.storage.chunking import reset_for_rebuild

            reset_for_rebuild(conn, chat_id)
            chats = (
                [chat_id]
                if chat_id is not None
                else [row[0] for row in conn.execute("SELECT id FROM chats")]
            )
            for chat in chats:
                days = {
                    r[0]
                    for r in conn.execute(
                        "SELECT utc_day FROM index_segments WHERE chat_id=?", (chat,)
                    )
                }
                invalidate_segments(conn, chat, days, "vector_clear")
            args = (chat_id,) if chat_id is not None else ()
            scope = " WHERE chat_id=?" if chat_id is not None else ""
            conn.execute("UPDATE index_segments SET dense_generation=0" + scope, args)
            conn.execute("DELETE FROM vector_segment_cleanup" + scope, args)
            self.semantic.query_cache.clear()
            self.semantic.estimates.cache.clear()
        else:
            sql_kind = "image" if kind == "images" else "ocr"
            sql, args = self._blob_scope(chat_id, exclusive=True)
            scope = " AND sha256 IN (" + sql + ")" if chat_id is not None else ""
            args = args if chat_id is not None else ()
            if kind == "ocr":
                self.media.invalidate_ocr_dense(
                    lambda sha: (
                        chat_id is None
                        or conn.execute(
                            "SELECT 1 FROM (" + sql + ") WHERE sha256=?", (*args, sha)
                        ).fetchone()
                        is not None
                    )
                )
            conn.execute("DELETE FROM media_embeddings WHERE kind=?" + scope, (sql_kind, *args))
            if kind == "images":
                conn.execute(
                    "DELETE FROM media_failures WHERE space_id IN "
                    "(SELECT id FROM media_spaces WHERE kind='image')" + scope,
                    args,
                )
            else:
                # OCR failures use recognition-version + text-space identities.
                versions = [r[0] for r in conn.execute("SELECT DISTINCT version FROM ocr_cache")]
                spaces = [r[0] for r in conn.execute("SELECT id FROM embedding_spaces")]
                conn.executemany(
                    "DELETE FROM media_failures WHERE space_id=?" + scope,
                    [
                        (
                            hashlib.sha256(serialize(["ocr", version, space]).encode()).hexdigest(),
                            *args,
                        )
                        for version in versions
                        for space in spaces
                    ],
                )
            self.media.metrics.reset("clip" if kind == "images" else "ocr_dense")
        # Search pagination must never return hits pointing at cleared vectors.
        conn.execute("UPDATE search_archive_epoch SET generation=generation+1 WHERE id=1")

    def _run(self, kind, chat_id=None):
        try:
            while not self.stop.is_set():
                with self.lock:
                    with self.db.connect() as conn:
                        self._pause(conn, kind, chat_id)
                        # Per-chat workers recheck the paused chat under this
                        # lifecycle lock before every write. Shared media is kept;
                        # foreign batches need not be stopped or waited for.
                        running = self._running(conn, kind) if chat_id is None else False
                    if not running:
                        self._clear_locked(kind, chat_id)
                        return
                self.stop.wait(0.1)
            self._update(state="interrupted")
        except Exception:
            self._update(
                state="failed", error="Не удалось очистить векторный индекс. Повторите очистку."
            )

    def _clear_locked(self, kind, chat_id=None):
        self._update(state="clearing")
        with self.db.connect() as conn:
            spaces = self._spaces(conn, kind)
            predicates = self._partitions(conn, kind, chat_id, exclusive=True)
            # Commit invalidation BEFORE removing files. A crash or disk error
            # leaves an explicitly paused rebuild, never ready-but-missing data.
            self._reset(conn, kind, chat_id)
        store = self.semantic._vector_store()
        before = sum(disk_bytes(store.table_path(s["id"])) for s in spaces)
        removed = (
            store.clear(spaces, predicate=self._predicate(kind))
            if chat_id is None
            else store.clear_partitions(spaces, predicates)
        )
        after = sum(disk_bytes(store.table_path(s["id"])) for s in spaces)
        self._update(state="done", deleted_vectors=removed, freed_bytes=max(0, before - after))

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join()
