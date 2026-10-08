"""Collect updater-owned files only after an installation has reached a decision."""

import json
import logging
import os
import re
import shutil
from pathlib import Path

import psutil
from filelock import FileLock, Timeout

from telegram_search.updates.installer import recovery_path

logger = logging.getLogger(__name__)
NONCE = re.compile(r"[a-f0-9]{32}")
TOMBSTONE = re.compile(r"\.cleanup-[a-f0-9]{32}")
METADATA = {
    "plan.json",
    "commit.json",
    "transaction.json",
    "handshake.json",
    ".installer.lock",
    ".commit.lock",
    "helper.log",
    "startup.log",
    "rollback.log",
}
SNAPSHOT = {"backup.sqlite", "config.backup.json"}


def read_json(path):
    if path.is_symlink() or path.stat().st_size > 64_000:
        raise ValueError("Unsafe update metadata")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Invalid update metadata")
    return value


def helper_running(directory):
    path = directory / "handshake.json"
    if not path.exists():
        return False
    value = read_json(path)
    if type(value.get("pid")) is not int or value["pid"] <= 0:
        raise ValueError("Invalid updater PID")
    try:
        process = psutil.Process(value["pid"])
        return (
            abs(process.create_time() - value["created"]) < 0.01
            and process.is_running()
            and process.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False


def remove(path):
    # Never traverse directory links/junctions, including links inside a build.
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        path.unlink() if path.is_symlink() else path.rmdir()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def installation_plan(plan, directory, workspace, installation):
    """Paths outside the update cache are derived from the running installation."""
    nonce = directory.name
    if installation is None or installation.resolve() != installation:
        return False
    expected = {
        "workspace": str(workspace),
        "directory": str(directory),
        "nonce": nonce,
        "target": str(installation),
        "stage": str(installation.parent / (".bts-stage-" + nonce)),
        "backup": str(installation.parent / (".bts-backup-" + nonce)),
        "helper": str(directory / "helper" / installation.name),
    }
    return all(plan.get(key) == value for key, value in expected.items()) and plan.get(
        "platform"
    ) in {"linux", "win32", "darwin"}


def complete_snapshot(directory, plan):
    snapshot, settings = (directory / name for name in ("backup.sqlite", "config.backup.json"))
    backup = Path(plan["backup"])
    return (
        snapshot.is_file()
        and settings.is_file()
        and not snapshot.is_symlink()
        and not settings.is_symlink()
        and backup.is_dir()
        and backup.resolve() == backup
        and not backup.is_symlink()
    )


def cleanup_updates(workspace, installation, *, current=None, protected=(), stopped=lambda: False):
    """Return True if busy/locked files should be retried, without failing an update.

    Callers serialize this against download/install preparation. Incomplete
    transactions and unrecognized plans are left intact for manual recovery.
    The GPU library cache is intentionally reusable across application versions.
    """
    root = workspace / "cache/updates"
    if root.is_symlink() or root.resolve() != root:
        return False
    records = []
    retry = False
    for directory in root.iterdir():
        if (
            not TOMBSTONE.fullmatch(directory.name)
            or directory.is_symlink()
            or not directory.is_dir()
            or directory.resolve() != directory
        ):
            continue
        if stopped():
            return True
        try:
            # This name is a durable terminal decision: external folders and
            # payload were already collected before the atomic rename.
            remove(directory)
        except OSError:
            logger.debug("Update cleanup tombstone is still locked", exc_info=True)
            retry = True
    for directory in root.iterdir():
        if (
            not NONCE.fullmatch(directory.name)
            or directory.is_symlink()
            or not directory.is_dir()
            or directory.resolve() != directory
            or directory in protected
        ):
            continue
        try:
            path = directory / "plan.json"
            plan = read_json(path) if path.exists() else None
            commit = directory / "commit.json"
            value = read_json(commit) if commit.exists() else {}
            decision = value.get("state") == "updated" and value.get("nonce") == directory.name
            transaction = directory / "transaction.json"
            phase = read_json(transaction).get("phase") if transaction.exists() else None
            terminal = phase in {"rolled_back", "aborted"}
            if plan is not None and (
                not installation_plan(plan, directory, workspace, installation)
                or not (decision or terminal)
            ):
                continue
            if (
                plan is not None
                and phase == "aborted"
                and (
                    Path(plan["backup"]).exists()
                    or Path(plan["backup"]).is_symlink()
                    or not installation.is_dir()
                )
            ):
                continue
            if plan is None and (transaction.exists() or decision):
                continue  # Missing plan in an installation is not a discarded download.
            snapshot = directory / "backup.sqlite"
            stamp = (
                snapshot.stat().st_mtime_ns
                if decision and complete_snapshot(directory, plan)
                else None
            )
            records.append((directory, plan, stamp, decision))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    snapshots = [(stamp, directory.name) for directory, _, stamp, _ in records if stamp is not None]
    retained = max(snapshots)[1] if snapshots else None
    for directory, plan, _, decision in records:
        if stopped():
            return True
        try:
            if helper_running(directory):
                retry = True
                continue
            lock_path = directory / ".installer.lock"
            if lock_path.is_symlink():
                continue
            with FileLock(lock_path, timeout=0):
                # If all snapshots are damaged, keep their remaining recovery
                # files rather than deleting the last available backup material.
                keep = directory.name == retained or (retained is None and decision)
                keep_metadata = keep or (plan is not None and directory.name == current)
                if plan is not None:
                    recovery_path(plan).unlink(missing_ok=True)
                    folders = [
                        installation.parent / (".bts-stage-" + directory.name),
                        installation.with_name(installation.name + ".failed-" + directory.name),
                    ]
                    if not keep:
                        folders.append(installation.parent / (".bts-backup-" + directory.name))
                    for folder in folders:
                        remove(folder)
                # Delete payload first, preserving every decision until we can
                # durably mark the entire obsolete directory as garbage.
                for path in directory.iterdir():
                    if path.name in METADATA or (keep and path.name in SNAPSHOT):
                        continue
                    remove(path)
            if not keep_metadata:
                tombstone = root / (".cleanup-" + directory.name)
                if tombstone.exists() or tombstone.is_symlink():
                    raise OSError("Update cleanup tombstone already exists")
                directory.rename(tombstone)
                if os.name == "posix":
                    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                remove(tombstone)
        except (OSError, ValueError, KeyError, TypeError, psutil.AccessDenied, Timeout):
            # Windows may keep the helper executable/log open briefly after exit.
            # A subsequent pass/startup retries, and never changes the update result.
            logger.debug("Update cleanup deferred for %s", directory.name, exc_info=True)
            retry = True
    return retry
