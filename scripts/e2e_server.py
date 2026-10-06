"""Start an isolated browser-test server using public synthetic fixtures only."""

import tempfile
from pathlib import Path

import uvicorn

from telegram_search.backend.api import create_app
from telegram_search.ingestion.importer import ImportService
from telegram_search.storage.database import Database

repo = Path(__file__).resolve().parents[1]
(repo / "workspace").mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix="e2e-", dir=repo / "workspace") as temporary:
    db = Database(Path(temporary))
    db.initialize()
    importer = ImportService(db)
    try:
        for filename in ("chat.json", "second-chat.json"):
            job = importer.prepare(str(repo / "tests" / "fixtures" / "synthetic" / filename))
            result = importer.run(job)
            if result["state"] != "completed":
                raise RuntimeError("Synthetic setup failed")
    finally:
        importer.shutdown()
    uvicorn.run(
        create_app(Path(temporary)),
        host="127.0.0.1",
        port=8766,
        access_log=False,
        log_level="warning",
    )
