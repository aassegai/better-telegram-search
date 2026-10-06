import json
from pathlib import Path

import pytest

from telegram_search.ingestion.importer import ImportService
from telegram_search.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "workspace")
    database.initialize()
    return database


@pytest.fixture
def importer(db):
    service = ImportService(db, batch_size=2)
    yield service
    service.shutdown()


def message(mid=1, text="Синтетический тест", date=1750000000, author="user1", **extra):
    return {
        "id": mid,
        "type": "message",
        "date_unixtime": str(date),
        "from": "Тестовый автор",
        "from_id": author,
        "text": text,
        **extra,
    }


def export(root: Path, messages: list, chat_id=100, name="Синтетический диалог") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "result.json"
    path.write_text(
        json.dumps(
            {"id": chat_id, "name": name, "type": "personal_chat", "messages": messages},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def load(importer, path, **kwargs):
    result = importer.run(importer.prepare(str(path), **kwargs))
    assert result["state"] == "completed", result["error"]
    return result
