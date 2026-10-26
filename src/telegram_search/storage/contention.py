"""Classify transient SQLite contention without exposing SQL, paths, or content."""

import sqlite3


def database_busy(error):
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    return str(error).lower() in {
        "database is locked",
        "database table is locked",
        "database schema is locked",
    }
