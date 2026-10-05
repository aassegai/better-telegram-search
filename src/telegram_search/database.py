import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .errors import UserError
from .privacy import repository_warning


class Database:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.path = self.workspace / "data" / "app.sqlite"

    def initialize(self) -> None:
        if repository_warning(self.workspace):
            raise UserError(
                "Workspace внутри Git-репозитория должен быть целиком исключён в .gitignore. "
                "Выберите папку вне репозитория или добавьте правило до запуска."
            )
        for folder in ("data", "cache", "models"):
            (self.workspace / folder).mkdir(parents=True, exist_ok=True)
        config = self.workspace / "config.json"
        if not config.exists():
            config.write_text(
                json.dumps({"version": 1, "device": "cpu", "search_backend": "fts5_messages"}),
                encoding="utf-8",
            )
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)"
            )
            version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            if version is None:
                conn.executescript(Path(__file__).with_name("schema.sql").read_text())
            elif version != 1:
                raise UserError("Версия базы не поддерживается этим приложением.")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def checkpoint(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def rebuild(self) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO message_fts(message_fts) VALUES ('rebuild')")

    def compact(self) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM media_blobs WHERE sha256 NOT IN "
                "(SELECT sha256 FROM media_refs WHERE sha256 IS NOT NULL)"
            )
        self.checkpoint()
        with self.connect() as conn:
            conn.execute("VACUUM")

    def source_path(self, relative: str) -> Path:
        return (self.workspace / relative).resolve()
