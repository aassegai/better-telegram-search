import re

from telegram_search.shared.errors import UserError
from telegram_search.updates.network import REPOSITORY

MAX_ARCHIVE_BYTES = 2 * 1024**3 - 1
CHECKS = {
    "sqlite_fts5",
    "lancedb",
    "onnx_cpu",
    "tokenizers",
    "safetensors",
    "ocr_rus_eng",
    "frontend_http",
    "csrf",
    "torch_absent",
    "unicode_paths",
    "update_installer",
    "device_selection",
}


def required_checks(variant):
    return CHECKS | ({"cuda_libraries"} if variant == "gpu" else set())


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"v?\d{1,6}\.\d{1,6}\.\d{1,6}", value):
        raise UserError("Некорректная версия обновления.")
    return tuple(int(part) for part in value.removeprefix("v").split("."))


def select_release(release, current_version, platform, arch, variant="cpu", current_variant="cpu"):
    tag = release.get("tag_name")
    new = version(tag)
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise UserError("Обновление должно быть опубликованным стабильным релизом.")
    if new < version(current_version) or (
        new == version(current_version) and variant == current_variant
    ):
        return None
    if variant not in {"cpu", "gpu"} or (
        variant == "gpu" and (platform, arch) not in {("win32", "x86_64"), ("linux", "x86_64")}
    ):
        raise UserError("Для этой платформы нет отдельной GPU-сборки.")
    extension = ("tar.xz" if variant == "gpu" else "tar.gz") if platform == "linux" else "zip"
    suffix = "-gpu" if variant == "gpu" else ""
    prefix = f"better-telegram-search-{tag.removeprefix('v')}-{platform}-{arch}{suffix}"
    assets = release.get("assets", [])
    if not isinstance(assets, list) or len(assets) > 100:
        raise UserError("Некорректный список файлов обновления.")
    selected = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise UserError("Некорректный список файлов обновления.")
        name = asset.get("name")
        if name not in {prefix + "." + extension, prefix + ".json"}:
            continue
        if name in selected or asset.get("state") != "uploaded":
            raise UserError("Файлы обновления ещё не готовы.")
        expected_url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
        if (
            asset.get("browser_download_url") != expected_url
            or type(asset.get("size")) is not int
            or type(asset.get("id")) is not int
        ):
            raise UserError("Некорректные файлы обновления.")
        selected[name] = asset
    archive = selected.get(prefix + "." + extension)
    report = selected.get(prefix + ".json")
    if not archive or not report:
        raise UserError("Для этой платформы нет готовой сборки в релизе.")
    if not 0 < archive["size"] <= MAX_ARCHIVE_BYTES or not 0 < report["size"] <= 100_000:
        raise UserError("Недопустимый размер файлов обновления.")
    digest = archive.get("digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise UserError("В GitHub отсутствует контрольная сумма сборки.")
    return {
        "version": tag.removeprefix("v"),
        "tag": tag,
        "variant": variant,
        "platform": platform,
        "arch": arch,
        "archive": archive,
        "report": report,
        "notes": str(release.get("body") or "")[:20_000],
    }


def validate_manifest(manifest, selected):
    asset = selected["archive"]
    checks = manifest.get("checks")
    if (
        manifest.get("artifact") != asset["name"]
        or manifest.get("platform") != selected["platform"]
        or manifest.get("arch") != selected["arch"]
        or manifest.get("variant", "cpu") != selected["variant"]
        or manifest.get("source_dirty") is not False
        or not isinstance(manifest.get("commit"), str)
        or not re.fullmatch(r"[a-f0-9]{40}", manifest["commit"])
        or manifest.get("bytes") != asset["size"]
        or asset["digest"] != "sha256:" + str(manifest.get("sha256"))
        or not isinstance(checks, dict)
        or not required_checks(selected["variant"]) <= checks.keys()
        or any(value is not True for value in checks.values())
    ):
        raise UserError("Сборка не соответствует проверенному отчёту релиза.")
    return manifest
