"""A copied frozen helper replaces an installation only after its parent exits."""

import base64
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import closing
from pathlib import Path

import psutil
from filelock import FileLock

from telegram_search.config.runtime import frozen
from telegram_search.shared.errors import UserError
from telegram_search.updates.archive import executable
from telegram_search.updates.manifest import version


def atomic_json(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".update-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def application_root():
    if not frozen():
        return None
    path = Path(sys.executable).absolute()
    return path.parents[2] if sys.platform == "darwin" else path.parent


def build_info(root=None, platform=None):
    platform = platform or sys.platform
    root = root or application_root()
    if root is None:
        return {"variant": "cpu"}
    folder = root / "Contents/Frameworks" if platform == "darwin" else root / "_internal"
    path = folder / "build.json"
    if not path.is_file():
        raise UserError("В сборке отсутствуют сведения для автообновления.")
    return json.loads(path.read_text(encoding="utf-8"))


def clean_environment():
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "PYTHONHOME",
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "UV_PROJECT_ENVIRONMENT",
            "BTS_UPDATE_HEALTHCHECK",
            "BTS_DISABLE_UPDATE_CHECK",
        }
        and not key.startswith("_PYI")
    }
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if "LD_LIBRARY_PATH_ORIG" in environment:
        environment["LD_LIBRARY_PATH"] = environment.pop("LD_LIBRARY_PATH_ORIG")
    else:
        environment.pop("LD_LIBRARY_PATH", None)
    return environment


def tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\x00")
        if path.is_symlink():
            digest.update(b"link:" + os.readlink(path).encode())
        elif path.is_file():
            with path.open("rb") as stream:
                digest.update(hashlib.file_digest(stream, "sha256").digest())
            digest.update(str(path.stat().st_mode & 0o777).encode())
        elif path.is_dir():
            digest.update(b"directory")
        else:
            raise UserError("Недопустимый файл в папке обновления.")
        digest.update(b"\x00")
    return digest.hexdigest()


def validate_installation(root, workspace):
    if root is None:
        raise UserError("Автоустановка доступна только в готовой сборке приложения.")
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise UserError("Папка приложения недоступна для безопасного обновления.")
    if workspace.is_relative_to(root):
        raise UserError("Для автообновления workspace должен находиться вне папки приложения.")
    if not os.access(root.parent, os.W_OK):
        raise UserError("Нет прав записи в папку приложения. Перенесите сборку в доступную папку.")


def copy_installation(source, destination):
    # No user data are included: workspace and model store live outside this tree.
    shutil.copytree(source, destination, symlinks=True)


def recovery_path(plan):
    suffix = ".cmd" if plan["platform"] == "win32" else ".sh"
    return Path(plan["target"]).parent / ("Recover-BetterTelegramSearch-" + plan["nonce"] + suffix)


def write_recovery(plan, digest):
    command = [
        str(executable(Path(plan["helper"]), plan["platform"])),
        "--internal-recover",
        str(Path(plan["directory"]) / "plan.json"),
        digest,
    ]
    path = recovery_path(plan)
    if plan["platform"] == "win32":
        script = "& " + " ".join("'" + value.replace("'", "''") + "'" for value in command)
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        value = (
            "@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand "
            + encoded
            + "\r\n"
        )
    else:
        value = "#!/bin/sh\nexec " + shlex.join(command) + "\n"
    with path.open("x", encoding="utf-8", newline="") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o700)


def health(port, nonce, expected_version):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f"http://127.0.0.1:{port}"
    with opener.open(base + "/api/session", timeout=2) as response:
        session = json.load(response)
    if session.get("instance_id") != nonce:
        return False
    with opener.open(base + "/api/doctor", timeout=2) as response:
        doctor = json.load(response)
    if doctor.get("version") != expected_version or doctor.get("database_check") != "ok":
        return False
    request = urllib.request.Request(
        base + "/api/updates/confirm",
        data=json.dumps({"nonce": nonce}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-Session-Token": session["token"]},
    )
    with opener.open(request, timeout=5) as response:
        return json.load(response).get("confirmed") is True


def stop_child(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)


def wait_for_startup(workspace, nonce):
    """A health child cannot open the DB before the helper durably records its PID."""
    directory = workspace.resolve() / "cache/updates" / nonce
    if len(nonce) != 32 or any(character not in "0123456789abcdef" for character in nonce):
        raise UserError("Недопустимое подтверждение обновления.")
    created = psutil.Process().create_time()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            status = json.loads((directory.parent / "status.json").read_text())
            state = json.loads((directory / "transaction.json").read_text())
            if status.get("installation_nonce") != nonce or status.get("state") != "installing":
                break
            if state.get("phase") in {"rolling_back", "rolled_back"}:
                break
            if (
                state.get("phase") == "starting"
                and state.get("child_pid") == os.getpid()
                and abs(state.get("child_created", 0) - created) < 0.01
            ):
                return
        except (OSError, ValueError, TypeError):
            pass
        time.sleep(0.1)
    raise UserError(
        "Установщик не подтвердил запуск новой версии. Используйте файл восстановления."
    )


def snapshot(workspace, directory):
    database = workspace / "data/app.sqlite"
    with (
        closing(sqlite3.connect(database)) as source,
        closing(sqlite3.connect(directory / "backup.sqlite")) as target,
    ):
        source.backup(target)
    shutil.copyfile(workspace / "config.json", directory / "config.backup.json")


def committed(status_path, nonce):
    try:
        value = json.loads((status_path.parent / nonce / "commit.json").read_text(encoding="utf-8"))
        return value.get("state") == "updated" and value.get("nonce") == nonce
    except (ValueError, OSError):
        return False


def commit_update(workspace, nonce):
    with FileLock(workspace / "cache/updates" / nonce / ".commit.lock", timeout=10):
        _commit_update(workspace, nonce)


def _commit_update(workspace, nonce):
    path = workspace / "cache/updates/status.json"
    if committed(path, nonce):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("installation_nonce") == nonce and value.get("state") == "installing":
            value.update(state="updated", error=None, commit_nonce=nonce)
            try:
                atomic_json(path, value)
            except OSError:
                pass  # The durable decision already exists; UI persistence is best effort.
        return
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("state") != "installing" or value.get("installation_nonce") != nonce:
        raise UserError("Недопустимое подтверждение обновления.")
    value.update(state="updated", error=None, commit_nonce=nonce)
    # A per-transaction decision survives subsequent checks/installs changing
    # the UI status file. Once committed, this installation cannot be rolled back.
    atomic_json(path.parent / nonce / "commit.json", {"state": "updated", "nonce": nonce})
    try:
        atomic_json(path, value)
    except OSError:
        pass


def restore(workspace, directory):
    database = workspace / "data/app.sqlite"
    for suffix in ("-wal", "-shm"):
        database.with_name(database.name + suffix).unlink(missing_ok=True)
    shutil.copyfile(directory / "backup.sqlite", database)
    shutil.copyfile(directory / "config.backup.json", workspace / "config.json")


def apply_plan(plan, *, restart=True):
    """Filesystem transaction; callers validate the plan and wait for the parent."""
    workspace = Path(plan["workspace"])
    target, stage, backup = (Path(plan[key]) for key in ("target", "stage", "backup"))
    directory = Path(plan["directory"])
    status_path = directory.parent / "status.json"
    process = None
    replaced = False

    def report(state, error=None):
        value = json.loads(status_path.read_text(encoding="utf-8"))
        value.update(state=state, error=error)
        if state == "rolled_back":
            value["rollback_nonce"] = plan["nonce"]
        atomic_json(status_path, value)

    def phase(state, **values):
        atomic_json(directory / "transaction.json", {"phase": state, **values})

    try:
        with FileLock(workspace / ".writer.lock", timeout=10):
            if tree_digest(stage) != plan["stage_digest"]:
                raise UserError("Файлы обновления изменились после проверки.")
            snapshot(workspace, directory)
            phase("replacing")
            os.rename(target, backup)
            try:
                os.rename(stage, target)
                replaced = True
                phase("starting")
            except Exception:
                os.rename(backup, target)
                raise
        if not restart:
            report("updated")
            return
        environment = clean_environment()
        environment["BTS_UPDATE_HEALTHCHECK"] = plan["nonce"]
        with (directory / "startup.log").open("wb") as log:
            process = subprocess.Popen(
                [
                    str(executable(target, plan["platform"])),
                    "--workspace",
                    str(workspace),
                    "run",
                    "--port",
                    str(plan["port"]),
                    "--no-browser",
                ],
                cwd=target.parent,
                env=environment,
                stdout=log,
                stderr=log,
            )
        phase(
            "starting",
            child_pid=process.pid,
            child_created=psutil.Process(process.pid).create_time(),
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and process.poll() is None:
            if committed(status_path, plan["nonce"]):
                return
            try:
                if health(plan["port"], plan["nonce"], plan["version"]):
                    if not committed(status_path, plan["nonce"]):
                        raise UserError("Новая версия не подтвердила установку.")
                    return
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.5)
        raise UserError("Новая версия не запустилась; восстановлена предыдущая версия.")
    except Exception:
        # Confirmation and rollback must make one serialized decision.
        with FileLock(directory / ".commit.lock", timeout=10):
            if committed(status_path, plan["nonce"]):
                return
            stop_child(process)
            if replaced:
                with FileLock(workspace / ".writer.lock", timeout=10):
                    phase("rolling_back")
                    failed = target.with_name(target.name + ".failed-" + plan["nonce"])
                    os.rename(target, failed)
                    os.rename(backup, target)
                    restore(workspace, directory)
                    phase("rolled_back")
                report(
                    "rolled_back", "Новая версия не запустилась; восстановлена предыдущая версия."
                )
                if restart:
                    environment = clean_environment()
                    environment["BTS_DISABLE_UPDATE_CHECK"] = "1"
                    with (directory / "rollback.log").open("wb") as log:
                        subprocess.Popen(
                            [
                                str(executable(target, plan["platform"])),
                                "--workspace",
                                str(workspace),
                                "run",
                                "--port",
                                str(plan["port"]),
                                "--no-browser",
                            ],
                            cwd=target.parent,
                            env=environment,
                            stdout=log,
                            stderr=log,
                        )
            else:
                report("failed", "Обновление не установлено. Текущая версия сохранена.")
            raise


def recover_plan(plan):
    with FileLock(Path(plan["directory"]) / ".commit.lock", timeout=10):
        _recover_plan(plan)


def _recover_plan(plan):
    """Resume safely from the external recovery launcher after helper/process loss."""
    workspace, directory = Path(plan["workspace"]), Path(plan["directory"])
    target, backup = Path(plan["target"]), Path(plan["backup"])
    status_path = directory.parent / "status.json"
    if committed(status_path, plan["nonce"]):
        return  # Never roll back a version that has accepted writes.
    transaction = directory / "transaction.json"
    state = json.loads(transaction.read_text()) if transaction.is_file() else {}
    if state.get("child_pid"):
        try:
            child = psutil.Process(state["child_pid"])
            if abs(child.create_time() - state["child_created"]) < 0.01:
                child.terminate()
                try:
                    child.wait(timeout=30)
                except psutil.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=15)
        except psutil.NoSuchProcess:
            pass
    with FileLock(workspace / ".writer.lock", timeout=10):
        if committed(status_path, plan["nonce"]):
            return
        needs_restore = state.get("phase") == "rolling_back"
        if backup.is_dir():
            atomic_json(transaction, {"phase": "rolling_back"})
            needs_restore = True
            if target.exists():
                failed = target.with_name(target.name + ".failed-" + plan["nonce"])
                os.rename(target, failed)
            os.rename(backup, target)
        if needs_restore:
            restore(workspace, directory)
            atomic_json(transaction, {"phase": "rolled_back"})
        if not target.is_dir():
            raise UserError("Папка приложения недоступна для безопасного обновления.")
        value = json.loads(status_path.read_text())
        value.update(
            state="rolled_back", error="Восстановлена предыдущая версия после прерывания установки."
        )
        value["rollback_nonce"] = plan["nonce"]
        atomic_json(status_path, value)
    environment = clean_environment()
    environment["BTS_DISABLE_UPDATE_CHECK"] = "1"
    subprocess.Popen(
        [
            str(executable(target, plan["platform"])),
            "--workspace",
            str(workspace),
            "run",
            "--port",
            str(plan["port"]),
            "--no-browser",
        ],
        cwd=target.parent,
        env=environment,
    )


def main(plan_path, expected_digest, *, recover=False):
    """Internal-only entry point of the copied frozen executable."""
    path = Path(plan_path).resolve()
    data = path.read_bytes()
    if len(data) > 16_000 or hashlib.sha256(data).hexdigest() != expected_digest:
        raise UserError("Недопустимый план обновления.")
    plan = json.loads(data)
    workspace, directory = Path(plan["workspace"]), Path(plan["directory"])
    target, stage, backup = (Path(plan[key]) for key in ("target", "stage", "backup"))
    validate_installation(backup if recover and not target.exists() else target, workspace)
    version(plan["version"])
    if (
        plan["platform"] != sys.platform
        or path.parent != directory
        or directory.parent != workspace / "cache/updates"
        or directory.name != plan["nonce"]
        or application_root() != Path(plan["helper"])
        or not Path(plan["helper"]).is_relative_to(directory)
        or stage.parent != target.parent
        or backup.parent != target.parent
        or stage.name != ".bts-stage-" + plan["nonce"]
        or backup.name != ".bts-backup-" + plan["nonce"]
        or (backup.exists() and not recover)
        or type(plan["parent_pid"]) is not int
        or type(plan["port"]) is not int
        or not 1 <= plan["port"] <= 65535
    ):
        raise UserError("Недопустимый план обновления.")
    if not recover and tree_digest(stage) != plan["stage_digest"]:
        raise UserError("Файлы обновления изменились после проверки.")
    atomic_json(
        directory / "handshake.json",
        {
            "state": "waiting",
            "pid": os.getpid(),
            "created": psutil.Process().create_time(),
        },
    )
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            parent = psutil.Process(plan["parent_pid"])
            if abs(parent.create_time() - plan["parent_created"]) > 0.01:
                break
            if parent.status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.2)
    else:
        raise UserError("Приложение не остановилось. Обновление не установлено.")
    with FileLock(directory / ".installer.lock", timeout=0):
        if recover:
            recover_plan(plan)
        else:
            apply_plan(plan)
