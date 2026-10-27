"""Small main-DB job records and bounded private per-analysis SQLite reports."""

import hashlib
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict

from telegram_search.search.chunking.classification import whole_word
from telegram_search.search.chunking.noise.contracts import ANALYSIS_VERSION, validate_evidence
from telegram_search.security.paths import is_link_or_reparse
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize
from telegram_search.storage.chunking import chat_policy

ACTIVE = {"queued", "running", "cancelling"}
TERMINAL = {"ready", "limited", "cancelled", "failed", "stale", "interrupted"}


def source_fence(conn, chat_id):
    row = conn.execute("SELECT revision FROM chats WHERE id=?", (chat_id,)).fetchone()
    if row is None:
        raise KeyError(chat_id)
    return {
        "chat_id": chat_id,
        "revision": row[0],
        # This also fences deletion + reimport of an identical chat ID. It is
        # deliberately conservative: another chat's mutation can stale a report.
        "archive_epoch": conn.execute("SELECT generation FROM search_archive_epoch").fetchone()[0],
        "excluded_authors": [
            r[0]
            for r in conn.execute(
                "SELECT author_id FROM index_excluded_authors WHERE chat_id=? ORDER BY author_id",
                (chat_id,),
            )
        ],
        "policy_id": chat_policy(conn, chat_id),
    }


def fence_id(fence):
    return hashlib.sha256(serialize(fence).encode()).hexdigest()


def report_path(workspace, job_id):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise KeyError(job_id)
    path = workspace / "data" / "noise-reports" / (job_id + ".sqlite")
    current = workspace
    for part in path.relative_to(workspace).parts:
        current /= part
        if is_link_or_reparse(current):
            raise UserError("Отчёт анализа не может содержать ссылки или junction.")
    if path.resolve() != path or not path.is_relative_to(workspace):
        raise UserError("Отчёт анализа должен находиться внутри workspace.")
    if any(
        is_link_or_reparse(path.with_name(path.name + suffix))
        for suffix in ("-journal", "-wal", "-shm")
    ):
        raise UserError("Отчёт анализа не может содержать ссылки или junction.")
    return path


class NoiseStore:
    def __init__(self, db):
        self.db = db

    def recover(self):
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE noise_analyses SET state='interrupted',error='interrupted' "
                "WHERE state IN ('queued','running','cancelling')"
            )

    def prune_orphans(self, active_ids=()):
        root = report_path(self.db.workspace, "0" * 32).parent
        if not root.exists():
            return
        with self.db.connection_timeout(0.2), self.db.connect() as conn:
            kept = {row[0] for row in conn.execute("SELECT id FROM noise_analyses")}
        # Chat deletion removes metadata through its FK while the worker may
        # still be unwinding. Never unlink that worker's open private report.
        kept.update(active_ids)
        for number, path in enumerate(root.iterdir()):
            if number >= 128:
                raise UserError("Превышен лимит файлов отчётов анализа.")
            match = re.fullmatch(r"([a-f0-9]{32})\.sqlite(?:-journal)?", path.name)
            if match and match[1] not in kept:
                main = report_path(self.db.workspace, match[1])
                if path in {main, main.with_name(main.name + "-journal")}:
                    path.unlink(missing_ok=True)

    def create(self, chat_id, options, *, active_ids=()):
        self.prune_orphans(active_ids)
        with self.db.connection_timeout(0.2), self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            fence = source_fence(conn, chat_id)
            if conn.execute("SELECT COUNT(*) FROM noise_analyses").fetchone()[0] >= 8:
                raise UserError(
                    "Сохранено 8 анализов. Удалите ненужный отчёт перед новым анализом."
                )
            if conn.execute(
                "SELECT 1 FROM noise_analyses WHERE chat_id=? "
                "AND state IN ('queued','running','cancelling')",
                (chat_id,),
            ).fetchone():
                raise UserError("Анализ этого диалога уже выполняется.")
            job_id = secrets.token_hex(16)
            progress = {
                "phase": "queued",
                "completed": 0,
                "total": None,
                "scanned_messages": 0,
                "whole_messages": 0,
                "forms": 0,
                "elapsed_seconds": 0,
                "version": ANALYSIS_VERSION,
            }
            conn.execute(
                "INSERT INTO noise_analyses(id,chat_id,created_at,state,fence_json,"
                "options_json,progress_json) VALUES(?,?,?,'queued',?,?,?)",
                (
                    job_id,
                    chat_id,
                    time.time(),
                    serialize(fence),
                    serialize(asdict(options)),
                    serialize(progress),
                ),
            )
        return job_id, fence

    def get(self, chat_id, job_id):
        with self.db.connection_timeout(0.2), self.db.connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT * FROM noise_analyses WHERE chat_id=? AND id=?", (chat_id, job_id)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            current = source_fence(conn, chat_id)
        result = dict(row)
        result["stale"] = json.loads(row["fence_json"]) != current
        result["current_policy_id"] = current["policy_id"]
        return result

    def list(self, chat_id):
        with self.db.connection_timeout(0.2), self.db.connect() as conn:
            conn.execute("BEGIN")
            source_fence(conn, chat_id)
            return [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM noise_analyses WHERE chat_id=? ORDER BY created_at DESC,id",
                    (chat_id,),
                )
            ]

    def update(self, job_id, state, progress, summary=None, error=None):
        with self.db.connection_timeout(0.2), self.db.connect() as conn:
            return conn.execute(
                "UPDATE noise_analyses SET state=?,progress_json=?,summary_json=?,"
                "error=? WHERE id=?",
                (
                    state,
                    serialize(progress),
                    serialize(summary) if summary is not None else None,
                    error,
                    job_id,
                ),
            ).rowcount

    def delete(self, chat_id, job_id):
        row = self.get(chat_id, job_id)
        if row["state"] in ACTIVE:
            raise UserError("Сначала остановите анализ.")
        path = report_path(self.db.workspace, job_id)
        with self.db.connection_timeout(0.2), self.db.connect() as conn:
            conn.execute("DELETE FROM noise_analyses WHERE id=? AND chat_id=?", (job_id, chat_id))
        for candidate in (path, path.with_name(path.name + "-journal")):
            if not is_link_or_reparse(candidate):
                candidate.unlink(missing_ok=True)


REPORT_SCHEMA = """
CREATE TABLE identity(job_id TEXT PRIMARY KEY,fence_id TEXT NOT NULL,version TEXT NOT NULL);
CREATE TABLE forms(form TEXT PRIMARY KEY,whole_count INTEGER NOT NULL DEFAULT 0,
 eligible_count INTEGER NOT NULL DEFAULT 0,decision TEXT,payload TEXT);
CREATE TABLE protections(form TEXT,kind TEXT,uses INTEGER NOT NULL,PRIMARY KEY(form,kind));
CREATE TABLE days(form TEXT,day TEXT,PRIMARY KEY(form,day));
CREATE TABLE authors(form TEXT,author_key TEXT,PRIMARY KEY(form,author_key));
CREATE TABLE phrases(form TEXT PRIMARY KEY,uses INTEGER NOT NULL);
CREATE INDEX form_frequency ON forms(whole_count DESC,form);
CREATE TABLE families(seed TEXT PRIMARY KEY,observed_count INTEGER NOT NULL,
 eligible_count INTEGER NOT NULL,omit_count INTEGER NOT NULL,review_count INTEGER NOT NULL,
 keep_count INTEGER NOT NULL);
"""


class NoiseReport:
    def __init__(self, db, job_id, fence, *, create=False):
        self.path = report_path(db.workspace, job_id)
        self.job_id, self.fence = job_id, fence
        self.forms_count = self.phrases_count = 0
        self.limits = set()
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            report_path(db.workspace, job_id)
            with self.path.open("xb"):
                pass
            with self.connect(write=True) as conn:
                conn.executescript(REPORT_SCHEMA)
                conn.execute(
                    "INSERT INTO identity VALUES(?,?,?)",
                    (job_id, fence_id(fence), ANALYSIS_VERSION),
                )

    @contextmanager
    def connect(self, *, write=False):
        # Recheck every open; caller never supplies a report path or SQL.
        workspace = self.path.parents[2]
        report_path(workspace, self.job_id)
        if self.path.stat().st_size > 128 * 1024**2:
            raise UserError("Отчёт анализа несовместим или повреждён.")
        conn = sqlite3.connect(
            self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=0.2
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024**2)
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("PRAGMA cache_size=-2048")
            conn.execute("PRAGMA temp_store=MEMORY")
            if write:
                conn.execute("PRAGMA page_size=4096")
                conn.execute("PRAGMA max_page_count=32768")  # At most 128 MiB per report.
            with conn:
                yield conn
        finally:
            conn.close()

    def verify(self, conn):
        row = conn.execute("SELECT * FROM identity").fetchone()
        if row is None or tuple(row) != (self.job_id, fence_id(self.fence), ANALYSIS_VERSION):
            raise UserError("Отчёт анализа несовместим или повреждён.")

    def digest(self, check=lambda: None):
        report_path(self.path.parents[2], self.job_id)
        if self.path.stat().st_size > 128 * 1024**2:
            raise UserError("Отчёт анализа несовместим или повреждён.")
        result, total = hashlib.sha256(), 0
        with self.path.open("rb") as stream:
            while batch := stream.read(1024**2):
                check()
                total += len(batch)
                if total > 128 * 1024**2:
                    raise UserError("Отчёт анализа несовместим или повреждён.")
                result.update(batch)
        check()
        return result.hexdigest()

    def add_batch(self, batch, max_forms):
        with self.connect(write=True) as conn:
            for item in batch.observations:
                exists = conn.execute("SELECT 1 FROM forms WHERE form=?", (item.form,)).fetchone()
                if not exists and self.forms_count >= max_forms:
                    self.limits.add("form_limit")
                    continue
                conn.execute(
                    "INSERT INTO forms(form,whole_count,eligible_count) VALUES(?,1,?) "
                    "ON CONFLICT(form) DO UPDATE SET whole_count=whole_count+1,"
                    "eligible_count=eligible_count+excluded.eligible_count",
                    (item.form, int(item.eligible)),
                )
                self.forms_count += int(not exists)
                conn.execute("INSERT OR IGNORE INTO days VALUES(?,?)", (item.form, item.day))
                conn.execute(
                    "INSERT OR IGNORE INTO authors VALUES(?,?)", (item.form, item.author_key)
                )
                conn.executemany(
                    "INSERT INTO protections VALUES(?,?,1) ON CONFLICT(form,kind) "
                    "DO UPDATE SET uses=uses+1",
                    [(item.form, kind) for kind in item.protections],
                )
            for form, count in batch.phrases.items():
                exists = conn.execute("SELECT 1 FROM phrases WHERE form=?", (form,)).fetchone()
                if not exists and self.phrases_count >= max_forms:
                    self.limits.add("phrase_form_limit")
                    continue
                conn.execute(
                    "INSERT INTO phrases VALUES(?,?) ON CONFLICT(form) "
                    "DO UPDATE SET uses=uses+excluded.uses",
                    (form, count),
                )
                self.phrases_count += int(not exists)
        if batch.limited_messages:
            self.limits.add("text_limit")

    def forms(self, after="", limit=128):
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT form,whole_count,eligible_count FROM forms WHERE form>? "
                    "ORDER BY form LIMIT ?",
                    (after, limit),
                )
            ]

    def frequency(self, conn, form):
        return conn.execute(
            "SELECT COALESCE((SELECT whole_count FROM forms WHERE form=?),0) + "
            "COALESCE((SELECT uses FROM phrases WHERE form=?),0)",
            (form, form),
        ).fetchone()[0]

    def add_candidates(self, rows):
        limited = 0
        with self.connect(write=True) as conn:
            for count, payload in rows:
                encoded = serialize(payload)
                if len(encoded.encode()) > 8192:
                    self.limits.add("payload_limit")
                    payload = {
                        **payload,
                        "complete": False,
                        "noise": payload["noise"][:4],
                        "useful": payload["useful"][:4],
                    }
                    encoded = serialize(payload)
                limited += int(not payload["complete"])
                conn.execute(
                    "UPDATE forms SET decision=?,payload=? WHERE form=?",
                    (payload["decision"], encoded, count["form"]),
                )
                seed = payload["primary_seed"]
                if seed is not None:
                    values = (
                        seed,
                        count["whole_count"],
                        count["eligible_count"],
                        count["eligible_count"] if payload["decision"] == "omit" else 0,
                        count["eligible_count"] if payload["decision"] == "review" else 0,
                        count["eligible_count"] if payload["decision"] == "keep" else 0,
                    )
                    conn.execute(
                        "INSERT INTO families VALUES(?,?,?,?,?,?) ON CONFLICT(seed) "
                        "DO UPDATE SET observed_count=observed_count+excluded.observed_count,"
                        "eligible_count=eligible_count+excluded.eligible_count,"
                        "omit_count=omit_count+excluded.omit_count,"
                        "review_count=review_count+excluded.review_count,"
                        "keep_count=keep_count+excluded.keep_count",
                        values,
                    )
        return limited

    def page(self, *, decision=None, offset=0, limit=50):
        self._validate_page(offset, limit)
        if decision not in {None, "keep", "review", "omit"}:
            raise UserError("Неизвестное решение фильтра.")
        with self.connect() as conn:
            self.verify(conn)
            clause = " WHERE decision=?" if decision else ""
            params = (decision,) if decision else ()
            rows = conn.execute(
                "SELECT form,whole_count,eligible_count,decision,substr(payload,1,8193) AS payload,"
                "length(CAST(payload AS BLOB)) AS payload_bytes FROM forms"
                + clause
                + " ORDER BY whole_count DESC,form LIMIT ? OFFSET ?",
                (*params, limit + 1, offset),
            ).fetchall()
            output = []
            for row in rows[:limit]:
                payload = row["payload"]
                if payload is not None and (
                    not isinstance(payload, str) or row["payload_bytes"] > 8192
                ):
                    raise UserError("Отчёт анализа несовместим или повреждён.")
                if (
                    not isinstance(row["form"], str)
                    or not 1 <= len(row["form"]) <= 64
                    or whole_word(row["form"]) != row["form"]
                    or type(row["whole_count"]) is not int
                    or not 1 <= row["whole_count"] <= 1000000
                    or type(row["eligible_count"]) is not int
                    or not 0 <= row["eligible_count"] <= row["whole_count"]
                ):
                    raise UserError("Отчёт анализа несовместим или повреждён.")
                candidate = None
                if payload is not None:
                    candidate = validate_evidence(
                        json.loads(payload, parse_constant=self._invalid_constant), row["form"]
                    )
                    if candidate["decision"] != row["decision"]:
                        raise UserError("Отчёт анализа несовместим или повреждён.")
                days = conn.execute(
                    "SELECT COUNT(*) FROM days WHERE form=?", (row["form"],)
                ).fetchone()[0]
                authors = conn.execute(
                    "SELECT COUNT(*) FROM authors WHERE form=?", (row["form"],)
                ).fetchone()[0]
                protections = dict(
                    conn.execute("SELECT kind,uses FROM protections WHERE form=?", (row["form"],))
                )
                if (
                    not 1 <= days <= row["whole_count"]
                    or not 1 <= authors <= row["whole_count"]
                    or any(
                        not isinstance(kind, str)
                        or not 1 <= len(kind) <= 64
                        or not kind.isascii()
                        or type(uses) is not int
                        or not 1 <= uses <= row["whole_count"]
                        for kind, uses in protections.items()
                    )
                ):
                    raise UserError("Отчёт анализа несовместим или повреждён.")
                output.append(
                    {
                        "form": row["form"],
                        "whole_count": row["whole_count"],
                        "eligible_count": row["eligible_count"],
                        "days": days,
                        "authors": authors,
                        "protections": protections,
                        "candidate": candidate,
                    }
                )
            return {"forms": output, "next_offset": offset + limit if len(rows) > limit else None}

    def families(self, *, offset=0, limit=50):
        self._validate_page(offset, limit)
        with self.connect() as conn:
            self.verify(conn)
            rows = conn.execute(
                "SELECT seed,observed_count,eligible_count,omit_count,review_count,keep_count "
                "FROM families ORDER BY observed_count DESC,seed LIMIT ? OFFSET ?",
                (limit + 1, offset),
            ).fetchall()
            for row in rows[:limit]:
                if (
                    not isinstance(row["seed"], str)
                    or not 1 <= len(row["seed"]) <= 64
                    or whole_word(row["seed"]) != row["seed"]
                    or any(
                        type(row[key]) is not int or not 0 <= row[key] <= 1000000
                        for key in (
                            "observed_count",
                            "eligible_count",
                            "omit_count",
                            "review_count",
                            "keep_count",
                        )
                    )
                    or row["eligible_count"] > row["observed_count"]
                    or sum(row[key] for key in ("omit_count", "review_count", "keep_count"))
                    != row["eligible_count"]
                ):
                    raise UserError("Отчёт анализа несовместим или повреждён.")
            return {
                "families": [dict(row) for row in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None,
            }

    @staticmethod
    def _validate_page(offset, limit):
        if (
            type(offset) is not int
            or not 0 <= offset <= 50000
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise UserError("Недопустимая страница отчёта.")

    @staticmethod
    def _invalid_constant(value):
        raise ValueError("Invalid report number")
