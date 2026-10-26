"""Workspace-local tabs retain drafts and bounded snapshots, never running inference."""

import json
import uuid
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from telegram_search.search.cache import SearchCache
from telegram_search.search.spec import SearchSpec
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize


class TabConflict(UserError):
    pass


class SearchDraft(SearchSpec):
    # Editing is allowed to pass through incomplete dates and no selected modalities.
    # Only the executable SearchSpec is accepted by the jobs endpoint.
    modalities: list[Literal["text", "images", "ocr"]] = Field(
        default_factory=lambda: ["text", "images", "ocr"], max_length=3
    )

    @model_validator(mode="after")
    def validate_dates(self):
        return self


class TabView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visible_count: int = Field(default=0, ge=0, le=100, strict=True)
    scroll_y: float = Field(default=0, ge=0, le=1_000_000_000, allow_inf_nan=False)
    display_settings_open: bool = Field(default=False, strict=True)


class TabPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    draft: SearchDraft | None = None
    view: TabView | None = None
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=64)


class SearchTabs:
    capacity = 8

    def __init__(self, db, history, jobs):
        self.db, self.history, self.jobs = db, history, jobs
        self.lock = history.lock

    def recover(self):
        with self.lock, self.db.connect() as conn:
            conn.execute(
                "UPDATE search_tabs SET interrupted=1,run_job_id=NULL,"
                "last_state='interrupted',error=NULL WHERE run_job_id IS NOT NULL"
            )

    @staticmethod
    def _row(conn, tab_id):
        row = conn.execute("SELECT * FROM search_tabs WHERE id=?", (tab_id,)).fetchone()
        if not row:
            raise KeyError(tab_id)
        return row

    def _entry(self, conn, row):
        snapshot = (
            self.history.snapshot_row(conn, row["snapshot_id"]) if row["snapshot_id"] else None
        )
        return {
            "id": row["id"],
            "kind": row["kind"],
            "position": row["position"],
            "revision": row["revision"],
            "draft": json.loads(row["draft_json"]),
            "view": TabView.model_validate(json.loads(row["view_json"])).model_dump(),
            "snapshot": self.history._entry(snapshot) if snapshot else None,
            "job_id": row["run_job_id"],
            "interrupted": bool(row["interrupted"]),
            "last_state": row["last_state"],
            "error": row["error"],
        }

    def list(self):
        self._settle()
        with self.lock, self.db.connect() as conn:
            self.history._invalidate(conn, SearchCache.revision(self.db))
            return {
                "tabs": [
                    self._entry(conn, row)
                    for row in conn.execute("SELECT * FROM search_tabs ORDER BY position")
                ],
                "active_tab_id": conn.execute(
                    "SELECT active_tab_id FROM search_tab_state WHERE id=1"
                ).fetchone()[0],
                "capacity": self.capacity,
            }

    def create(self, draft=None, *, snapshot_id=None, view=None):
        draft = draft or SearchDraft(
            page_size=self.db.settings.search_result_limit,
            chunk_size=self.db.settings.display_chunk_size,
        )
        with self.lock, self.db.connect() as conn:
            count, position = conn.execute(
                "SELECT count(*),coalesce(max(position),-1)+1 FROM search_tabs"
            ).fetchone()
            if count >= self.capacity:
                raise TabConflict("Достигнут лимит открытых вкладок. Закройте лишние вкладки.")
            tab_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO search_tabs(id,position,draft_json,view_json) VALUES(?,?,?,?)",
                (
                    tab_id,
                    position,
                    serialize(draft.model_dump(mode="json")),
                    serialize((view or TabView()).model_dump()),
                ),
            )
            if snapshot_id:
                self._bind(conn, tab_id, snapshot_id, clone_browse=True)
            conn.execute("UPDATE search_tab_state SET active_tab_id=? WHERE id=1", (tab_id,))
            return self._entry(conn, self._row(conn, tab_id))

    def _bind(self, conn, tab_id, snapshot_id, *, clone_browse=False):
        self._row(conn, tab_id)
        if snapshot_id:
            row = conn.execute(
                "SELECT * FROM search_snapshots WHERE id=?", (snapshot_id,)
            ).fetchone()
            if not row:
                raise KeyError(snapshot_id)
            if clone_browse and not json.loads(row["spec_json"])["query"].strip():
                # Browse windows advance; each tab needs its own bounded cursor/window.
                snapshot_id = str(uuid.uuid4())
                columns = list(row.keys())
                conn.execute(
                    f"INSERT INTO search_snapshots({','.join(columns)}) "
                    f"VALUES({','.join('?' for _ in columns)})",
                    [snapshot_id if key == "id" else row[key] for key in columns],
                )
            conn.execute(
                "INSERT INTO search_snapshot_owners(owner,snapshot_id) VALUES(?,?) "
                "ON CONFLICT(owner) DO UPDATE SET snapshot_id=excluded.snapshot_id",
                ("tab:" + tab_id, snapshot_id),
            )
        else:
            conn.execute("DELETE FROM search_snapshot_owners WHERE owner=?", ("tab:" + tab_id,))
        conn.execute("UPDATE search_tabs SET snapshot_id=? WHERE id=?", (snapshot_id, tab_id))
        self.history._collect(conn)

    def patch(self, tab_id, patch):
        self._settle(tab_id)
        with self.lock, self.db.connect() as conn:
            row = self._row(conn, tab_id)
            if row["revision"] != patch.revision:
                raise TabConflict("Вкладка изменена в другом окне. Обновите её состояние.")
            if "snapshot_id" in patch.model_fields_set:
                if row["run_job_id"]:
                    raise TabConflict("Остановите поиск перед заменой сохранённой выдачи.")
                if patch.snapshot_id != row["snapshot_id"]:
                    self._bind(conn, tab_id, patch.snapshot_id, clone_browse=True)
            if patch.draft is not None:
                conn.execute(
                    "UPDATE search_tabs SET draft_json=? WHERE id=?",
                    (serialize(patch.draft.model_dump(mode="json")), tab_id),
                )
            if patch.view is not None:
                conn.execute(
                    "UPDATE search_tabs SET view_json=? WHERE id=?",
                    (serialize(patch.view.model_dump()), tab_id),
                )
            conn.execute("UPDATE search_tabs SET revision=revision+1 WHERE id=?", (tab_id,))
            return self._entry(conn, self._row(conn, tab_id))

    def activate(self, tab_id):
        with self.lock, self.db.connect() as conn:
            self._row(conn, tab_id)
            conn.execute("UPDATE search_tab_state SET active_tab_id=? WHERE id=1", (tab_id,))

    def close(self, tab_id):
        # Same lock order as publication: lifecycle -> job cancellation -> SQLite.
        with self.lock:
            with self.db.connect() as conn:
                row = self._row(conn, tab_id)
                job_id = row["run_job_id"]
            if job_id:
                try:
                    self.jobs.cancel(job_id)
                except KeyError:
                    pass
            with self.db.connect() as conn:
                active = conn.execute(
                    "SELECT active_tab_id FROM search_tab_state WHERE id=1"
                ).fetchone()[0]
                conn.execute("DELETE FROM search_tabs WHERE id=?", (tab_id,))
                conn.execute("DELETE FROM search_snapshot_owners WHERE owner=?", ("tab:" + tab_id,))
                if active == tab_id:
                    neighbor = conn.execute(
                        "SELECT id FROM search_tabs ORDER BY abs(position-?),position LIMIT 1",
                        (row["position"],),
                    ).fetchone()
                    conn.execute(
                        "UPDATE search_tab_state SET active_tab_id=? WHERE id=1",
                        (neighbor[0] if neighbor else None,),
                    )
                self.history._collect(conn)

    def start(self, tab_id, spec):
        with self.lock:
            self._settle(tab_id)
            with self.db.connect() as conn:
                if self._row(conn, tab_id)["run_job_id"]:
                    raise TabConflict("В этой вкладке уже выполняется поиск.")
            job = self.jobs.create(spec, tab_id=tab_id)
            try:
                with self.db.connect() as conn:
                    conn.execute(
                        "UPDATE search_tabs SET run_job_id=?,interrupted=0,"
                        "last_state='queued',error=NULL WHERE id=?",
                        (job["job_id"], tab_id),
                    )
            except Exception:
                # Worker publication waits for lifecycle_lock. A failed durable
                # registration must not leave an orphan search running afterward.
                self.jobs.cancel(job["job_id"])
                raise
            return job

    def finish(self, job, snapshot_id=None, *, connection=None):
        # Caller holds lifecycle/publication locks; closed or superseded tabs stay closed.
        manager = self.db.connect() if connection is None else nullcontext(connection)
        with manager as conn:
            row = conn.execute(
                "SELECT id FROM search_tabs WHERE run_job_id=?", (job.id,)
            ).fetchone()
            if not row:
                return
            if snapshot_id:
                self._bind(conn, row[0], snapshot_id)
                conn.execute(
                    "UPDATE search_tabs SET view_json='{}',revision=revision+1 WHERE id=?",
                    (row[0],),
                )
            conn.execute(
                "UPDATE search_tabs SET run_job_id=NULL,last_state=?,error=? WHERE id=?",
                ("completed" if snapshot_id else job.state, job.error, row[0]),
            )

    def result(self, tab_id, *, offset=0, limit=None, cursor=None, snapshot_id=None):
        with self.lock, self.db.connect() as conn:
            self.history._invalidate(conn, SearchCache.revision(self.db))
            tab = self._row(conn, tab_id)
            if snapshot_id is not None and snapshot_id != tab["snapshot_id"]:
                raise TabConflict("Сохранённая выдача изменилась. Откройте вкладку заново.")
            row = self.history.snapshot_row(conn, tab["snapshot_id"], include_result=True)
            if not row:
                return {"entry": None, "result": None}
            view = TabView.model_validate(json.loads(tab["view_json"]))
            return self.history._restore(
                conn,
                row,
                offset=offset,
                limit=limit,
                cursor=cursor,
                visible_count=view.visible_count,
            )

    def metadata(self, tab_id):
        self._settle(tab_id)
        with self.lock, self.db.connect() as conn:
            self.history._invalidate(conn, SearchCache.revision(self.db))
            return self._entry(conn, self._row(conn, tab_id))

    def _settle(self, tab_id=None):
        """Retry terminal cleanup after a transient SQLite error; never rerun jobs."""
        if self.jobs is None:
            return
        with self.lock:
            with self.db.connect() as conn:
                pending = conn.execute(
                    "SELECT id,run_job_id FROM search_tabs WHERE run_job_id IS NOT NULL"
                    + (" AND id=?" if tab_id is not None else ""),
                    (tab_id,) if tab_id is not None else (),
                ).fetchall()
            for row in pending:
                try:
                    status = self.jobs.status(row["run_job_id"])
                except KeyError:
                    with self.db.connect() as conn:
                        conn.execute(
                            "UPDATE search_tabs SET run_job_id=NULL,interrupted=1,"
                            "last_state='interrupted',error=NULL WHERE id=? AND run_job_id=?",
                            (row["id"], row["run_job_id"]),
                        )
                else:
                    if status["state"] in {"completed", "cancelled", "failed"}:
                        self.finish(
                            SimpleNamespace(
                                id=status["job_id"],
                                state=status["state"],
                                error=status["error"],
                            )
                        )

    def favorite(self, tab_id, selected):
        with self.lock:
            with self.db.connect() as conn:
                snapshot_id = self._row(conn, tab_id)["snapshot_id"]
            if not snapshot_id:
                raise KeyError(tab_id)
            return self.history.favorite_snapshot(snapshot_id, selected)
