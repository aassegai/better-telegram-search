"""Update garbage collection against synthetic installations and workspaces only."""

import hashlib
import os
import shutil
import threading
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient
from filelock import FileLock, Timeout

from telegram_search.backend.api import create_app
from telegram_search.updates import cleanup, installer
from telegram_search.updates import service as service_module
from telegram_search.updates.service import UpdateService


def transaction(db, tmp_path, nonce, *, phase="starting", confirmed=True, stamp=1):
    target = tmp_path / "BetterTelegramSearch"
    target.mkdir(exist_ok=True)
    (target / "application").write_bytes(b"current application")
    directory = db.workspace / "cache/updates" / nonce
    directory.mkdir(parents=True)
    plan = {
        "workspace": str(db.workspace),
        "directory": str(directory),
        "nonce": nonce,
        "target": str(target),
        "stage": str(target.parent / (".bts-stage-" + nonce)),
        "backup": str(target.parent / (".bts-backup-" + nonce)),
        "helper": str(directory / "helper" / target.name),
        "platform": "linux",
    }
    installer.atomic_json(directory / "plan.json", plan)
    installer.atomic_json(directory / "transaction.json", {"phase": phase})
    if confirmed:
        installer.atomic_json(directory / "commit.json", {"state": "updated", "nonce": nonce})
    for name in ("backup.sqlite", "config.backup.json", "helper.log", "startup.log"):
        (directory / name).write_bytes(b"synthetic " + name.encode())
    os.utime(directory / "backup.sqlite", ns=(stamp, stamp))
    (directory / "BetterTelegramSearch-synthetic.zip").write_bytes(b"archive")
    for name in ("extracted", "verified", "helper"):
        folder = directory / name
        folder.mkdir()
        (folder / "old-build").write_bytes(b"copied build")
    for key in ("stage", "backup"):
        folder = Path(plan[key])
        folder.mkdir()
        (folder / "old-build").write_bytes(b"copied build")
    failed = target.with_name(target.name + ".failed-" + nonce)
    failed.mkdir()
    (failed / "old-build").write_bytes(b"failed build")
    installer.recovery_path(plan).write_text("synthetic recovery launcher")
    return directory, target, plan


def test_cleanup_removes_archives_copied_builds_and_older_snapshots_but_keeps_user_data(
    db, tmp_path
):
    old, target, old_plan = transaction(db, tmp_path, "a" * 32, stamp=1)
    latest, _, plan = transaction(db, tmp_path, "b" * 32, stamp=2)
    protected = [
        db.workspace / "data/app.sqlite",
        db.workspace / "config.json",
        db.workspace / "models/synthetic-model",
        db.workspace / "cache/gpu-runtime/objects/synthetic-cached-library",
        db.workspace / "exports/result.json",
    ]
    for path in protected[2:]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"keep this local file")
    before = {path: path.read_bytes() for path in protected}
    # Old builds may hardlink immutable CUDA files from the reusable cache.
    os.link(protected[3], Path(old_plan["backup"]) / "cached-library")
    unrelated = latest.parent / "user-notes"
    unrelated.mkdir()
    (unrelated / "note").write_text("leave alone")
    discarded = latest.parent / ("c" * 32)
    discarded.mkdir()
    (discarded / "partial-download.zip").write_bytes(b"partial")
    assert not cleanup.cleanup_updates(db.workspace, target, current=latest.name)
    assert not old.exists() and not discarded.exists()
    assert (latest / "backup.sqlite").is_file()
    assert (latest / "config.backup.json").is_file()
    assert (latest / "commit.json").is_file()
    assert (latest / "startup.log").is_file()
    for name in ("helper", "verified", "extracted", "BetterTelegramSearch-synthetic.zip"):
        assert not (latest / name).exists()
    for obsolete in (old_plan["backup"], old_plan["stage"], plan["stage"]):
        assert not Path(obsolete).exists()
    assert Path(plan["backup"]).is_dir()
    assert not installer.recovery_path(plan).exists()
    assert not list(tmp_path.glob("*.failed-*"))
    assert (target / "application").read_bytes() == b"current application"
    assert (unrelated / "note").read_text() == "leave alone"
    assert {path: path.read_bytes() for path in protected} == before
    assert not cleanup.cleanup_updates(db.workspace, target, current=latest.name)


@pytest.mark.parametrize("phase", ["replacing", "starting", "rolling_back", "unknown"])
def test_incomplete_installation_is_left_available_for_recovery(db, tmp_path, phase):
    directory, target, plan = transaction(db, tmp_path, "a" * 32, phase=phase, confirmed=False)
    assert not cleanup.cleanup_updates(db.workspace, target)
    assert (directory / "helper/old-build").is_file()
    assert (directory / "backup.sqlite").is_file()
    assert Path(plan["backup"]).is_dir()
    assert Path(plan["stage"]).is_dir()
    assert installer.recovery_path(plan).is_file()


def test_rolled_back_attempt_discards_failed_build_but_retains_last_confirmed_backup(db, tmp_path):
    previous, target, previous_plan = transaction(db, tmp_path, "a" * 32)
    failed, _, plan = transaction(
        db, tmp_path, "b" * 32, phase="rolled_back", confirmed=False, stamp=2
    )
    assert not cleanup.cleanup_updates(db.workspace, target, current=failed.name)
    assert Path(previous_plan["backup"]).is_dir()
    assert (previous / "backup.sqlite").is_file()
    assert not (failed / "backup.sqlite").exists()
    assert (failed / "startup.log").is_file()
    assert not Path(plan["backup"]).exists()
    assert not target.with_name(target.name + ".failed-" + failed.name).exists()


@pytest.mark.parametrize("missing", ["backup.sqlite", "config.backup.json", "build"])
def test_incomplete_new_snapshot_cannot_displace_last_complete_backup(db, tmp_path, missing):
    previous, target, previous_plan = transaction(db, tmp_path, "a" * 32)
    newest, _, plan = transaction(db, tmp_path, "b" * 32, stamp=2)
    if missing == "build":
        shutil.rmtree(plan["backup"])
    else:
        (newest / missing).unlink()
    assert not cleanup.cleanup_updates(db.workspace, target, current=newest.name)
    assert (previous / "backup.sqlite").is_file()
    assert (previous / "config.backup.json").is_file()
    assert Path(previous_plan["backup"]).is_dir()
    assert not (newest / "helper").exists()


def test_remaining_recovery_material_is_preserved_when_no_snapshot_is_complete(db, tmp_path):
    directory, target, plan = transaction(db, tmp_path, "a" * 32)
    (directory / "config.backup.json").unlink()
    assert not cleanup.cleanup_updates(db.workspace, target)
    assert (directory / "backup.sqlite").is_file()
    assert Path(plan["backup"]).is_dir()
    assert not (directory / "helper").exists()


@pytest.mark.parametrize("running", [True, False])
def test_helper_process_identity_protects_only_the_live_helper(db, tmp_path, running):
    directory, target, _ = transaction(db, tmp_path, "a" * 32)
    installer.atomic_json(
        directory / "handshake.json",
        {"pid": os.getpid(), "created": psutil.Process().create_time() + (0 if running else 1)},
    )
    assert cleanup.cleanup_updates(db.workspace, target) is running
    assert (directory / "helper").exists() is running


def test_locked_installer_and_windows_open_file_are_retried(db, tmp_path, monkeypatch):
    directory, target, _ = transaction(db, tmp_path, "a" * 32)
    with FileLock(directory / ".installer.lock", timeout=0):
        assert cleanup.cleanup_updates(db.workspace, target)
        assert (directory / "helper").is_dir()
    original = cleanup.remove
    blocked = directory / "helper"

    def remove(path):
        if path == blocked:
            raise PermissionError("Synthetic Windows file still open")
        original(path)

    monkeypatch.setattr(cleanup, "remove", remove)
    assert cleanup.cleanup_updates(db.workspace, target)
    assert blocked.is_dir()
    monkeypatch.setattr(cleanup, "remove", original)
    assert not cleanup.cleanup_updates(db.workspace, target)
    assert not blocked.exists()
    assert (directory / "backup.sqlite").is_file()


def test_partially_deleted_metadata_is_collected_again_via_durable_tombstone(
    db, tmp_path, monkeypatch
):
    old, target, plan = transaction(db, tmp_path, "a" * 32)
    transaction(db, tmp_path, "b" * 32, stamp=2)
    tombstone = old.parent / (".cleanup-" + old.name)
    original = cleanup.remove

    def locked_metadata(path):
        if path == tombstone:
            assert not (path / "helper").exists()
            (path / "plan.json").unlink()
            raise PermissionError("Synthetic Windows transaction.json is still open")
        original(path)

    monkeypatch.setattr(cleanup, "remove", locked_metadata)
    assert cleanup.cleanup_updates(db.workspace, target)
    assert not old.exists()
    assert not Path(plan["backup"]).exists()
    assert (tombstone / "transaction.json").exists()
    assert not (tombstone / "plan.json").exists()
    monkeypatch.setattr(cleanup, "remove", original)
    assert not cleanup.cleanup_updates(db.workspace, target)
    assert not tombstone.exists()


def test_failed_helper_start_marks_preparation_discarded_without_touching_app(
    db, tmp_path, monkeypatch
):
    target = tmp_path / "BetterTelegramSearch"
    target.mkdir()
    (target / "application").write_bytes(b"old application")
    service = UpdateService(db.workspace)
    download = service.root / ("a" * 32)
    download.mkdir()
    archive = download / "update.zip"
    archive.write_bytes(b"synthetic verified archive")
    product = download / "extracted"
    product.mkdir()
    (product / "application").write_bytes(b"new application")
    service.archive, service.product = archive, product
    service.selected = {
        "version": "0.4.0",
        "archive": {"digest": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()},
    }
    monkeypatch.setattr(service_module, "application_root", lambda: target)

    def extract(archive, destination, **kwargs):
        shutil.copytree(product, destination)
        return destination

    def failed_start(*args, **kwargs):
        raise OSError("Synthetic helper launch error")

    monkeypatch.setattr(service_module, "extract", extract)
    monkeypatch.setattr(service_module.subprocess, "Popen", failed_start)
    try:
        with pytest.raises(OSError, match="Synthetic helper"):
            service._install(9999)
        directory = service.root / service.data["installation_nonce"]
        assert cleanup.read_json(directory / "transaction.json")["phase"] == "aborted"
        assert (directory / "helper").exists()
        assert not service._cleanup_cache()
        assert not (directory / "helper").exists()
        assert not (directory / "verified").exists()
        assert (target / "application").read_bytes() == b"old application"
        assert archive.exists()  # The verified download can still be retried.
    finally:
        service.close()


@pytest.mark.parametrize("field", ["backup", "stage", "target", "workspace", "directory", "nonce"])
def test_modified_plan_cannot_delete_unrelated_paths(db, tmp_path, field):
    directory, target, plan = transaction(db, tmp_path, "a" * 32)
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (outside / "private-file").write_bytes(b"do not delete")
    plan[field] = str(outside)
    installer.atomic_json(directory / "plan.json", plan)
    assert not cleanup.cleanup_updates(db.workspace, target)
    assert (directory / "helper").is_dir()
    assert (outside / "private-file").read_bytes() == b"do not delete"


@pytest.mark.skipif(os.name == "nt", reason="Windows symlinks may require privileges")
def test_cleanup_does_not_follow_symlinked_transactions_or_payloads(db, tmp_path):
    directory, target, _ = transaction(db, tmp_path, "a" * 32)
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (outside / "private-file").write_bytes(b"do not delete")
    (directory.parent / ("b" * 32)).symlink_to(outside, target_is_directory=True)
    (directory / "verified/redirect").symlink_to(outside, target_is_directory=True)
    assert not cleanup.cleanup_updates(db.workspace, target)
    assert (outside / "private-file").read_bytes() == b"do not delete"
    assert (directory.parent / ("b" * 32)).is_symlink()


@pytest.mark.parametrize("metadata", ["plan.json", "transaction.json", "commit.json"])
def test_malformed_metadata_is_left_intact(db, tmp_path, metadata):
    directory, target, _ = transaction(db, tmp_path, "a" * 32)
    (directory / metadata).write_text("[]")
    assert not cleanup.cleanup_updates(db.workspace, target)
    assert (directory / "helper").is_dir()


def test_service_protects_prepared_download_and_serializes_against_other_instances(db):
    service = UpdateService(db.workspace)
    directory = service.root / ("a" * 32)
    directory.mkdir()
    archive = directory / "update.zip"
    archive.write_bytes(b"ready for installation")
    service.archive = archive
    try:
        assert not service._cleanup_cache()
        assert archive.is_file()
        service.archive = None
        with FileLock(service.root / ".maintenance.lock", timeout=0):
            with pytest.raises(Timeout):
                service._cleanup_cache()
        assert archive.is_file()
        assert not service._cleanup_cache()
        assert not directory.exists()
    finally:
        service.close()


def test_slow_cleanup_does_not_block_status_and_stops_with_service(db, monkeypatch):
    service = UpdateService(db.workspace)
    entered, released = threading.Event(), threading.Event()

    def slow_cleanup(*args, **kwargs):
        entered.set()
        released.wait(timeout=5)
        return False

    monkeypatch.setattr(service_module, "cleanup_updates", slow_cleanup)
    try:
        service.start_cleanup()
        assert entered.wait(timeout=2)
        result = []
        reader = threading.Thread(target=lambda: result.append(service.status()))
        reader.start()
        reader.join(timeout=1)
        assert not reader.is_alive()
        assert result[0]["state"] == "idle"
    finally:
        released.set()
        service.close()
    assert not service.cleanup_task.is_alive()


def test_background_cleanup_retries_busy_files_without_changing_update_result(db, monkeypatch):
    service = UpdateService(db.workspace)
    calls = []

    def busy_once():
        calls.append(True)
        return len(calls) == 1

    monkeypatch.setattr(service, "_cleanup_cache", busy_once)
    monkeypatch.setattr(service.stop, "wait", lambda timeout: False)
    service._set(state="updated")
    try:
        service.start_cleanup()
        service.cleanup_task.join(timeout=2)
        assert len(calls) == 2
        assert service.status()["state"] == "updated"
    finally:
        service.close()


def test_application_cleans_on_startup_but_health_child_waits_for_confirmation(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        UpdateService,
        "start_cleanup",
        lambda service: calls.append(installer.committed(service.status_path, "a" * 32)),
    )
    with TestClient(create_app(tmp_path / "workspace"), base_url="http://localhost"):
        assert calls == [False]
    calls.clear()
    monkeypatch.setenv("BTS_UPDATE_HEALTHCHECK", "a" * 32)
    with TestClient(create_app(tmp_path / "workspace"), base_url="http://localhost") as client:
        assert not calls
        updates = client.app.state.updates
        (updates.root / ("a" * 32)).mkdir()
        updates._set(state="installing", installation_nonce="a" * 32)
        client.app.state.update_database_check = "ok"
        headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
        assert client.post(
            "/api/updates/confirm", headers=headers, json={"nonce": "a" * 32}
        ).json() == {"confirmed": True}
        assert calls == [True]
