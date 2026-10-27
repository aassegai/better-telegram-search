"""One latest durable comparison per analysis; no mutable private report files."""

import json
import re
import secrets
import time
from dataclasses import asdict

from telegram_search.search.chunking.noise.coverage import application_ready
from telegram_search.shared.errors import NoiseDraftConflict, UserError
from telegram_search.shared.text import serialize
from telegram_search.storage.noise import ACTIVE, source_fence


class NoiseDryRunStore:
    def __init__(self, db):
        self.db = db

    def recover(self):
        with self.db.connection_timeout(0.2), self.db.connect() as conn:
            conn.execute(
                "UPDATE noise_dry_runs SET state='interrupted',error='interrupted',"
                "summary_json=NULL "
                "WHERE state IN ('queued','running','cancelling')"
            )

    def _parent(self, conn, chat_id, analysis_id):
        row = conn.execute(
            "SELECT * FROM noise_analyses WHERE chat_id=? AND id=?", (chat_id, analysis_id)
        ).fetchone()
        if row is None:
            raise KeyError(analysis_id)
        return row

    def current(self, conn, row):
        parent = self._parent(conn, row["chat_id"], row["analysis_id"])
        if (
            parent["state"] != "ready"
            or parent["draft_revision"] != row["draft_revision"]
            or parent["fence_json"] != row["fence_json"]
            or source_fence(conn, row["chat_id"]) != json.loads(row["fence_json"])
        ):
            return False
        state = conn.execute(
            "SELECT s.active_space_id,c.text_batch FROM semantic_state s "
            "JOIN chats c ON c.id=? WHERE s.id=1",
            (row["chat_id"],),
        ).fetchone()
        return bool(
            state
            and state[0] == row["space_id"]
            and (state[1] or self.db.settings.embedding_batch) == row["batch_size"]
        )

    def create(self, chat_id, analysis_id, revision, options):
        with self.db.connection_timeout(0.2), self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            parent = self._parent(conn, chat_id, analysis_id)
            if parent["draft_revision"] != revision:
                raise NoiseDraftConflict("Проект изменился. Обновите его перед сохранением.")
            if source_fence(conn, chat_id) != json.loads(parent["fence_json"]):
                raise NoiseDraftConflict(
                    "Диалог или исключения изменились. Запустите анализ заново."
                )
            summary = json.loads(parent["summary_json"] or "null")
            if (
                parent["state"] != "ready"
                or not application_ready(summary)
                or not isinstance(summary.get("report_sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", summary["report_sha256"])
            ):
                raise UserError("Нужен полный актуальный анализ. Запустите анализ заново.")
            previous = conn.execute(
                "SELECT state FROM noise_dry_runs WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
            if previous and previous[0] in ACTIVE:
                raise UserError("Сравнение этого отчёта уже выполняется.")
            model = conn.execute(
                "SELECT s.active_space_id,c.text_batch FROM semantic_state s "
                "JOIN chats c ON c.id=? WHERE s.id=1",
                (chat_id,),
            ).fetchone()
            if model is None or model[0] is None:
                raise UserError(
                    "Для dry-run нужен локальный токенизатор активной текстовой модели."
                )
            job_id = secrets.token_hex(16)
            conn.execute("DELETE FROM noise_dry_runs WHERE analysis_id=?", (analysis_id,))
            conn.execute(
                "INSERT INTO noise_dry_runs(id,analysis_id,chat_id,draft_revision,fence_json,"
                "space_id,batch_size,created_at,state,options_json,progress_json) "
                "VALUES(?,?,?,?,?,?,?,?,'queued',?,?)",
                (
                    job_id,
                    analysis_id,
                    chat_id,
                    revision,
                    parent["fence_json"],
                    model[0],
                    model[1] or self.db.settings.embedding_batch,
                    time.time(),
                    serialize(asdict(options)),
                    serialize({"phase": "queued", "scanned_messages": 0, "elapsed_seconds": 0}),
                ),
            )
            return dict(
                conn.execute("SELECT * FROM noise_dry_runs WHERE id=?", (job_id,)).fetchone()
            )

    def get(self, chat_id, analysis_id, job_id):
        with self.db.connection_timeout(0.2), self.db.connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT * FROM noise_dry_runs WHERE chat_id=? AND analysis_id=? AND id=?",
                (chat_id, analysis_id, job_id),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            return {**dict(row), "stale": not self.current(conn, row)}

    def latest(self, chat_id, analysis_id):
        with self.db.connection_timeout(0.2), self.db.connect() as conn:
            self._parent(conn, chat_id, analysis_id)
            row = conn.execute(
                "SELECT id FROM noise_dry_runs WHERE chat_id=? AND analysis_id=?",
                (chat_id, analysis_id),
            ).fetchone()
            return row[0] if row else None

    def progress(self, job_id, value):
        with self.db.connection_timeout(0.2), self.db.connect() as conn:
            conn.execute(
                "UPDATE noise_dry_runs SET state='running',progress_json=? WHERE id=?",
                (serialize(value), job_id),
            )

    def publish(self, conn, job):
        row = conn.execute("SELECT * FROM noise_dry_runs WHERE id=?", (job.id,)).fetchone()
        if row is None:
            raise KeyError(job.id)
        if job.state in {"ready", "limited"} and not self.current(conn, row):
            job.state, job.error = "stale", "source_changed"
        if job.state not in {"ready", "limited"}:
            job.summary = None
        conn.execute(
            "UPDATE noise_dry_runs SET state=?,progress_json=?,summary_json=?,error=? WHERE id=?",
            (
                job.state,
                serialize(job.progress),
                serialize(job.summary) if job.summary is not None else None,
                job.error,
                job.id,
            ),
        )
