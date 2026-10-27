"""Stream export headers, message count and fingerprint in one existing pass."""

import hashlib
import os
from contextlib import contextmanager
from contextvars import ContextVar

import ijson

from telegram_search.shared.errors import UserError

_media_progress = ContextVar("import_media_progress", default=None)


@contextmanager
def checking_media(progress):
    token = _media_progress.set(progress)
    try:
        yield
    finally:
        _media_progress.reset(token)
        progress.activity(None)


def file_hash(path, progress=None):
    digest = hashlib.sha256()
    total = path.stat().st_size
    media = _media_progress.get() if progress is None else None
    if progress:
        progress.phase("verify_json", total=total, unit="bytes")
    with path.open("rb") as stream:
        completed = 0
        while block := stream.read(1024 * 1024):
            digest.update(block)
            completed += len(block)
            if progress:
                progress.update(completed)
            if media:
                media.activity("checking_media", completed, total)
    if progress:
        progress.update(completed, force=True)
    return digest.hexdigest()


class ExportReader:
    def __init__(self, stream, progress, total):
        self.stream, self.progress, self.total = stream, progress, total
        self.digest = hashlib.sha256()
        self.completed = 0

    def read(self, size=-1):
        data = self.stream.read(size)
        self.digest.update(data)
        self.completed += len(data)
        if self.progress and data:
            self.progress.update(self.completed)
        return data

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)


def _signature(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def scan_export(path, progress=None):
    metadata, has_messages, total_messages = {}, False, 0
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if progress:
                progress.phase("metadata", total=before.st_size, unit="bytes")
            source = ExportReader(stream, progress, before.st_size)
            for prefix, event, value in ijson.parse(source, use_float=True):
                if prefix in {"id", "name", "type"} and event in {"string", "number", "null"}:
                    metadata[prefix] = value
                if prefix == "messages" and event == "start_array":
                    has_messages = True
                if prefix == "messages.item" and event in {
                    "start_map",
                    "start_array",
                    "string",
                    "number",
                    "boolean",
                    "null",
                }:
                    total_messages += 1
            if _signature(os.fstat(stream.fileno())) != _signature(before):
                raise UserError("JSON изменился во время чтения. Повторите проверку.")
            # Windows stat(path) and fstat(fd) can report different file/device
            # identifiers. Compare two open handles through the same API instead.
            # This probe reads no content: the JSON still has one streaming pass.
            with path.open("rb") as current:
                if _signature(os.fstat(current.fileno())) != _signature(before):
                    raise UserError("JSON изменился во время чтения. Повторите проверку.")
        if progress:
            progress.update(source.completed, force=True)
    except UserError:
        raise
    except (OSError, ValueError, ijson.JSONError) as exc:
        raise UserError("Не удалось прочитать JSON экспорта Telegram.") from exc
    if not has_messages:
        raise UserError("Нужен JSON одного диалога из Telegram Desktop с массивом messages.")
    return metadata, source.digest.hexdigest(), total_messages
