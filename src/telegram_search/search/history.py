"""Durable bounded results, last-ten history and explicit favorites in this workspace."""

import json
import time
import uuid
from contextlib import nullcontext

from telegram_search.config.model_registry import ocr_spec, rerank_spec, visual_specs
from telegram_search.search.browse import BrowseService
from telegram_search.search.cache import SearchCache
from telegram_search.search.spec import SearchSpec
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize


class SearchHistory:
    def __init__(self, db, lifecycle_lock):
        self.db, self.lock = db, lifecycle_lock

    @staticmethod
    def _collect(conn):
        conn.execute(
            "DELETE FROM search_snapshots WHERE NOT EXISTS "
            "(SELECT 1 FROM search_history h WHERE h.snapshot_id=search_snapshots.id) "
            "AND NOT EXISTS (SELECT 1 FROM search_snapshot_owners o "
            "WHERE o.snapshot_id=search_snapshots.id)"
        )

    def recover(self):
        # Process-local jobs never resume after restart. Durable tab owners are
        # independent and must survive this cleanup when tabs are introduced.
        with self.db.connect() as conn:
            conn.execute("DELETE FROM search_snapshot_owners WHERE owner LIKE 'job:%'")
            self._collect(conn)

    @staticmethod
    def _invalidate(conn, revision):
        conn.execute(
            "UPDATE search_snapshots SET result_json=NULL,result_bytes=0,"
            "stale_reason='revision_changed' WHERE archive_revision<>? AND result_json IS NOT NULL",
            (revision,),
        )

    @staticmethod
    def _models(conn, db, result):
        settings = db.settings
        text = conn.execute(
            "SELECT e.id,e.profile,e.manifest_json FROM semantic_state s "
            "JOIN embedding_spaces e ON e.id=s.active_space_id WHERE s.enabled=1"
        ).fetchone()
        media = conn.execute(
            "SELECT images_enabled,ocr_enabled,visual_profile,visual_space_id FROM media_state"
        ).fetchone()
        ocr_version = conn.execute("SELECT version FROM ocr_queue_version WHERE id=1").fetchone()
        rerank = conn.execute("SELECT manifest_id,preparation_state FROM rerank_state").fetchone()
        return {
            "text": {"space_id": text[0], "profile": text[1], "manifest": json.loads(text[2])}
            if text
            else None,
            "images": {
                "profile": media[2],
                "space_id": media[3],
                "manifests": [spec.manifest for spec in visual_specs(media[2]).values()],
            }
            if media[0]
            else None,
            "ocr": {
                "engine": settings.ocr_engine,
                "version": getattr(
                    db, "active_ocr_version", ocr_version[0] if ocr_version else None
                ),
                "builtin_manifest": ocr_spec().identity
                if settings.ocr_engine == "paddle"
                else None,
            }
            if media[1]
            else None,
            "rerank": {
                "manifest_id": rerank[0],
                "state": rerank[1],
                "manifest": rerank_spec().manifest,
                "enabled": settings.giga_rerank_enabled,
                "applied": bool(result.get("rerank_applied")),
            },
        }

    @staticmethod
    def _coverage(conn, result, spec):
        where, params = spec.filters().sql()
        segments = conn.execute(
            "SELECT count(*),coalesce(sum(chunk_generation=target_generation "
            "AND dense_generation=target_generation AND embedding_space_id="
            "(SELECT active_space_id FROM semantic_state WHERE id=1)),0) FROM index_segments s "
            "WHERE EXISTS(SELECT 1 FROM indexable_messages m WHERE m.chat_id=s.chat_id "
            "AND m.timestamp>=unixepoch(s.utc_day) AND m.timestamp<unixepoch(s.utc_day)+86400 "
            f"AND {where})",
            params,
        ).fetchone()
        return {
            "text_segments": {"total": segments[0], "ready": segments[1]},
            "media": {
                key: result.get("coverage", {}).get(key)
                for key in (
                    "total_photos",
                    "images_ready",
                    "ocr_ready",
                    "ocr_nonempty_ready",
                    "ocr_dense_ready",
                )
            },
        }

    def remember(self, spec, result, revision, owner, *, max_bytes, on_publish=None):
        """Caller serializes cancellation and publication; one transaction owns all refs."""
        with self.lock, self.db.connect() as conn:
            if SearchCache.revision(self.db) != revision:
                raise UserError("Архив или настройки изменились. Повторите поиск.")
            existing = conn.execute(
                "SELECT id,favorite FROM search_history WHERE spec_key=?", (spec.identity,)
            ).fetchone()
            history_id = existing[0] if existing else str(uuid.uuid4())
            snapshot_id, completed_at = str(uuid.uuid4()), time.time()
            models, coverage = (
                self._models(conn, self.db, result),
                self._coverage(conn, result, spec),
            )
            value = {
                **result,
                "history_id": history_id,
                "favorite": bool(existing[1]) if existing else False,
                "snapshot_id": snapshot_id,
                "completed_at": completed_at,
                "applied_models": models,
                "snapshot_coverage": coverage,
            }
            raw = serialize(value).encode()
            if len(raw) > max_bytes:
                raise UserError("Выдача слишком велика. Уменьшите размер фрагментов.")
            snapshot = conn.execute(
                "INSERT INTO search_snapshots(id,spec_json,spec_key,archive_revision,completed_at,"
                "models_json,coverage_json,result_json,result_bytes,results_count) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    serialize(spec.model_dump(mode="json")),
                    spec.identity,
                    revision,
                    completed_at,
                    serialize(models),
                    serialize(coverage),
                    raw,
                    len(raw),
                    len(result["results"]),
                ),
            )
            conn.execute(
                "INSERT INTO search_history(id,spec_key,snapshot_id,last_run_at,completion_order) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(spec_key) DO UPDATE SET snapshot_id=excluded.snapshot_id,"
                "last_run_at=excluded.last_run_at,completion_order=excluded.completion_order,"
                "recent=1,view_json='{}'",
                (history_id, spec.identity, snapshot_id, completed_at, snapshot.lastrowid),
            )
            conn.execute(
                "INSERT INTO search_snapshot_owners(owner,snapshot_id) VALUES(?,?) "
                "ON CONFLICT(owner) DO UPDATE SET snapshot_id=excluded.snapshot_id",
                (owner, snapshot_id),
            )
            conn.execute(
                "UPDATE search_history SET recent=0 WHERE recent=1 AND id NOT IN "
                "(SELECT id FROM search_history WHERE recent=1 "
                "ORDER BY completion_order DESC,id LIMIT 10)"
            )
            conn.execute("DELETE FROM search_history WHERE recent=0 AND favorite=0")
            self._invalidate(conn, revision)
            if on_publish is not None:
                on_publish(conn, snapshot_id)
            self._collect(conn)
        return raw

    def release(self, owner):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM search_snapshot_owners WHERE owner=?", (owner,))
            self._collect(conn)

    @staticmethod
    def _entry(row):
        return {
            "id": row["history_id"],
            "snapshot_id": row["id"],
            "spec": json.loads(row["spec_json"]),
            "completed_at": row["completed_at"],
            "favorite": bool(row["favorite"]),
            "recent": bool(row["recent"]),
            "cache_available": bool(row["result_bytes"]),
            "stale_reason": row["stale_reason"],
            "cached_results": row["results_count"],
            "models": json.loads(row["models_json"]),
            "coverage": json.loads(row["coverage_json"]),
        }

    @staticmethod
    def _rows(*, include_result=False, snapshot_id=None):
        columns = (
            "s.id,s.spec_json,s.completed_at,s.models_json,s.coverage_json,"
            "s.result_bytes,s.results_count,s.stale_reason"
        )
        return (
            f"SELECT {columns}"
            + (",s.result_json" if include_result else "")
            + ",h.id AS history_id,h.favorite,h.recent,h.view_json FROM search_history h "
            + (
                "JOIN search_snapshots s ON s.id=h.snapshot_id "
                if snapshot_id is None
                else "JOIN search_snapshots s ON s.id=? AND s.spec_key=h.spec_key "
            )
        )

    def list(self, *, offset=0, limit=50):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise UserError("Недопустимые параметры страницы.")
        with self.lock, self.db.connect() as conn:
            self._invalidate(conn, SearchCache.revision(self.db))
            rows = conn.execute(
                self._rows() + "ORDER BY h.completion_order DESC,h.id LIMIT ? OFFSET ?",
                (limit + 1, offset),
            ).fetchall()
            return {
                "entries": [self._entry(row) for row in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None,
            }

    def get(self, history_id, *, offset=0, limit=None, cursor=None, snapshot_id=None):
        if (
            type(offset) is not int
            or not 0 <= offset <= 100
            or (limit is not None and (type(limit) is not int or not 1 <= limit <= 100))
        ):
            raise UserError("Недопустимые параметры страницы.")
        with self.lock, self.db.connect() as conn:
            self._invalidate(conn, SearchCache.revision(self.db))
            row = conn.execute(
                self._rows(include_result=True, snapshot_id=snapshot_id) + "WHERE h.id=?",
                (*([snapshot_id] if snapshot_id else []), history_id),
            ).fetchone()
            if not row:
                raise KeyError(history_id)
            return self._restore(conn, row, offset=offset, limit=limit, cursor=cursor)

    def _restore(self, conn, row, *, offset=0, limit=None, cursor=None, visible_count=None):
        entry = self._entry(row)
        value = json.loads(row["result_json"]) if row["result_json"] is not None else None
        if value is not None:
            spec = SearchSpec.model_validate(entry["spec"])
            value.update(
                snapshot_id=entry["snapshot_id"], history_id=entry["id"], favorite=entry["favorite"]
            )
            if spec.browsing and offset:
                raise UserError("Для просмотра сообщений используйте курсор страницы.")
            if cursor and not spec.browsing:
                raise UserError("Курсор доступен только для просмотра сообщений.")
            size = (
                limit
                if limit is not None
                else min(
                    100,
                    visible_count
                    or json.loads(row["view_json"]).get(
                        "visible_count", entry["spec"]["page_size"]
                    ),
                )
            )
            if cursor:
                spec = SearchSpec.model_validate(entry["spec"])
                value = {
                    **value,
                    **BrowseService(self.db).search(spec, cursor=cursor, limit=size),
                }
                self.append_browse(row["id"], cursor, value, connection=conn)
                return {
                    "entry": entry,
                    "result": {**value, "offset": 0, "next_offset": None, "limit": size},
                }
            end = min(offset + size, len(value["results"]))
            if spec.browsing and end < row["results_count"] and end:
                epoch = conn.execute(
                    "SELECT generation FROM search_archive_epoch WHERE id=1"
                ).fetchone()[0]
                value["browse_cursor"] = BrowseService.cursor_for(
                    spec, epoch, value["results"][end - 1]
                )
            if offset:
                conn.execute(
                    "UPDATE search_history SET view_json=? WHERE snapshot_id=?",
                    (serialize({"visible_count": end}), row["id"]),
                )
            value = {
                **value,
                "results": value["results"][offset:end],
                "offset": offset,
                "next_offset": end if not spec.browsing and end < row["results_count"] else None,
                "cached_results": row["results_count"],
                "limit": limit if limit is not None else entry["spec"]["page_size"],
                "has_more": end < row["results_count"] or value.get("has_more", False),
            }
        return {"entry": entry, "result": value}

    def metadata(self, history_id, *, snapshot_id=None):
        with self.lock, self.db.connect() as conn:
            self._invalidate(conn, SearchCache.revision(self.db))
            row = conn.execute(
                self._rows(snapshot_id=snapshot_id) + "WHERE h.id=?",
                (*([snapshot_id] if snapshot_id else []), history_id),
            ).fetchone()
            if not row:
                raise KeyError(history_id)
            return self._entry(row)

    @staticmethod
    def snapshot_row(conn, snapshot_id, *, include_result=False):
        columns = (
            "s.id,s.spec_json,s.completed_at,s.models_json,s.coverage_json,"
            "s.result_bytes,s.results_count,s.stale_reason"
        )
        return conn.execute(
            f"SELECT {columns}"
            + (",s.result_json" if include_result else "")
            + ",h.id AS history_id,coalesce(h.favorite,0) AS favorite,"
            "coalesce(h.recent,0) AS recent,'{}' AS view_json FROM search_snapshots s "
            "LEFT JOIN search_history h ON h.spec_key=s.spec_key WHERE s.id=?",
            (snapshot_id,),
        ).fetchone()

    def favorite_snapshot(self, snapshot_id, selected):
        """A tab may outlive the last-ten history record for its performed intent."""
        with self.lock, self.db.connect() as conn:
            snapshot = conn.execute(
                "SELECT rowid,* FROM search_snapshots WHERE id=?", (snapshot_id,)
            ).fetchone()
            if not snapshot:
                raise KeyError(snapshot_id)
            if selected:
                conn.execute(
                    "INSERT INTO search_history(id,spec_key,snapshot_id,last_run_at,"
                    "completion_order,recent,favorite) VALUES(?,?,?,?,?,0,1) "
                    "ON CONFLICT(spec_key) DO UPDATE SET favorite=1",
                    (
                        str(uuid.uuid4()),
                        snapshot["spec_key"],
                        snapshot_id,
                        snapshot["completed_at"],
                        snapshot["rowid"],
                    ),
                )
            else:
                conn.execute(
                    "UPDATE search_history SET favorite=0 WHERE spec_key=?", (snapshot["spec_key"],)
                )
                conn.execute("DELETE FROM search_history WHERE recent=0 AND favorite=0")
            self._collect(conn)
            return self._entry(self.snapshot_row(conn, snapshot_id))

    def note_view(self, snapshot_id, visible_count):
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE search_history SET view_json=? WHERE snapshot_id=?",
                (serialize({"visible_count": min(100, visible_count)}), snapshot_id),
            )

    def append_browse(self, snapshot_id, cursor, page, *, max_bytes=32 * 1024**2, connection=None):
        manager = self.db.connect() if connection is None else nullcontext(connection)
        with self.lock, manager as conn:
            row = conn.execute(
                "SELECT result_json FROM search_snapshots WHERE id=?", (snapshot_id,)
            ).fetchone()
            if not row or row[0] is None:
                return
            value = json.loads(row[0])
            if value.get("browse_cursor") != cursor:
                return  # Replayed/old pages cannot move an already advanced window backwards.
            values = [*value["results"], *page["results"]][-SearchCache.capacity :]
            value.update(
                results=values, browse_cursor=page.get("browse_cursor"), has_more=page["has_more"]
            )
            raw = serialize(value).encode()
            if len(raw) > max_bytes:
                raise UserError("Выдача слишком велика. Уменьшите размер фрагментов.")
            conn.execute(
                "UPDATE search_snapshots SET result_json=?,result_bytes=?,results_count=? "
                "WHERE id=?",
                (raw, len(raw), len(values), snapshot_id),
            )
            conn.execute(
                "UPDATE search_history SET view_json=? WHERE snapshot_id=?",
                (serialize({"visible_count": len(values)}), snapshot_id),
            )

    def favorite(self, history_id, selected):
        with self.lock, self.db.connect() as conn:
            changed = conn.execute(
                "UPDATE search_history SET favorite=? WHERE id=?", (int(selected), history_id)
            ).rowcount
            if not changed:
                raise KeyError(history_id)
            conn.execute("DELETE FROM search_history WHERE recent=0 AND favorite=0")
            self._collect(conn)

    def clear(self, kind):
        with self.lock, self.db.connect() as conn:
            if kind == "cache":
                conn.execute(
                    "UPDATE search_snapshots SET result_json=NULL,result_bytes=0,"
                    "stale_reason='cache_cleared'"
                )
            elif kind == "history":
                conn.execute("UPDATE search_history SET recent=0")
                conn.execute("DELETE FROM search_history WHERE favorite=0")
            elif kind == "favorites":
                conn.execute("UPDATE search_history SET favorite=0")
                conn.execute("DELETE FROM search_history WHERE recent=0")
            else:
                raise ValueError("Unknown saved search collection")
            self._collect(conn)
