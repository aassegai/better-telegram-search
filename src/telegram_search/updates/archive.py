"""Bounded extraction without executable links escaping the application directory."""

import os
import shutil
import stat
import tarfile
import zipfile
from pathlib import PurePosixPath

from telegram_search.shared.errors import UserError

MAX_UNPACKED = 12 * 1024**3
MAX_FILES = 30_000


def extract(archive, destination, *, platform, max_bytes=MAX_UNPACKED):
    root_name = "Better Telegram Search.app" if platform == "darwin" else "BetterTelegramSearch"
    destination.mkdir(parents=True, exist_ok=False)
    total = 0
    seen = set()
    links = []

    def target(name, size):
        nonlocal total
        parts = PurePosixPath(name).parts
        if (
            not parts
            or parts[0] not in {root_name, "__MACOSX"}
            or ".." in parts
            or "\\" in name
            or ":" in name
            or "\x00" in name
            or name.startswith("/")
            or size < 0
        ):
            raise UserError("Архив обновления содержит небезопасный путь.")
        key = "/".join(parts).casefold()
        if key in seen:
            raise UserError("Архив обновления содержит повторяющиеся пути.")
        seen.add(key)
        total += size
        if total > min(max_bytes, MAX_UNPACKED) or len(seen) > MAX_FILES:
            raise UserError("Архив обновления превышает лимит распаковки.")
        return destination.joinpath(*parts)

    def write(path, stream, size, mode):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise UserError("Архив обновления содержит конфликтующие пути.")
        with path.open("xb") as output:
            remaining = size
            while remaining:
                data = stream.read(min(1024**2, remaining))
                if not data:
                    raise UserError("Архив обновления оборван.")
                output.write(data)
                remaining -= len(data)
            if stream.read(1):
                raise UserError("Неверный размер файла в архиве обновления.")
        path.chmod(mode & 0o777 & ~0o022 or 0o644)

    try:
        if platform == "linux":
            with tarfile.open(archive, "r:*") as incoming:
                for member in incoming:
                    path = target(member.name, member.size)
                    if member.isdir():
                        path.mkdir(parents=True, exist_ok=True)
                    elif member.issym():
                        links.append((path, member.linkname))
                    elif member.isfile():
                        with incoming.extractfile(member) as stream:
                            write(path, stream, member.size, member.mode)
                    else:
                        raise UserError("Архив обновления содержит недопустимый тип файла.")
        else:
            with zipfile.ZipFile(archive) as incoming:
                for member in incoming.infolist():
                    path = target(member.filename, member.file_size)
                    mode = member.external_attr >> 16
                    if member.is_dir():
                        path.mkdir(parents=True, exist_ok=True)
                    elif stat.S_ISLNK(mode):
                        if member.file_size > 4096:
                            raise UserError("Небезопасная ссылка в архиве обновления.")
                        links.append((path, incoming.read(member).decode("utf-8")))
                    elif stat.S_IFMT(mode) in {0, stat.S_IFREG}:
                        with incoming.open(member) as stream:
                            write(path, stream, member.file_size, mode)
                    else:
                        raise UserError("Архив обновления содержит недопустимый тип файла.")
        product = destination / root_name
        if len(links) > 1024:
            raise UserError("Небезопасная ссылка в архиве обновления.")
        pending = links
        # Framework aliases may precede their targets. Create dependencies first,
        # while bounding graph depth and rejecting cycles, dangling links and escape.
        for _ in range(128):
            deferred = []
            for path, link in pending:
                relative = PurePosixPath(link)
                resolved = (path.parent / link).resolve()
                if (
                    relative.is_absolute()
                    or "\\" in link
                    or ":" in link
                    or "\x00" in link
                    or not resolved.is_relative_to(product)
                    or not path.parent.resolve().is_relative_to(product)
                    or path.exists()
                    or path.is_symlink()
                ):
                    raise UserError("Небезопасная ссылка в архиве обновления.")
                if not resolved.exists():
                    deferred.append((path, link))
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(link, path)
            if not deferred:
                break
            if len(deferred) == len(pending):
                raise UserError("Небезопасная ссылка в архиве обновления.")
            pending = deferred
        else:
            raise UserError("Небезопасная ссылка в архиве обновления.")
        return product
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        shutil.rmtree(destination, ignore_errors=True)
        raise UserError("Не удалось безопасно распаковать обновление.") from exc
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def executable(root, platform):
    if platform == "darwin":
        return root / "Contents/MacOS/telegram-search"
    return root / ("telegram-search.exe" if platform == "win32" else "telegram-search")
