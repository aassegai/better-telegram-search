"""Split native dependencies from the small update payload; full packages stay portable."""

import gzip
import json
import shutil
from pathlib import Path

from telegram_search.inference.gpu_cache import digest, runtime_library, validate


def split_runtime(product, output, version, platform):
    product, output = Path(product).resolve(), Path(output)
    files = []
    for path in sorted((product / "_internal").rglob("*")):
        if not path.is_file() or not runtime_library(path.name):
            continue
        if not path.resolve().is_relative_to(product):
            raise ValueError("GPU library resolves outside the build")
        checksum = digest(path)
        asset = f"gpu-runtime-{platform}-x86_64-{checksum}.gz"
        packed = output / asset
        if not packed.exists():
            with path.open("rb") as source, packed.open("wb") as target:
                with gzip.GzipFile(
                    filename="", fileobj=target, mode="wb", mtime=0, compresslevel=1
                ) as encoded:
                    shutil.copyfileobj(source, encoded, length=1024**2)
        files.append(
            {
                "path": path.relative_to(product).as_posix(),
                "sha256": checksum,
                "bytes": path.stat().st_size,
                "asset": asset,
                "compressed_sha256": digest(packed),
                "compressed_bytes": packed.stat().st_size,
            }
        )
    if len({item["asset"] for item in files}) > 35:
        raise ValueError("GPU payload exceeds the compatible release asset budget")
    manifest = validate({"schema": 1, "version": version, "platform": platform, "files": files})
    (product / "_internal/gpu-runtime.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


def pack(product, archive, *, exclude=()):
    import tarfile
    import zipfile

    product, archive = Path(product), Path(archive)
    excluded = {product.name + "/" + name for name in exclude}
    if archive.suffix == ".zip":
        with zipfile.ZipFile(
            archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as out:
            for path in sorted(product.rglob("*")):
                name = path.relative_to(product.parent).as_posix()
                if name not in excluded:
                    out.write(path, name)
    else:
        compression = {"preset": 1} if archive.name.endswith(".xz") else {"compresslevel": 3}
        with tarfile.open(
            archive, "w:xz" if archive.name.endswith(".xz") else "w:gz", **compression
        ) as out:
            out.add(
                product,
                arcname=product.name,
                filter=lambda entry: None if entry.name in excluded else entry,
            )
