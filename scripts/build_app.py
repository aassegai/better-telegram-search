"""Build, unpack and verify a portable CPU or NVIDIA GPU application on the target OS.

Inputs are explicitly selected public code/resources. No workspace or corpus is collected.
Run with the dedicated uv bundle environment; Python 3.12 is the release ABI.
"""

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=REPO / "artifacts")
    parser.add_argument("--expected-arch", choices=["x86_64", "arm64"])
    parser.add_argument("--variant", choices=["cpu", "gpu"], default="cpu")
    args = parser.parse_args()
    arch = {"AMD64": "x86_64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    if args.expected_arch and args.expected_arch != arch:
        raise RuntimeError("Runner architecture does not match the artifact name")
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Release builds require Python 3.12")
    installed = {item.metadata["Name"].lower() for item in importlib.metadata.distributions()}
    if installed & {"torch", "torchvision", "torchaudio", "transformers", "sentence-transformers"}:
        raise RuntimeError("Build environment contains excluded ML training dependencies")
    if args.variant == "gpu" and (sys.platform, arch) not in {
        ("linux", "x86_64"),
        ("win32", "x86_64"),
    }:
        raise RuntimeError("NVIDIA GPU packages require Windows/Linux x86_64")
    gpu_installed = "onnxruntime-gpu" in installed
    if gpu_installed != (args.variant == "gpu"):
        raise RuntimeError("Runtime does not match the requested build variant")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    build_root = REPO / "build" / f"{sys.platform}-{arch}-{args.variant}"
    assets = build_root / "assets"
    (assets / "licenses").mkdir(parents=True, exist_ok=True)
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    (assets / "build.json").write_text(
        json.dumps(
            {"version": version, "variant": args.variant, "platform": sys.platform, "arch": arch}
        ),
        encoding="utf-8",
    )
    from telegram_search.inference.ocr import OcrEngine

    engine = OcrEngine(assets)
    engine.prepare()
    tessdata = assets / "tessdata"
    tessdata.mkdir(exist_ok=True)
    for item in engine.manifest["files"]:
        shutil.copyfile(engine.root / item["name"], tessdata / item["name"])
    notices = json.loads((REPO / "bundle/native-notices.json").read_text())
    for notice in notices:
        target = assets / "licenses" / (notice["name"] + ".txt")
        if not target.is_file() or sha256(target) != notice["sha256"]:
            with urllib.request.urlopen(notice["url"], timeout=60) as response:
                data = response.read(notice["bytes"] + 1)
            if len(data) != notice["bytes"] or hashlib.sha256(data).hexdigest() != notice["sha256"]:
                raise RuntimeError("Native license checksum mismatch")
            if "member" in notice:
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                    data = archive.extractfile(notice["member"]).read()
            target.write_bytes(data)
        if "member" not in notice and sha256(target) != notice["sha256"]:
            raise RuntimeError("Native license checksum mismatch")
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if not python_license.is_file():
        python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("Actual CPython license is missing from the build interpreter")
    shutil.copyfile(python_license, assets / "licenses/CPython.txt")
    inventory = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata["Name"]
        inventory.append({"name": name, "version": distribution.version})
        for file in distribution.files or []:
            if any(word in file.name.lower() for word in ("license", "notice", "copying")):
                source = distribution.locate_file(file)
                if source.is_file():
                    identifier = hashlib.sha256(str(file).encode()).hexdigest()[:12]
                    target = assets / "licenses" / name / f"{identifier}-{file.name}"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
    (assets / "licenses" / "dependencies.json").write_text(
        json.dumps(sorted(inventory, key=lambda item: item["name"]), indent=2), encoding="utf-8"
    )
    run("npm.cmd" if sys.platform == "win32" else "npm", "run", "build", cwd=REPO / "frontend")
    environment = {**os.environ, "BTS_BUNDLE_ASSETS": str(assets)}
    run(
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(build_root / "dist"),
        "--workpath",
        str(build_root / "work"),
        str(REPO / "bundle" / "application.spec"),
        cwd=REPO,
        env=environment,
    )
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    name = f"better-telegram-search-{version}-{sys.platform}-{arch}" + (
        "-gpu" if args.variant == "gpu" else ""
    )
    if sys.platform == "darwin":
        product = build_root / "dist" / "Better Telegram Search.app"
        archive = output / f"{name}.zip"
        run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(product), str(archive))
    else:
        product = build_root / "dist" / "BetterTelegramSearch"
        shutil.copyfile(REPO / "docs" / "portable-builds.md", product / "README.md")
        shutil.copyfile(REPO / "docs" / "portable-builds.en.md", product / "README.en.md")
        extension = (
            "zip" if sys.platform == "win32" else ("tar.xz" if args.variant == "gpu" else "tar.gz")
        )
        archive = output / f"{name}.{extension}"
        from bundle.gpu_payload import pack, split_runtime

        runtime = (
            split_runtime(product, output, version, sys.platform) if args.variant == "gpu" else None
        )
        pack(product, archive)
        if runtime:
            core = output / f"{name}-core.{extension}"
            pack(product, core, exclude=[item["path"] for item in runtime["files"]])
    if archive.stat().st_size >= 2 * 1024**3:
        raise RuntimeError("Archive exceeds GitHub's per-asset size limit")
    # Smoke-check the artifact after extraction, outside the source tree and environment.
    with tempfile.TemporaryDirectory(prefix="bts-release-") as temporary:
        extracted = Path(temporary) / "Тест сборки 中文"
        extracted.mkdir()
        if sys.platform == "darwin":
            run("ditto", "-x", "-k", str(archive), str(extracted))
            executable = extracted / product.name / "Contents" / "MacOS" / "telegram-search"
        elif sys.platform == "win32":
            shutil.unpack_archive(archive, extracted)
            executable = extracted / product.name / "telegram-search.exe"
        else:
            with tarfile.open(archive) as incoming:
                incoming.extractall(extracted, filter="data")
            executable = extracted / product.name / "telegram-search"
        clean_env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "PYTHONHOME",
                "PYTHONPATH",
                "VIRTUAL_ENV",
                "UV_PROJECT_ENVIRONMENT",
                "LD_LIBRARY_PATH",
            }
        }
        try:
            result = run(
                str(executable),
                "--self-test",
                cwd=extracted,
                env=clean_env,
                capture_output=True,
                timeout=180,
            )
        except subprocess.CalledProcessError as exc:
            # The smoke test uses only public synthetic input, never a user's workspace.
            raise RuntimeError(
                "Artifact smoke failed:\n" + exc.stderr.decode(errors="replace")
            ) from exc
        smoke = json.loads(result.stdout)
        if args.variant == "gpu":
            from telegram_search.inference.gpu_cache import RuntimeCache

            core_extracted = Path(temporary) / "GPU core 中文"
            shutil.unpack_archive(core, core_extracted)
            core_product = core_extracted / product.name
            cache = RuntimeCache(Path(temporary) / "runtime-cache")
            # Test the deployed cache seed path on each native OS (especially
            # Windows loaded-library permissions), then hydrate from blobs too.
            cache.hydrate(core_product, seed=extracted / product.name, local_assets=output)
            shutil.rmtree(core_extracted)
            shutil.unpack_archive(core, core_extracted)
            RuntimeCache(Path(temporary) / "empty-cache").hydrate(core_product, local_assets=output)
            core_result = run(
                str(core_product / executable.name),
                "--self-test",
                cwd=core_extracted,
                env=clean_env,
                capture_output=True,
                timeout=180,
            )
            if json.loads(core_result.stdout) != smoke:
                raise RuntimeError("GPU core does not match the full package checks")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    manifest = {
        "artifact": archive.name,
        "bytes": archive.stat().st_size,
        "sha256": sha256(archive),
        "commit": commit,
        "source_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=REPO, text=True).strip()
        ),
        "platform": sys.platform,
        "arch": arch,
        "variant": args.variant,
        "python": platform.python_version(),
        "checks": smoke,
        "dependencies": sorted(inventory, key=lambda item: item["name"]),
    }
    manifest_path = archive.with_name(f"{name}.json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    archive.with_name(f"{name}.sha256").write_text(
        f"{manifest['sha256']}  {archive.name}\n", encoding="utf-8"
    )
    if args.variant == "gpu":
        core_manifest = {
            **manifest,
            "artifact": core.name,
            "bytes": core.stat().st_size,
            "sha256": sha256(core),
            "gpu_runtime": runtime,
            "gpu_core_verified": True,
        }
        core.with_name(f"{name}-core.json").write_text(
            json.dumps(core_manifest, indent=2), encoding="utf-8"
        )
        core.with_name(f"{name}-core.sha256").write_text(
            f"{core_manifest['sha256']}  {core.name}\n", encoding="utf-8"
        )
    print(json.dumps({key: manifest[key] for key in ("artifact", "bytes", "sha256", "checks")}))


if __name__ == "__main__":
    main()
