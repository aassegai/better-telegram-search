"""Strict, dependency-free GPU payload manifest validation."""

import re
from pathlib import PurePosixPath

from telegram_search.shared.errors import UserError

MAX_FILE = 2 * 1024**3 - 1
PREFIXES = (
    "cudart",
    "cublas",
    "cudnn",
    "cufft",
    "curand",
    "nvrtc",
    "nvjitlink",
    "onnxruntime_providers_cuda",
)


def runtime_library(name):
    name = name.lower().removeprefix("lib")
    return name.startswith(PREFIXES) and bool(re.search(r"(?:\.dll|\.so(?:\.\d+)*)$", name))


def validate(manifest, platform=None):
    """Paths can only name GPU libraries; never overwrite app code or configuration."""
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != 1
        or manifest.get("platform") not in {"linux", "win32"}
        or (platform is not None and manifest["platform"] != platform)
        or not re.fullmatch(r"\d+\.\d+\.\d+", str(manifest.get("version", "")))
        or not isinstance(manifest.get("files"), list)
        or not 1 <= len(manifest["files"]) <= 128
    ):
        raise UserError("Некорректный список GPU-библиотек.")
    seen, total = set(), 0
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise UserError("Некорректный список GPU-библиотек.")
        name = item.get("path", "")
        if not isinstance(name, str):
            raise UserError("Недопустимый путь GPU-библиотеки.")
        path = PurePosixPath(name)
        if (
            not isinstance(name, str)
            or len(name) > 512
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", name)
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or path.parts[0] != "_internal"
            or path.as_posix().casefold() in seen
            or not runtime_library(path.name)
        ):
            raise UserError("Недопустимый путь GPU-библиотеки.")
        for field in ("sha256", "compressed_sha256"):
            if not re.fullmatch(r"[a-f0-9]{64}", str(item.get(field, ""))):
                raise UserError("Некорректная контрольная сумма GPU-библиотеки.")
        for field in ("bytes", "compressed_bytes"):
            if type(item.get(field)) is not int or not 0 < item[field] <= MAX_FILE:
                raise UserError("Недопустимый размер GPU-библиотеки.")
        expected = f"gpu-runtime-{manifest['platform']}-x86_64-{item['sha256']}.gz"
        if item.get("asset") != expected:
            raise UserError("Некорректный файл GPU-библиотеки.")
        seen.add(path.as_posix().casefold())
        total += item["bytes"]
    if total > 12 * 1024**3:
        raise UserError("Список GPU-библиотек слишком велик.")
    return manifest
