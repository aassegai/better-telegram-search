"""Owned package snapshots and atomic custom-model registrations."""

import json
import os
import re
import shutil
import tempfile
from importlib.resources import files
from pathlib import Path

from filelock import FileLock

from telegram_search.model_adapters.manifest import AdapterManifest, plain_path
from telegram_search.security.privacy import repository_warning
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize


class AdapterStore:
    def __init__(self, workspace):
        workspace = Path(workspace).expanduser().resolve()
        self.workspace = workspace
        self.root = plain_path(workspace / "models" / "custom-adapters")

    def _initialize(self):
        warning = repository_warning(self.workspace)
        if warning:
            raise UserError(warning)
        plain_path(self.root).mkdir(mode=0o700, parents=True, exist_ok=True)

    def stage(self, manifest_path, *, check=lambda: None, on_progress=None):
        manifest_path = plain_path(Path(manifest_path))
        manifest = AdapterManifest.read(manifest_path)
        manifest.verify_files(manifest_path.parent, check=check, on_progress=on_progress)
        self._initialize()
        total = sum(item["bytes"] for item in manifest.data["files"])
        if shutil.disk_usage(self.root).free < total + 64 * 1024**2:
            raise UserError("Not enough free space to stage the custom model package.")
        temporary = Path(tempfile.mkdtemp(prefix=".checking-", dir=self.root))
        copied = 0
        try:
            for item in manifest.data["files"]:
                target = temporary / item["path"]
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with (
                    (manifest_path.parent / item["path"]).open("rb") as source,
                    target.open("xb") as destination,
                ):
                    while block := source.read(1024**2):
                        check()
                        destination.write(block)
                        copied += len(block)
                        if on_progress:
                            on_progress(
                                {
                                    "phase": "copying",
                                    "completed": copied,
                                    "total": total,
                                    "unit": "bytes",
                                }
                            )
            (temporary / "manifest.json").write_text(manifest.canonical, encoding="utf-8")
            manifest.verify_files(temporary, closed=True, check=check, on_progress=on_progress)
            return manifest, temporary
        except BaseException:
            shutil.rmtree(temporary)
            raise

    def runner(self):
        self._initialize()
        source = files("telegram_search.model_adapters").joinpath("worker_runtime.py").read_bytes()
        import hashlib

        digest = hashlib.sha256(source).hexdigest()
        path = plain_path(self.root / f"runner-{digest}.py")
        if path.exists():
            if path.read_bytes() != source:
                raise UserError("Custom model worker file is damaged.")
            return path
        with tempfile.NamedTemporaryFile(dir=self.root, prefix=".runner-", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(source)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def register(self, manifest, staged, *, python, report):
        self._initialize()
        staged = plain_path(Path(staged))
        if staged.parent != self.root or not staged.name.startswith(".checking-"):
            raise UserError("Only a checked package snapshot can be registered.")
        if report.get("identity") != manifest.identity or report.get("state") != "passed":
            raise UserError("Run a successful adapter smoke test before registration.")
        manifest.verify_files(staged, closed=True)
        stored = AdapterManifest.read(staged / "manifest.json")
        if stored != manifest:
            raise UserError("Adapter manifest changed during validation.")
        with FileLock(plain_path(self.root / ".registry.lock"), timeout=0):
            registry = self.registrations()
            previous = registry.get(manifest.identity)
            if previous and previous["environment"] != report["environment"]:
                raise UserError(
                    "The adapter environment changed. Update the manifest revision "
                    "and validate it again before registration."
                )
            if manifest.identity not in registry and len(registry) >= 256:
                raise UserError("Custom model registry is full.")
            registry[manifest.identity] = {
                "identity": manifest.identity,
                "python": str(Path(python).expanduser().absolute()),
                "environment": report["environment"],
                "summary": manifest.summary(),
            }
            encoded = serialize(registry)
            if len(encoded.encode()) > 4 * 1024**2:
                raise UserError("Custom model registry is too large.")
            destination = plain_path(self.root / manifest.identity)
            if destination.exists():
                if AdapterManifest.read(destination / "manifest.json") != manifest:
                    raise UserError("Registered adapter manifest is damaged.")
                manifest.verify_files(destination, closed=True)
                shutil.rmtree(staged)
            else:
                staged.rename(destination)
            path = plain_path(self.root / "registry.json")
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.root, prefix=".registry-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                try:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
            try:
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return registry[manifest.identity]

    def registrations(self):
        path = plain_path(self.root / "registry.json")
        if not path.exists():
            return {}
        if path.stat().st_size > 4 * 1024**2:
            raise UserError("Custom model registry is too large.")
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(result, dict)
                or len(result) > 256
                or not all(
                    isinstance(k, str)
                    and re.fullmatch(r"[0-9a-f]{64}", k)
                    and isinstance(v, dict)
                    and v.get("identity") == k
                    and isinstance(v.get("summary"), dict)
                    and isinstance(v.get("environment"), dict)
                    and isinstance(v.get("python"), str)
                    for k, v in result.items()
                )
            ):
                raise ValueError()
            return result
        except (ValueError, OSError, RecursionError) as exc:
            raise UserError("Custom model registry is damaged.") from exc

    def get(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise UserError("Invalid custom model identity.")
        record = self.registrations().get(identity)
        if not record or record.get("identity") != identity:
            raise UserError("Custom model is not registered.")
        package = plain_path(self.root / identity)
        try:
            manifest = AdapterManifest.read(package / "manifest.json")
        except (OSError, ValueError, RecursionError) as exc:
            raise UserError("Registered adapter manifest is unavailable or damaged.") from exc
        if manifest.identity != identity:
            raise UserError("Registered adapter manifest is damaged.")
        return manifest, package, record
