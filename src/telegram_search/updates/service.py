import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import psutil

from telegram_search import __version__
from telegram_search.config.runtime import frozen
from telegram_search.shared.errors import UserError
from telegram_search.updates import network
from telegram_search.updates.archive import executable, extract
from telegram_search.updates.installer import (
    application_root,
    atomic_json,
    build_info,
    clean_environment,
    committed,
    copy_installation,
    recovery_path,
    tree_digest,
    validate_installation,
    write_recovery,
)
from telegram_search.updates.manifest import required_checks, select_release, validate_manifest


class UpdateService:
    def __init__(self, workspace, shutdown=None):
        self.workspace = workspace
        self.root = workspace / "cache/updates"
        if self.root.is_symlink() or not self.root.resolve().is_relative_to(workspace):
            raise UserError("Папка обновлений должна находиться внутри workspace.")
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.task = None
        self.shutdown_callback = shutdown
        self.selected = None
        self.product = None
        self.archive = None
        self.manifest = None
        self.process = None
        self.recovery_required = False
        self.current_variant = build_info().get("variant", "cpu") if frozen() else "cpu"
        self.arch = {"AMD64": "x86_64", "aarch64": "arm64"}.get(
            platform.machine(), platform.machine()
        )
        self.data = {
            "state": "idle",
            "error": None,
            "available_version": None,
            "variant": self.current_variant,
            "notes": "",
            "completed_bytes": 0,
            "total_bytes": 0,
            "installation_nonce": None,
            "commit_nonce": None,
            "rollback_nonce": None,
        }
        if self.status_path.is_file() and self.status_path.stat().st_size <= 64_000:
            try:
                stored = json.loads(self.status_path.read_text(encoding="utf-8"))
                for key in ("installation_nonce", "commit_nonce", "rollback_nonce"):
                    if key in stored:
                        self.data[key] = stored[key]
                if stored.get("state") in {"installing", "updated", "rolled_back"}:
                    self.data.update({key: stored[key] for key in self.data if key in stored})
                    if stored["state"] == "installing" and not os.environ.get(
                        "BTS_UPDATE_HEALTHCHECK"
                    ):
                        nonce = stored.get("installation_nonce")
                        if isinstance(nonce, str) and committed(self.status_path, nonce):
                            self._set(state="updated", error=None, commit_nonce=nonce)
                        else:
                            self._set(
                                state="failed", error="Предыдущая установка обновления прервана."
                            )
            except (ValueError, OSError):
                pass
        if not os.environ.get("BTS_UPDATE_HEALTHCHECK") and self._pending_recovery():
            self.recovery_required = True
            self._set(
                state="recovery_required",
                error=(
                    "Установка обновления прервана после замены приложения. "
                    "Закройте приложение и используйте файл восстановления рядом с ним."
                ),
            )

    def _pending_recovery(self):
        for directory in self.root.iterdir():
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or not re.fullmatch(r"[a-f0-9]{32}", directory.name)
                or committed(self.status_path, directory.name)
            ):
                continue
            try:
                transaction = json.loads((directory / "transaction.json").read_text())
                plan = json.loads((directory / "plan.json").read_text())
                if plan["nonce"] == directory.name and (
                    transaction.get("phase") == "rolling_back"
                    or (
                        transaction.get("phase") in {"replacing", "starting"}
                        and Path(plan["backup"]).is_dir()
                    )
                ):
                    return True
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return False

    @property
    def status_path(self):
        return self.root / "status.json"

    def status(self):
        with self.lock:
            if self.data["state"] == "installing" and not (self.task and self.task.is_alive()):
                try:
                    stored = json.loads(self.status_path.read_text(encoding="utf-8"))
                    if stored.get("state") in {"updated", "rolled_back", "failed"}:
                        self.data.update({key: stored[key] for key in self.data if key in stored})
                    elif self.process and self.process.poll() is not None:
                        self._set(
                            state="failed",
                            error=(
                                "Установщик обновления остановился. "
                                "Используйте файл восстановления рядом с приложением."
                            ),
                        )
                except (OSError, ValueError):
                    pass
            return {
                **self.data,
                "current_version": __version__,
                "current_variant": self.current_variant,
                "install_supported": frozen() and self.shutdown_callback is not None,
                "gpu_build_supported": (sys.platform, self.arch)
                in {("linux", "x86_64"), ("win32", "x86_64")},
            }

    def _set(self, **values):
        with self.lock:
            self.data.update(values)
            atomic_json(self.status_path, self.data)

    def _submit(self, function, state, *args, initial=None):
        with self.lock:
            if (
                self.stop.is_set()
                or self.recovery_required
                or (self.task and self.task.is_alive())
                or self.data["state"] == "installing"
            ):
                raise UserError("Операция обновления уже выполняется.")
            self._set(state=state, error=None, **(initial or {}))

            def work():
                try:
                    function(*args)
                except Exception as exc:
                    self._set(
                        state="failed",
                        error=str(exc)
                        if isinstance(exc, UserError)
                        else "Не удалось обновить приложение. Проверьте сеть и свободное место.",
                    )

            self.task = threading.Thread(target=work, name="app-update", daemon=True)
            self.task.start()
        return self.status()

    def check(self, variant=None):
        variant = variant or self.current_variant
        if variant not in {"cpu", "gpu"}:
            raise UserError("Недопустимый вариант сборки.")
        return self._submit(
            self._check,
            "checking",
            variant,
            initial={
                "variant": variant,
                "available_version": None,
                "notes": "",
                "completed_bytes": 0,
                "total_bytes": 0,
            },
        )

    def _check(self, variant):
        with self.lock:
            self.selected = None
            self.product = self.archive = self.manifest = None
        self._cleanup_cache()
        release = network.read_json(network.API + "/releases/latest")
        selected = select_release(
            release, __version__, sys.platform, self.arch, variant, self.current_variant
        )
        with self.lock:
            self.selected = selected
            self.product = self.archive = self.manifest = None
        self._set(
            state="available" if selected else "up_to_date",
            variant=variant,
            available_version=selected["version"] if selected else None,
            notes=selected["notes"] if selected else "",
            completed_bytes=0,
            total_bytes=selected["archive"]["size"] if selected else 0,
        )

    def download(self):
        with self.lock:
            if not self.selected or self.data["state"] not in {"available", "failed", "ready"}:
                raise UserError("Сначала проверьте наличие обновления.")
            return self._submit(self._download, "downloading", initial={"completed_bytes": 0})

    def _download(self):
        selected = self.selected
        fresh = network.read_json(network.API + "/releases/tags/" + selected["tag"])
        checked = select_release(
            fresh, __version__, sys.platform, self.arch, selected["variant"], self.current_variant
        )
        keys = ("id", "name", "size", "digest", "browser_download_url")
        if not checked or any(
            checked[kind].get(key) != selected[kind].get(key)
            for kind in ("archive", "report")
            for key in keys
        ):
            raise UserError("Релиз изменился. Проверьте обновления заново.")
        manifest = validate_manifest(
            network.read_json(selected["report"]["browser_download_url"], limit=100_000), selected
        )
        size = selected["archive"]["size"]
        if shutil.disk_usage(self.root).free < size * 4 + 512 * 1024**2:
            raise UserError("Недостаточно места для скачивания и проверки обновления.")
        directory = self.root / uuid.uuid4().hex
        directory.mkdir(mode=0o700)
        archive = directory / selected["archive"]["name"]
        digest = hashlib.sha256()
        completed = 0
        with (
            network.open_url(selected["archive"]["browser_download_url"]) as response,
            archive.open("xb") as output,
        ):
            while True:
                if self.stop.is_set():
                    raise UserError("Обновление остановлено.")
                data = response.read(1024**2)
                if not data:
                    break
                completed += len(data)
                if completed > size:
                    raise UserError("Размер скачанного обновления не совпадает с релизом.")
                output.write(data)
                digest.update(data)
                self._set(completed_bytes=completed, total_bytes=size)
            output.flush()
            os.fsync(output.fileno())
        if completed != size or "sha256:" + digest.hexdigest() != selected["archive"]["digest"]:
            raise UserError("Контрольная сумма обновления не совпадает с релизом.")
        self._set(state="verifying")
        product = extract(archive, directory / "extracted", platform=sys.platform)
        self._hydrate_runtime(product, manifest, selected, completed)
        metadata = build_info(product)
        if (
            metadata.get("version") != selected["version"]
            or metadata.get("variant") != selected["variant"]
            or metadata.get("platform") != sys.platform
            or metadata.get("arch") != self.arch
        ):
            raise UserError("Содержимое сборки не соответствует релизу.")
        result = subprocess.run(
            [str(executable(product, sys.platform)), "--self-test"],
            env=clean_environment(),
            cwd=directory,
            capture_output=True,
            timeout=240,
            check=True,
        )
        checks = json.loads(result.stdout)
        if not required_checks(selected["variant"]) <= checks.keys() or any(
            value is not True for value in checks.values()
        ):
            raise UserError("Встроенная проверка обновления не пройдена.")
        with self.lock:
            self.product, self.archive, self.manifest = product, archive, manifest
        self._set(state="ready")

    def _hydrate_runtime(self, product, manifest, selected, completed=None):
        if not selected.get("gpu_core"):
            return
        from telegram_search.inference.gpu_cache import RuntimeCache, read_manifest

        runtime = read_manifest(product, sys.platform)
        if runtime != manifest.get("gpu_runtime"):
            raise UserError("GPU-библиотеки не соответствуют проверенному релизу.")
        # Reserve enough space even on a cache miss. Existing immutable objects
        # are reused by SHA, and changed libraries are always atomically replaced.
        needed = sum(item["bytes"] + item["compressed_bytes"] for item in runtime["files"])
        if shutil.disk_usage(self.root).free < needed + 512 * 1024**2:
            raise UserError("Недостаточно места для скачивания и проверки обновления.")
        total = selected["archive"]["size"] + sum(
            {item["asset"]: item["compressed_bytes"] for item in runtime["files"]}.values()
        )

        def progress(count):
            nonlocal completed
            if completed is not None:
                completed += count
                self._set(completed_bytes=completed, total_bytes=total)

        RuntimeCache(self.workspace).hydrate(
            product,
            seed=application_root(),
            progress=progress,
            stopped=self.stop.is_set,
        )
        if completed is not None:
            # Actual network bytes, including only missing/changed libraries.
            self._set(completed_bytes=completed, total_bytes=completed)

    def _cleanup_cache(self):
        """Keep one rollback snapshot; never touch an active copied helper."""
        current = self.data.get("installation_nonce")
        installation = application_root()
        snapshots = []
        for directory in self.root.iterdir():
            if directory.is_symlink() or not re.fullmatch(r"[a-f0-9]{32}", directory.name):
                continue
            snapshot = directory / "backup.sqlite"
            if snapshot.is_file() and (
                committed(self.status_path, directory.name)
                or self.data.get("rollback_nonce") == directory.name
            ):
                snapshots.append((snapshot.stat().st_mtime_ns, directory.name))
        retained = max(snapshots)[1] if snapshots else None
        for directory in self.root.iterdir():
            if directory.name == current and not (
                self.data.get("commit_nonce") == current
                or self.data.get("rollback_nonce") == current
            ):
                continue
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or not re.fullmatch(r"[a-f0-9]{32}", directory.name)
            ):
                continue
            try:
                handshake = directory / "handshake.json"
                if handshake.is_file():
                    value = json.loads(handshake.read_text())
                    process = psutil.Process(value["pid"])
                    if (
                        abs(process.create_time() - value["created"]) < 0.01
                        and process.is_running()
                    ):
                        continue
            except psutil.NoSuchProcess:
                pass
            except (OSError, ValueError, KeyError, TypeError, psutil.AccessDenied):
                continue
            path = directory / "plan.json"
            if path.is_file() and installation:
                try:
                    plan = json.loads(path.read_text())
                    if plan["target"] == str(installation) and plan["nonce"] == directory.name:
                        recovery_path(plan).unlink(missing_ok=True)
                        if directory.name not in {current, retained}:
                            backup = installation.parent / (".bts-backup-" + directory.name)
                            failed = installation.with_name(
                                installation.name + ".failed-" + directory.name
                            )
                            for folder in (backup, failed):
                                if folder.is_dir() and not folder.is_symlink():
                                    shutil.rmtree(folder)
                except (ValueError, OSError, KeyError):
                    continue
            if directory.name in {current, retained}:
                for name in ("helper", "verified"):
                    folder = directory / name
                    if folder.is_dir() and not folder.is_symlink():
                        shutil.rmtree(folder)
            else:
                shutil.rmtree(directory)

    def install(self, port):
        with self.lock:
            if self.data["state"] != "ready" or not self.product or not self.shutdown_callback:
                raise UserError("Сначала скачайте и проверьте обновление в готовой сборке.")
            validate_installation(application_root(), self.workspace)
            return self._submit(self._install, "installing", port)

    def _install(self, port):
        target = application_root()
        validate_installation(target, self.workspace)
        if self.stop.is_set():
            raise UserError("Обновление остановлено.")
        # Re-extract the authenticated archive instead of trusting a writable cache tree.
        with self.archive.open("rb") as stream:
            if (
                "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
                != self.selected["archive"]["digest"]
            ):
                raise UserError("Файлы обновления изменились после проверки.")
        nonce = uuid.uuid4().hex
        self._set(installation_nonce=nonce, commit_nonce=None, rollback_nonce=None)
        directory = self.root / nonce
        directory.mkdir(mode=0o700)
        stage = target.parent / (".bts-stage-" + nonce)
        helper = directory / "helper" / target.name
        required = sum(
            path.stat().st_size
            for path in self.product.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        old_size = sum(
            path.stat().st_size
            for path in target.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        cache_required = (
            required + old_size + (self.workspace / "data/app.sqlite").stat().st_size + 64 * 1024**2
        )
        target_required = required + 64 * 1024**2
        same_disk = self.root.stat().st_dev == target.parent.stat().st_dev
        if shutil.disk_usage(target.parent).free < target_required or shutil.disk_usage(
            self.root
        ).free < cache_required + (target_required if same_disk else 0):
            raise UserError("Недостаточно места рядом с приложением для установки обновления.")
        try:
            product = extract(self.archive, directory / "verified", platform=sys.platform)
            self._hydrate_runtime(product, self.manifest, self.selected)
            copy_installation(product, stage)
            copy_installation(target, helper)
            plan = {
                "workspace": str(self.workspace),
                "target": str(target),
                "stage": str(stage),
                "backup": str(target.parent / (".bts-backup-" + nonce)),
                "directory": str(directory),
                "helper": str(helper),
                "platform": sys.platform,
                "version": self.selected["version"],
                "nonce": nonce,
                "parent_pid": os.getpid(),
                "parent_created": psutil.Process().create_time(),
                "port": port,
                "stage_digest": tree_digest(stage),
            }
            path = directory / "plan.json"
            atomic_json(path, plan)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            write_recovery(plan, digest)
            with (directory / "helper.log").open("wb") as log:
                self.process = subprocess.Popen(
                    [str(executable(helper, sys.platform)), "--internal-update", str(path), digest],
                    env=clean_environment(),
                    cwd=directory,
                    stdout=log,
                    stderr=log,
                )
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and self.process.poll() is None:
                if (directory / "handshake.json").is_file():
                    self.shutdown_callback()
                    return
                if self.stop.is_set():
                    break
                time.sleep(0.2)
            raise UserError("Не удалось запустить установщик обновления. Текущая версия сохранена.")
        except Exception:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=15)
            if stage.exists():
                shutil.rmtree(stage)
            if "plan" in locals():
                recovery_path(plan).unlink(missing_ok=True)
            raise

    def close(self):
        self.stop.set()
        if self.task:
            self.task.join(timeout=35)
