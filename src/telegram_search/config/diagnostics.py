import platform
import shutil
import sqlite3
import threading
import time

import psutil

from telegram_search import __version__
from telegram_search.config.hardware import hardware_info
from telegram_search.storage.database import Database


def doctor(db: Database, *, database_check=None, stop=None) -> dict:
    with db.connect() as conn:
        if stop is not None:
            conn.set_progress_handler(lambda: int(stop.is_set()), 10000)
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.fts_probe USING fts5(text)")
        check = database_check or conn.execute("PRAGMA quick_check").fetchone()[0]
        messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        roots = conn.execute("SELECT id,relative_path FROM source_roots").fetchall()
        imports = [
            dict(row)
            for row in conn.execute("SELECT state,COUNT(*) AS count FROM imports GROUP BY state")
        ]
        pending = conn.execute("SELECT COUNT(*) FROM index_work WHERE state='pending'").fetchone()[
            0
        ]
        schema_version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    return {
        "version": __version__,
        "platform": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "fts5": True,
        "database_check": check,
        "device": db.settings.device,
        "models_loaded": 0,
        "dense_available": False,
        "ocr_available": False,
        "ram_available_bytes": psutil.virtual_memory().available,
        "disk_free_bytes": shutil.disk_usage(db.workspace).free,
        "database_bytes": db.path.stat().st_size,
        "messages": messages,
        "sources": [
            {"id": root["id"], "available": db.source_path(root["relative_path"]).is_dir()}
            for root in roots
        ],
        "imports": imports,
        "schema_version": schema_version,
        "pending_index_segments": pending,
        "hardware": hardware_info(db.workspace, device=db.settings.device),
    }


class DiagnosticsBusy(Exception):
    """A foreground operation owns the archive; keep the last diagnostic snapshot."""


class DiagnosticsSnapshot:
    """One background collection, no archive/SQL/model waits in the HTTP handler."""

    def __init__(self, db, collect, *, interval=30):
        self.collect, self.interval = collect, interval
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.worker = None
        self.attempted = float("-inf")
        self.updated = None
        self.state = "idle"
        self.report = {
            "version": __version__,
            "platform": platform.system(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "device": db.settings.device,
            "database_check": "checking",
            "models_loaded": None,
            "dense_available": None,
            "semantic": None,
            "media": None,
        }

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            retry_after = 2 if self.state == "waiting" else self.interval
            if (
                not self.stop.is_set()
                and not (self.worker and self.worker.is_alive())
                and now - self.attempted >= retry_after
            ):
                self.attempted, self.state = now, "collecting"
                self.worker = threading.Thread(target=self._collect, name="doctor", daemon=True)
                self.worker.start()
            return {
                **self.report,
                "diagnostic_snapshot": {
                    "state": self.state,
                    "partial": self.updated is None,
                    "age_seconds": round(now - self.updated, 3)
                    if self.updated is not None
                    else None,
                },
            }

    def _collect(self):
        try:
            value = self.collect(self.stop)
        except DiagnosticsBusy:
            with self.lock:
                self.state = "waiting"
        except Exception:
            # Exceptions from native libraries/SQLite may contain private paths.
            with self.lock:
                self.state = "failed"
        else:
            with self.lock:
                self.report, self.updated, self.state = value, time.monotonic(), "ready"

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=1)
