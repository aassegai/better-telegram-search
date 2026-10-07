"""Verified per-file GPU dependencies, retained independently of app updates."""

import gzip
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from filelock import FileLock

from telegram_search.inference.gpu_manifest import runtime_library, validate  # noqa: F401
from telegram_search.shared.errors import UserError
from telegram_search.updates import network


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_manifest(product, platform=None):
    path = product / "_internal/gpu-runtime.json"
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 100_000:
        raise UserError("Некорректный список GPU-библиотек.")
    try:
        return validate(json.loads(path.read_text(encoding="utf-8")), platform)
    except (ValueError, TypeError, KeyError) as exc:
        raise UserError("Некорректный список GPU-библиотек.") from exc


def matches(path, item):
    return (
        path.is_file()
        and not path.is_symlink()
        and path.stat().st_size == item["bytes"]
        and digest(path) == item["sha256"]
    )


class RuntimeCache:
    def __init__(self, workspace):
        workspace = workspace.resolve()
        self.root = workspace / "cache/gpu-runtime/objects"
        if not self.root.resolve().is_relative_to(workspace):
            raise UserError("Кэш GPU должен находиться внутри workspace.")
        for folder in (workspace / "cache", self.root.parent, self.root):
            if folder.is_symlink():
                raise UserError("Кэш GPU не может быть символической ссылкой.")
            folder.mkdir(parents=True, exist_ok=True)

    def _temporary(self):
        fd, name = tempfile.mkstemp(prefix=".gpu-", dir=self.root)
        os.close(fd)
        return Path(name)

    def _object(self, item, manifest, sources, local_assets, progress, stopped):
        target = self.root / item["sha256"]
        lock = self.root / (item["sha256"] + ".lock")
        if target.is_symlink() or lock.is_symlink():
            raise UserError("Недопустимый файл в кэше GPU.")
        with FileLock(lock, timeout=60):
            if matches(target, item):
                return target
            temporary = self._temporary()
            packed = self._temporary()
            try:
                for source in sources:
                    if matches(source, item):
                        temporary.unlink()
                        try:
                            os.link(source, temporary)
                        except OSError:
                            with source.open("rb") as incoming, temporary.open("wb") as copied:
                                shutil.copyfileobj(incoming, copied, length=1024**2)
                                copied.flush()
                                os.fsync(copied.fileno())
                        if not matches(temporary, item):
                            raise UserError("Кэш GPU изменился во время установки.")
                        os.replace(temporary, target)
                        return target
                if local_assets is not None:
                    incoming = (local_assets / item["asset"]).open("rb")
                else:
                    url = (
                        f"https://github.com/{network.REPOSITORY}/releases/download/"
                        f"v{manifest['version']}/{item['asset']}"
                    )
                    incoming = network.open_url(url)
                count, checksum = 0, hashlib.sha256()
                with incoming, packed.open("wb") as output:
                    while data := incoming.read(1024**2):
                        if stopped():
                            raise UserError("Обновление остановлено.")
                        count += len(data)
                        if count > item["compressed_bytes"]:
                            raise UserError("Размер GPU-библиотеки не совпадает с релизом.")
                        output.write(data)
                        checksum.update(data)
                        progress(len(data))
                if (
                    count != item["compressed_bytes"]
                    or checksum.hexdigest() != item["compressed_sha256"]
                ):
                    raise UserError("Контрольная сумма GPU-библиотеки не совпадает с релизом.")
                count = 0
                with gzip.open(packed, "rb") as source, temporary.open("wb") as output:
                    while data := source.read(min(1024**2, item["bytes"] - count + 1)):
                        if stopped():
                            raise UserError("Обновление остановлено.")
                        count += len(data)
                        if count > item["bytes"]:
                            raise UserError("Распакованная GPU-библиотека слишком велика.")
                        output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                if not matches(temporary, item):
                    raise UserError("Контрольная сумма GPU-библиотеки не совпадает с релизом.")
                os.replace(temporary, target)
                return target
            finally:
                temporary.unlink(missing_ok=True)
                packed.unlink(missing_ok=True)

    def hydrate(
        self,
        product,
        *,
        seed=None,
        local_assets=None,
        progress=lambda n: None,
        stopped=lambda: False,
    ):
        manifest = read_manifest(product)
        if manifest is None:
            return
        product = product.resolve()
        for item in manifest["files"]:
            path = product / item["path"]
            if not path.resolve().is_relative_to(product):
                raise UserError("GPU-библиотека выходит за пределы приложения.")
            for parent in path.parents:
                if parent == product:
                    break
                if parent.is_symlink():
                    raise UserError("Недопустимая папка GPU-библиотеки.")
            sources = [path]
            # A PyInstaller alias may seed the cache only through an already
            # containment-checked resolved regular file inside this product.
            if path.is_symlink():
                sources.append(path.resolve())
            if seed:
                candidate = seed / item["path"]
                if candidate.resolve().is_relative_to(seed.resolve()):
                    sources.append(candidate.resolve())
            cached = self._object(item, manifest, sources, local_assets, progress, stopped)
            if matches(path, item):
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                path.unlink()
            temporary = path.with_name(path.name + ".gpu-tmp")
            if temporary.exists() or temporary.is_symlink():
                raise UserError("В папке приложения уже есть временная GPU-библиотека.")
            try:
                try:
                    os.link(cached, temporary)
                except OSError:
                    shutil.copyfile(cached, temporary)
                if not matches(temporary, item):
                    raise UserError("Кэш GPU изменился во время установки.")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
