import platform
import shutil
import sqlite3

import psutil

from telegram_search.storage.database import Database


def doctor(db: Database) -> dict:
    with db.connect() as conn:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.fts_probe USING fts5(text)")
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
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
        "version": "0.1.0",
        "platform": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "fts5": True,
        "database_check": check,
        "device": "cpu",
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
    }
