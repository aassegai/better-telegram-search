import sqlite3
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from telegram_search.config.settings import Settings
from telegram_search.security.privacy import repository_warning
from telegram_search.shared.errors import UserError

SCHEMA_VERSION = 7


def execute_sql(conn, sql: str) -> None:
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("Incomplete SQL migration")


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
            if not (self.workspace / folder).resolve().is_relative_to(self.workspace):
                raise UserError("Папки данных должны находиться внутри workspace.")
            (self.workspace / folder).mkdir(parents=True, exist_ok=True)
        if not self.path.resolve().is_relative_to(self.workspace):
            raise UserError("База данных должна находиться внутри workspace.")
        self.settings = Settings.load(self.workspace)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)"
            )
            version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            if version is not None and not 1 <= version <= SCHEMA_VERSION:
                raise UserError("Версия базы не поддерживается этим приложением.")
        if version != SCHEMA_VERSION:
            try:
                with FileLock(self.workspace / ".writer.lock", timeout=0), self.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[
                        0
                    ]
                    if version is None:
                        execute_sql(conn, Path(__file__).with_name("schema.sql").read_text())
                        version = 1
                    for migration in sorted(Path(__file__).with_name("migrations").glob("*.sql")):
                        number = int(migration.name.split("_")[0])
                        if version < number <= SCHEMA_VERSION:
                            execute_sql(conn, migration.read_text())
            except Timeout as exc:
                raise UserError("Остановите приложение перед обновлением схемы workspace.") from exc

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
            conn.execute("INSERT INTO chunk_fts(chunk_fts) VALUES ('rebuild')")
            conn.execute("INSERT INTO ocr_fts(ocr_fts) VALUES ('rebuild')")

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
