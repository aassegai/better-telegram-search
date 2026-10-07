import hashlib
import importlib.metadata
import json
import math
import os
import shutil
import subprocess
import tempfile
import urllib.request
from importlib.resources import files
from pathlib import Path

from filelock import FileLock

from telegram_search.config.runtime import bundled_directory, ocr_command
from telegram_search.inference.bundles import checksum
from telegram_search.inference.ocr_process import OcrProcess
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize


class OcrEngine:
    """CPU Tesseract in a reusable child; each image has an independent timeout."""

    def __init__(self, workspace: Path, *, threads=4, max_edge=2400, timeout=45):
        self.root = workspace.resolve() / "models" / "ocr"
        if not self.root.resolve().is_relative_to(workspace.resolve()):
            raise UserError("Словари OCR должны находиться внутри workspace.")
        self.manifest = json.loads(
            files("telegram_search.config").joinpath("ocr_model.json").read_text()
        )
        try:
            runtime = importlib.metadata.version("tesserocr")
            native = (
                subprocess.run(
                    ocr_command("--runtime"),
                    capture_output=True,
                    timeout=10,
                    check=True,
                )
                .stdout.decode()
                .strip()
            )
        except (importlib.metadata.PackageNotFoundError, subprocess.SubprocessError) as exc:
            raise UserError(
                "Установите CPU OCR: uv sync --locked --extra semantic --extra ocr."
            ) from exc
        self.threads, self.max_edge, self.timeout = threads, max_edge, timeout
        self.worker = OcrProcess(
            ocr_command(str(self.root), str(self.max_edge), "--server"),
            threads=threads,
            timeout=timeout,
        )
        self.version = hashlib.sha256(
            serialize(
                {
                    "dictionaries": self.manifest,
                    "runtime": runtime,
                    "native_runtime": native,
                    "pillow": importlib.metadata.version("pillow"),
                    "preprocessing": "exif-rgb-autocontrast-v1",
                    "max_edge": max_edge,
                    "languages": "rus+eng",
                    "segmentation": "auto",
                }
            ).encode()
        ).hexdigest()

    def verify(self):
        for item in self.manifest["files"]:
            path = self.root / item["name"]
            if (
                path.is_symlink()
                or not path.resolve().is_relative_to(self.root.resolve())
                or not path.is_file()
                or path.stat().st_size != item["bytes"]
                or checksum(path) != item["sha256"]
            ):
                raise UserError("Словари OCR отсутствуют или повреждены. Подготовьте OCR.")

    def prepare(self, *, offline=False):
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(self.root.parent / ".ocr-download.lock", timeout=0):
            for item in self.manifest["files"]:
                path = self.root / item["name"]
                if path.is_file() and not path.is_symlink() and checksum(path) == item["sha256"]:
                    continue
                bundled = bundled_directory("tessdata")
                source = bundled / item["name"] if bundled else None
                if source and (
                    not source.is_file()
                    or source.stat().st_size != item["bytes"]
                    or checksum(source) != item["sha256"]
                ):
                    raise UserError("Словари OCR в сборке повреждены.")
                if offline and source is None:
                    raise UserError("Закреплённые словари OCR недоступны офлайн.")
                with tempfile.TemporaryDirectory(dir=self.root) as staging:
                    target = Path(staging) / item["name"]
                    if source:
                        shutil.copyfile(source, target)
                    else:
                        self._download(item, target)
                    if checksum(target) != item["sha256"]:
                        raise UserError("Контрольная сумма словаря OCR не совпадает.")
                    os.replace(target, path)
            self.verify()

    @staticmethod
    def _download(item, target):
        with (
            urllib.request.urlopen(item["url"], timeout=60) as response,
            target.open("wb") as out,
        ):
            remaining = item["bytes"]
            while remaining > 0:
                block = response.read(min(1024 * 1024, remaining))
                if not block:
                    break
                out.write(block)
                remaining -= len(block)
            if remaining or response.read(1):
                raise UserError("Размер словаря OCR не совпадает с manifest.")

    def recognize(self, data: bytes) -> dict:
        try:
            value = json.loads(self.worker.recognize(data))
            if (
                not isinstance(value["text"], str)
                or len(value["text"]) > 65536
                or type(value["confidence"]) not in {int, float}
                or not math.isfinite(value["confidence"])
                or not 0 <= value["confidence"] <= 100
            ):
                raise ValueError("OCR output")
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.worker.unload()
            raise UserError("OCR не смог обработать изображение за заданное время.") from exc

    def unload(self):
        self.worker.unload()
