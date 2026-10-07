import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from telegram_search.inference.gpu_cache import RuntimeCache, digest
from telegram_search.inference.gpu_manifest import validate
from telegram_search.shared.errors import UserError

spec = importlib.util.spec_from_file_location(
    "gpu_payload", Path(__file__).parents[1] / "bundle/gpu_payload.py"
)
payload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(payload)
pack, split_runtime = payload.pack, payload.split_runtime


def product(root, version, contents):
    directory = root / version / "BetterTelegramSearch"
    (directory / "_internal").mkdir(parents=True)
    (directory / "telegram-search.exe").write_bytes(b"synthetic app " + version.encode())
    for name, data in contents.items():
        (directory / "_internal" / name).write_bytes(data)
    output = root / version / "assets"
    output.mkdir()
    return directory, output, split_runtime(directory, output, version, "win32")


def core(source, output, runtime, destination):
    archive = output / "synthetic-core.zip"
    pack(source, archive, exclude=[item["path"] for item in runtime["files"]])
    shutil.unpack_archive(archive, destination)
    return destination / source.name


def test_cache_seeds_full_install_then_downloads_only_changed_file(tmp_path, monkeypatch):
    first, assets, manifest = product(
        tmp_path,
        "0.3.1",
        {
            "cudart64_12.dll": b"same GPU library",
            "cudnn64_9.dll": b"version A",
        },
    )
    cache = RuntimeCache(tmp_path / "workspace")
    first_core = core(first, assets, manifest, tmp_path / "first-core")
    downloads = []

    def download(url):
        downloads.append(url.rsplit("/", 1)[-1])
        return (new_assets / downloads[-1]).open("rb")

    monkeypatch.setattr("telegram_search.inference.gpu_cache.network.open_url", download)
    cache.hydrate(first_core, seed=first)
    assert not downloads  # Also executed natively on Windows in build_app.py.
    second, new_assets, newer = product(
        tmp_path,
        "0.3.2",
        {
            "cudart64_12.dll": b"same GPU library",
            "cudnn64_9.dll": b"version B",
        },
    )
    second_core = core(second, new_assets, newer, tmp_path / "second-core")
    cache.hydrate(second_core)
    changed = next(item for item in newer["files"] if "cudnn" in item["path"])
    assert downloads == [changed["asset"]]
    assert (first_core / "_internal/cudnn64_9.dll").read_bytes() == b"version A"
    assert (second_core / "_internal/cudnn64_9.dll").read_bytes() == b"version B"
    downloads.clear()
    cache.hydrate(second_core)
    assert not downloads


def test_corrupted_download_is_never_published_and_temporary_files_are_removed(tmp_path):
    source, assets, manifest = product(tmp_path, "0.3.1", {"cudart64_12.dll": b"library"})
    target = core(source, assets, manifest, tmp_path / "core")
    (assets / manifest["files"][0]["asset"]).write_bytes(b"damaged gzip")
    cache = RuntimeCache(tmp_path / "workspace")
    with pytest.raises(UserError):
        cache.hydrate(target, local_assets=assets)
    assert not (target / manifest["files"][0]["path"]).exists()
    assert not list(cache.root.glob(".gpu-*"))
    assert not (cache.root / manifest["files"][0]["sha256"]).exists()


@pytest.mark.parametrize(
    "path",
    [
        "../cudart.dll",
        "_internal/../cudart.dll",
        "_internal/app.py",
        "_internal/CUDART.dll/../x",
        12,
    ],
)
def test_manifest_cannot_replace_code_or_escape_product(tmp_path, path):
    _, _, manifest = product(tmp_path, "0.3.1", {"cudart.dll": b"library"})
    manifest["files"][0]["path"] = path
    with pytest.raises(UserError):
        validate(manifest)


def test_symlinked_cache_or_payload_parent_is_rejected(tmp_path):
    source, assets, manifest = product(tmp_path, "0.3.1", {"cudart.dll": b"library"})
    target = core(source, assets, manifest, tmp_path / "core")
    external = tmp_path / "external"
    external.mkdir()
    nested = target / "_internal/redirect"
    try:
        nested.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks require Windows privilege")
    manifest["files"][0]["path"] = "_internal/redirect/cudart.dll"
    (target / "_internal/gpu-runtime.json").write_text(json.dumps(manifest))
    with pytest.raises(UserError):
        RuntimeCache(tmp_path / "workspace").hydrate(target, local_assets=assets)
    assert not list(external.iterdir())


def test_linux_aliases_share_one_verified_cached_object(tmp_path):
    source = tmp_path / "BetterTelegramSearch"
    (source / "_internal/nvidia").mkdir(parents=True)
    library = source / "_internal/nvidia/libcudart.so.12"
    library.write_bytes(b"Linux synthetic library")
    (source / "_internal/libcudart.so.12").symlink_to("nvidia/libcudart.so.12")
    assets = tmp_path / "assets"
    assets.mkdir()
    manifest = split_runtime(source, assets, "0.3.1", "linux")
    assert len(manifest["files"]) == 2 and len(list(assets.iterdir())) == 1
    cache = RuntimeCache(tmp_path / "workspace")
    cache.hydrate(source, local_assets=assets)
    assert all(digest(source / item["path"]) == item["sha256"] for item in manifest["files"])
