import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from filelock import FileLock, Timeout

from telegram_search.config.model_registry import ModelSpec
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


class BundleStore:
    def __init__(self, workspace: Path):
        self.root = workspace.resolve() / "models"
        if not self.root.resolve().is_relative_to(workspace.resolve()):
            raise UserError("Папка моделей должна находиться внутри workspace.")

    def path(self, spec: ModelSpec) -> Path:
        path = self.root / "bundles" / spec.identity
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise UserError("Папка набора модели должна находиться внутри workspace.")
        return path

    def verify(self, spec: ModelSpec, path: Path | None = None) -> Path:
        path = path or self.path(spec)
        try:
            if (path / "manifest.json").is_symlink():
                raise ValueError("manifest boundary")
            if json.loads((path / "manifest.json").read_text()) != spec.manifest:
                raise ValueError("manifest")
            for item in spec.manifest["files"]:
                source = path / item["name"]
                if source.is_symlink() or not source.resolve().is_relative_to(path.resolve()):
                    raise ValueError("boundary")
                if source.stat().st_size != item["bytes"] or checksum(source) != item["sha256"]:
                    raise ValueError("checksum")
        except (OSError, ValueError) as exc:
            raise UserError(
                "Набор модели отсутствует или повреждён. Подготовьте модель заново."
            ) from exc
        return path

    def _release_file(self, item, *, offline, report):
        """Content-addressed cache for our verified, public export artifacts."""
        from telegram_search.updates.network import REPOSITORY, open_url

        url = item["url"]
        prefix = f"https://github.com/{REPOSITORY}/releases/download/"
        if not url.startswith(prefix) or "/../" in url:
            raise UserError("Недопустимый источник ONNX-модели.")
        cache = self.root / "downloads"
        if cache.is_symlink() or not cache.resolve().is_relative_to(self.root.resolve()):
            raise UserError("Недопустимая папка кэша модели.")
        cache.mkdir(exist_ok=True)
        target = cache / item["sha256"]
        if target.is_symlink():
            raise UserError("Файл модели не должен быть символьной ссылкой.")
        if target.is_file():
            if target.stat().st_size == item["bytes"] and checksum(target) == item["sha256"]:
                return target
            if offline:
                raise UserError("Локальный ONNX-кэш повреждён. Повторите загрузку.")
            target.unlink()
        if offline:
            raise UserError("ONNX-модель отсутствует в локальном кэше.")
        fd, temporary = tempfile.mkstemp(dir=cache, prefix=".download-")
        try:
            with os.fdopen(fd, "wb") as out, open_url(url) as response:
                count = 0
                while block := response.read(min(1024**2, item["bytes"] - count + 1)):
                    count += len(block)
                    if count > item["bytes"]:
                        raise UserError("ONNX-файл превышает закреплённый размер.")
                    out.write(block)
                    report(count)
                out.flush()
                os.fsync(out.fileno())
            downloaded = Path(temporary)
            if count != item["bytes"] or checksum(downloaded) != item["sha256"]:
                raise UserError("Проверка целостности ONNX-модели не прошла.")
            os.replace(downloaded, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return target

    def prepare(
        self,
        spec: ModelSpec,
        *,
        offline: bool = False,
        progress: Callable[[dict], None] | None = None,
        repair: bool = False,
        local_bundle: str | Path | None = None,
    ) -> Path:
        try:
            from huggingface_hub import hf_hub_download
        except ImportError as exc:
            raise UserError("Установите CPU runtime: uv sync --locked --extra semantic.") from exc
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / "hub").resolve().is_relative_to(self.root.resolve()):
            raise UserError("Кэш модели должен находиться внутри workspace.")
        destination = self.path(spec)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            lease = FileLock(self.root / ".download.lock", timeout=0).acquire()
        except Timeout as exc:
            raise UserError("Подготовка модели уже выполняется в этом workspace.") from exc
        with lease:
            if destination.exists():
                try:
                    return self.verify(spec)
                except UserError:
                    if not repair:
                        raise
                    if destination.is_symlink():
                        raise UserError("Папка модели не должна быть символьной ссылкой.") from None
                    shutil.rmtree(destination)
            local = (
                self.verify(spec, Path(local_bundle).expanduser().resolve())
                if local_bundle
                else None
            )
            if (
                not offline
                and shutil.disk_usage(self.root).free < spec.download_bytes * 2 + 256 * 1024**2
            ):
                raise UserError("Для подготовки модели недостаточно свободного места.")
            with tempfile.TemporaryDirectory(
                prefix=".preparing-", dir=destination.parent
            ) as temporary:
                stage = Path(temporary) / "bundle"
                stage.mkdir()
                completed = 0
                for item in spec.manifest["files"]:
                    if progress:
                        progress(
                            {
                                "stage": "downloading",
                                "completed_bytes": completed,
                                "total_bytes": spec.download_bytes,
                                "file": item["name"],
                            }
                        )
                    from huggingface_hub.utils import tqdm

                    last_report = 0.0

                    def report_bytes(current, completed=completed, item=item):
                        nonlocal last_report
                        now = time.monotonic()
                        if progress and now - last_report > 0.2:
                            last_report = now
                            progress(
                                {
                                    "stage": "downloading",
                                    "completed_bytes": completed + min(int(current), item["bytes"]),
                                    "total_bytes": spec.download_bytes,
                                    "file": item["name"],
                                }
                            )

                    class DownloadProgress(tqdm):
                        def update(self, n=1):
                            result = super().update(n)
                            report_bytes(self.n)
                            return result

                    for attempt in range(3):
                        try:
                            source = (
                                local / item["name"]
                                if local
                                else self._release_file(item, offline=offline, report=report_bytes)
                                if "url" in item
                                else Path(
                                    hf_hub_download(
                                        item.get("repo_id", spec.model_id),
                                        item.get("filename", item["name"]),
                                        revision=item.get("revision", spec.revision),
                                        cache_dir=self.root / "hub",
                                        local_files_only=offline,
                                        token=False,
                                        tqdm_class=DownloadProgress,
                                        force_download=repair and not offline,
                                    )
                                )
                            )
                            if (
                                source.stat().st_size != item["bytes"]
                                or checksum(source) != item["sha256"]
                            ):
                                raise UserError(
                                    "Проверка целостности upstream-файла модели не прошла."
                                )
                            break
                        except Exception as exc:
                            if isinstance(exc, UserError):
                                raise
                            if offline or attempt == 2:
                                raise UserError(
                                    "Не удалось получить модель. Повторите загрузку "
                                    "или используйте заполненный локальный кэш."
                                ) from exc
                    target = stage / item["name"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if local:
                        shutil.copyfile(source, target)
                    else:
                        try:
                            os.link(source.resolve(), target)
                        except OSError:
                            shutil.copyfile(source, target)
                    completed += item["bytes"]
                (stage / "manifest.json").write_text(serialize(spec.manifest), encoding="utf-8")
                self.verify(spec, stage)
                os.replace(stage, destination)
            if progress:
                progress(
                    {
                        "stage": "downloaded",
                        "completed_bytes": completed,
                        "total_bytes": spec.download_bytes,
                    }
                )
        return destination
