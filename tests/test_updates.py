"""Synthetic update releases, hostile archives and transactional rollback; no network."""

import copy
import hashlib
import io
import json
import os
import stat
import tarfile
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from telegram_search.backend.api import create_app
from telegram_search.shared.errors import UserError
from telegram_search.updates import installer, network
from telegram_search.updates.archive import extract
from telegram_search.updates.manifest import CHECKS, select_release, validate_manifest
from telegram_search.updates.service import UpdateService


def release_case(payload=b"synthetic archive", variant="cpu"):
    suffix = "-gpu" if variant == "gpu" else ""
    prefix = "better-telegram-search-0.4.0-linux-x86_64" + suffix
    report = {
        "artifact": prefix + (".tar.xz" if variant == "gpu" else ".tar.gz"),
        "platform": "linux",
        "arch": "x86_64",
        "variant": variant,
        "source_dirty": False,
        "commit": "a" * 40,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "checks": dict.fromkeys(CHECKS, True),
    }
    assets = []
    for name, data in [
        (report["artifact"], payload),
        (prefix + ".json", json.dumps(report).encode()),
    ]:
        assets.append(
            {
                "id": len(assets) + 1,
                "name": name,
                "size": len(data),
                "state": "uploaded",
                "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                "browser_download_url": f"https://github.com/{network.REPOSITORY}/releases/download/v0.4.0/{name}",
            }
        )
    release = {
        "tag_name": "v0.4.0",
        "draft": False,
        "prerelease": False,
        "body": "Public release notes",
        "assets": assets,
    }
    return release, report


def selected_case(**kwargs):
    release, report = release_case(**kwargs)
    return select_release(release, "0.3.0", "linux", "x86_64", kwargs.get("variant", "cpu")), report


def test_release_selection_matches_os_arch_variant_and_never_downgrades():
    release, _ = release_case(variant="gpu")
    selected = select_release(release, "0.3.0", "linux", "x86_64", "gpu")
    assert selected["variant"] == "gpu"
    with pytest.raises(UserError):
        select_release(release, "0.3.0", "linux", "arm64", "gpu")
    with pytest.raises(UserError):
        select_release(release, "0.3.0", "linux", "x86_64", "cpu")
    assert select_release(release, "0.5.0", "linux", "x86_64", "gpu") is None
    assert select_release(release, "0.4.0", "linux", "x86_64", "gpu", "gpu") is None
    assert select_release(release, "0.4.0", "linux", "x86_64", "gpu", "cpu")


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(draft=True),
        lambda value: value.update(prerelease=True),
        lambda value: value.update(tag_name="v0.4.0/../../bad"),
        lambda value: value["assets"][0].update(browser_download_url="https://evil.test/update"),
        lambda value: value["assets"][0].update(digest=None),
        lambda value: value["assets"][0].update(size=3 * 1024**3),
        lambda value: value["assets"].append(copy.deepcopy(value["assets"][0])),
    ],
)
def test_untrusted_release_metadata_is_rejected(change):
    release, _ = release_case()
    change(release)
    with pytest.raises(UserError):
        select_release(release, "0.3.0", "linux", "x86_64")


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_dirty", True),
        ("sha256", "b" * 64),
        ("platform", "win32"),
        ("variant", "gpu"),
        ("commit", "HEAD"),
        ("bytes", 1),
        ("checks", {"csrf": False}),
    ],
)
def test_manifest_cannot_bypass_verified_build_contract(field, value):
    selected, report = selected_case()
    report[field] = value
    with pytest.raises(UserError):
        validate_manifest(report, selected)


@pytest.mark.parametrize("check", ["device_selection", "update_installer", "cuda_libraries"])
def test_gpu_release_requires_new_native_checks(check):
    selected, report = selected_case(variant="gpu")
    report["checks"]["cuda_libraries"] = True
    report["checks"].pop(check)
    with pytest.raises(UserError):
        validate_manifest(report, selected)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/update",
        "https://evil.test/update",
        "https://github.com.evil.test/update",
        "https://github.com@evil.test/update",
        "https://github.com:444/update",
        "file:///etc/passwd",
    ],
)
def test_network_rejects_untrusted_origins_and_redirects(url):
    with pytest.raises(UserError):
        network.validate_url(url)
    with pytest.raises(UserError):
        network.Redirects().redirect_request(None, None, 302, "", {}, url)


def zip_case(path, entries):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries:
            archive.writestr(name, value)


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "/outside",
        "C:/outside",
        "BetterTelegramSearch/../../outside",
        "BetterTelegramSearch\\outside",
    ],
)
def test_archive_traversal_does_not_write_outside_staging(tmp_path, name):
    archive = tmp_path / "update.zip"
    zip_case(archive, [(name, b"danger")])
    with pytest.raises(UserError):
        extract(archive, tmp_path / "staging", platform="win32")
    assert not (tmp_path / "staging").exists()
    assert not (tmp_path / "outside").exists()


def test_duplicate_paths_zip_bombs_and_external_links_are_rejected(tmp_path):
    archive = tmp_path / "update.zip"
    zip_case(archive, [("BetterTelegramSearch/A", b"a"), ("BetterTelegramSearch/a", b"b")])
    with pytest.raises(UserError):
        extract(archive, tmp_path / "duplicate", platform="win32")
    zip_case(archive, [("BetterTelegramSearch/large", b"0" * 10000)])
    with pytest.raises(UserError):
        extract(archive, tmp_path / "large", platform="win32", max_bytes=100)
    link = zipfile.ZipInfo("BetterTelegramSearch/link")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as incoming:
        incoming.writestr(link, "../../outside")
    with pytest.raises(UserError):
        extract(archive, tmp_path / "link", platform="win32")


@pytest.mark.skipif(os.name == "nt", reason="Requires symbolic link privileges")
def test_macos_framework_aliases_are_resolved_in_dependency_order(tmp_path):
    archive = tmp_path / "update.zip"
    root = "Better Telegram Search.app/Contents/Frameworks/Python.framework"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(root + "/Versions/3.12/Python", b"synthetic framework")
        for name, target in [("Python", "Versions/Current/Python"), ("Versions/Current", "3.12")]:
            member = zipfile.ZipInfo(root + "/" + name)
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(member, target)
    product = extract(archive, tmp_path / "framework", platform="darwin")
    assert (
        product / "Contents/Frameworks/Python.framework/Python"
    ).read_bytes() == b"synthetic framework"


def test_cyclic_archive_aliases_are_rejected(tmp_path):
    archive = tmp_path / "cycle.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name, target in [("a", "b"), ("b", "a")]:
            member = zipfile.ZipInfo("BetterTelegramSearch/" + name)
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(member, target)
    with pytest.raises(UserError):
        extract(archive, tmp_path / "cycle", platform="win32")


def test_download_checks_github_digest_before_running_the_new_executable(db, tmp_path, monkeypatch):
    from telegram_search.updates import service as module

    archive_path = tmp_path / "synthetic.tar.gz"
    metadata = json.dumps(
        {"version": "0.4.0", "variant": "cpu", "platform": "linux", "arch": "x86_64"}
    ).encode()
    with tarfile.open(archive_path, "w:gz") as output:
        member = tarfile.TarInfo("BetterTelegramSearch/_internal/build.json")
        member.size = len(metadata)
        output.addfile(member, io.BytesIO(metadata))
    payload = archive_path.read_bytes()
    release, report = release_case(payload)
    service = UpdateService(db.workspace)
    service.arch = "x86_64"
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(
        module, "build_info", lambda root: json.loads((root / "_internal/build.json").read_text())
    )
    monkeypatch.setattr(
        network, "read_json", lambda url, **kwargs: report if url.endswith(".json") else release
    )
    monkeypatch.setattr(network, "open_url", lambda url: io.BytesIO(payload))
    calls = []
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: (
            calls.append(args)
            or SimpleNamespace(stdout=json.dumps(dict.fromkeys(CHECKS, True)).encode())
        ),
    )
    service._check("cpu")
    service._download()
    assert service.status()["state"] == "ready" and len(calls) == 1
    assert service.status()["completed_bytes"] == len(payload)
    monkeypatch.setattr(network, "open_url", lambda url: io.BytesIO(payload[:-1] + b"0"))
    with pytest.raises(UserError):
        service._download()
    assert len(calls) == 1
    service.close()


@pytest.mark.skipif(os.name == "nt", reason="Requires symbolic link privileges")
def test_linux_library_links_stay_inside_the_extracted_application(tmp_path):
    archive = tmp_path / "update.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        file = tarfile.TarInfo("BetterTelegramSearch/_internal/libexample.so.1")
        file.size = 4
        output.addfile(file, io.BytesIO(b"test"))
        link = tarfile.TarInfo("BetterTelegramSearch/_internal/libexample.so")
        link.type = tarfile.SYMTYPE
        link.linkname = "libexample.so.1"
        output.addfile(link)
    product = extract(archive, tmp_path / "extracted", platform="linux")
    assert (product / "_internal/libexample.so").read_bytes() == b"test"


def installation(db, tmp_path):
    root = tmp_path / "original"
    stage = tmp_path / "stage"
    root.mkdir()
    stage.mkdir()
    (root / "version").write_text("old")
    (stage / "version").write_text("new")
    directory = db.workspace / "cache/updates" / ("a" * 32)
    directory.mkdir(parents=True)
    installer.atomic_json(
        directory.parent / "status.json",
        {
            "state": "installing",
            "installation_nonce": "a" * 32,
        },
    )
    return {
        "workspace": str(db.workspace),
        "target": str(root),
        "stage": str(stage),
        "backup": str(tmp_path / "backup"),
        "directory": str(directory),
        "stage_digest": installer.tree_digest(stage),
        "platform": "linux",
        "nonce": "a" * 32,
        "port": 9999,
        "version": "0.4.0",
    }


def test_installation_replaces_only_application_and_keeps_backup(db, tmp_path):
    plan = installation(db, tmp_path)
    model = db.workspace / "models/synthetic-model"
    model.write_bytes(b"private synthetic model remains local")
    installer.apply_plan(plan, restart=False)
    assert (tmp_path / "original/version").read_text() == "new"
    assert (tmp_path / "backup/version").read_text() == "old"
    assert model.read_bytes() == b"private synthetic model remains local"
    assert (db.workspace / "cache/updates" / ("a" * 32) / "backup.sqlite").is_file()


def test_tampered_staging_never_replaces_current_installation(db, tmp_path):
    plan = installation(db, tmp_path)
    (tmp_path / "stage/version").write_text("tampered")
    with pytest.raises(UserError):
        installer.apply_plan(plan, restart=False)
    assert (tmp_path / "original/version").read_text() == "old"
    assert not (tmp_path / "backup").exists()


def test_failed_startup_rolls_back_application_database_and_settings(db, tmp_path, monkeypatch):
    import gc

    gc.disable()
    try:
        _assert_failed_startup_rolls_back(db, tmp_path, monkeypatch)
    finally:
        gc.enable()
        gc.collect()


def _assert_failed_startup_rolls_back(db, tmp_path, monkeypatch):
    plan = installation(db, tmp_path)
    before = (db.workspace / "config.json").read_bytes()
    with db.connect() as conn:
        conn.execute("CREATE TABLE synthetic_update_test(value TEXT)")
        conn.execute("INSERT INTO synthetic_update_test VALUES('old')")
    calls = []

    def start(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            with db.connect() as conn:
                conn.execute("UPDATE synthetic_update_test SET value='new'")
            (db.workspace / "config.json").write_text('{"device":"gpu"}')
        return SimpleNamespace(pid=os.getpid(), poll=lambda: 1)

    monkeypatch.setattr(installer.subprocess, "Popen", start)
    with pytest.raises(UserError):
        installer.apply_plan(plan)
    assert len(calls) == 2
    assert (tmp_path / "original/version").read_text() == "old"
    assert (db.workspace / "config.json").read_bytes() == before
    with db.connect() as conn:
        assert conn.execute("SELECT value FROM synthetic_update_test").fetchone()[0] == "old"


def test_update_tasks_are_serialized_and_source_installation_is_never_overwritten(db, monkeypatch):
    service = UpdateService(db.workspace, lambda: None)
    waiting, release = threading.Event(), threading.Event()

    def check(variant):
        waiting.set()
        release.wait(timeout=2)

    monkeypatch.setattr(service, "_check", check)
    service.check()
    assert waiting.wait(timeout=1)
    with pytest.raises(UserError):
        service.check()
    release.set()
    service.task.join(timeout=2)
    service.product = db.workspace
    service._set(state="ready")
    with pytest.raises(UserError):
        service.install(8765)
    service.close()


def test_updates_api_requires_local_session_and_confirmation_is_not_user_controlled(tmp_path):
    with TestClient(create_app(tmp_path / "workspace"), base_url="http://localhost") as client:
        assert client.get("/api/updates").json()["install_supported"] is False
        assert client.post("/api/updates/check", json={}).status_code == 403
        token = client.get("/api/session").json()["token"]
        headers = {"X-Session-Token": token}
        assert (
            client.post(
                "/api/updates/confirm", headers=headers, json={"nonce": "a" * 32}
            ).status_code
            == 400
        )
        assert client.post("/api/updates/install", headers=headers).status_code == 400


def test_verified_startup_holds_background_work_until_helper_confirmation(tmp_path, monkeypatch):
    nonce = "a" * 32
    monkeypatch.setenv("BTS_UPDATE_HEALTHCHECK", nonce)
    with TestClient(create_app(tmp_path / "workspace"), base_url="http://localhost") as client:
        assert client.app.state.semantic.background is None
        assert client.app.state.media.background is None
        headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
        assert client.post("/api/rebuild", headers=headers).status_code == 503
        client.app.state.updates._set(state="installing", installation_nonce=nonce)
        (client.app.state.updates.root / nonce).mkdir()
        assert (
            client.post(
                "/api/updates/confirm", headers=headers, json={"nonce": "b" * 32}
            ).status_code
            == 400
        )
        assert (
            client.post("/api/updates/confirm", headers=headers, json={"nonce": nonce}).json()[
                "confirmed"
            ]
            is True
        )
        assert client.app.state.media.background.is_alive()
        assert client.post(
            "/api/updates/confirm", headers=headers, json={"nonce": nonce}
        ).json() == {"confirmed": True}
        client.app.state.updates._set(state="updated")
        assert client.post("/api/rebuild", headers=headers).status_code == 200


def test_abandoned_installation_does_not_prevent_normal_startup_or_next_check(db, monkeypatch):
    root = db.workspace / "cache/updates"
    root.mkdir(parents=True)
    installer.atomic_json(root / "status.json", {"state": "installing"})
    service = UpdateService(db.workspace)
    assert service.status()["state"] == "failed"
    monkeypatch.setattr(service, "_check", lambda variant: service._set(state="up_to_date"))
    service.check()
    service.task.join(timeout=2)
    assert service.status()["state"] == "up_to_date"
    service.close()


def test_durable_confirmation_opens_application_even_if_ui_status_write_fails(
    tmp_path, monkeypatch
):
    nonce = "a" * 32
    monkeypatch.setenv("BTS_UPDATE_HEALTHCHECK", nonce)
    with TestClient(create_app(tmp_path / "workspace"), base_url="http://localhost") as client:
        updates = client.app.state.updates
        updates._set(state="installing", installation_nonce=nonce)
        (updates.root / nonce).mkdir()
        original = installer.atomic_json

        def fail_ui_status(path, value):
            if path == updates.status_path:
                raise OSError("synthetic UI status failure")
            original(path, value)

        monkeypatch.setattr(installer, "atomic_json", fail_ui_status)
        headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
        for _ in range(2):
            assert client.post(
                "/api/updates/confirm", headers=headers, json={"nonce": nonce}
            ).json() == {"confirmed": True}
        assert installer.committed(updates.status_path, nonce)
        assert not client.app.state.update_verifying
        assert client.app.state.media.background.is_alive()
        assert client.post("/api/rebuild", headers=headers).status_code == 200


def test_lost_confirm_reply_cannot_undo_durable_commit(db, tmp_path, monkeypatch):
    plan = installation(db, tmp_path)
    monkeypatch.setattr(
        installer.subprocess,
        "Popen",
        lambda *a, **kw: SimpleNamespace(
            pid=os.getpid(),
            poll=lambda: None,
        ),
    )
    stopped = []
    monkeypatch.setattr(installer, "stop_child", lambda child: stopped.append(child))

    def confirm_then_lose_reply(*args):
        installer.commit_update(db.workspace, plan["nonce"])
        raise OSError("synthetic lost response")

    monkeypatch.setattr(installer, "health", confirm_then_lose_reply)
    installer.apply_plan(plan)
    assert not stopped
    assert (tmp_path / "original/version").read_text() == "new"
    installer.commit_update(db.workspace, plan["nonce"])
    assert installer.committed(Path(plan["directory"]).parent / "status.json", plan["nonce"])
    status_path = Path(plan["directory"]).parent / "status.json"
    installer.atomic_json(status_path, {"state": "checking", "installation_nonce": "b" * 32})
    assert installer.committed(status_path, plan["nonce"])


def test_external_recovery_restores_original_path_after_loss_between_renames(
    db, tmp_path, monkeypatch
):
    plan = installation(db, tmp_path)
    original = installer.os.rename

    def lose_helper(source, target):
        original(source, target)
        if Path(source) == Path(plan["target"]):
            raise SystemExit("synthetic process loss")

    monkeypatch.setattr(installer.os, "rename", lose_helper)
    with pytest.raises(SystemExit):
        installer.apply_plan(plan, restart=False)
    assert not Path(plan["target"]).exists()
    monkeypatch.setattr(installer.os, "rename", original)
    calls = []
    monkeypatch.setattr(installer.subprocess, "Popen", lambda *a, **kw: calls.append(a))
    installer.recover_plan(plan)
    assert (tmp_path / "original/version").read_text() == "old" and len(calls) == 1


def test_rollback_holds_confirmation_lock_until_terminal_decision(db, tmp_path, monkeypatch):
    from filelock import FileLock, Timeout

    plan = installation(db, tmp_path)
    monkeypatch.setattr(
        installer.subprocess,
        "Popen",
        lambda *a, **kw: SimpleNamespace(pid=os.getpid(), poll=lambda: 1),
    )
    checked = []

    def stop_child(child):
        # A separate FileLock instance must be excluded even within this process.
        with pytest.raises(Timeout):
            with FileLock(Path(plan["directory"]) / ".commit.lock", timeout=0):
                pass
        checked.append(True)

    monkeypatch.setattr(installer, "stop_child", stop_child)
    with pytest.raises(UserError):
        installer.apply_plan(plan)
    assert checked == [True]
    with pytest.raises(UserError):
        installer.commit_update(db.workspace, plan["nonce"])


def test_manual_start_after_unconfirmed_swap_blocks_writes_and_workers(db, tmp_path, monkeypatch):
    plan = installation(db, tmp_path)
    installer.atomic_json(Path(plan["directory"]) / "plan.json", plan)
    original = installer.os.rename

    def lose_helper(source, target):
        original(source, target)
        if Path(source) == Path(plan["stage"]):
            raise SystemExit("synthetic loss after swap")

    monkeypatch.setattr(installer.os, "rename", lose_helper)
    with pytest.raises(SystemExit):
        installer.apply_plan(plan, restart=False)
    with TestClient(create_app(db.workspace), base_url="http://localhost") as client:
        assert client.app.state.semantic.background is None
        assert client.app.state.media.background is None
        assert client.get("/api/updates").json()["state"] == "recovery_required"
        headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
        assert (
            client.patch(
                "/api/settings", headers=headers, json={"search_result_limit": 100}
            ).status_code
            == 503
        )
        assert client.post("/api/updates/check", headers=headers, json={}).status_code == 503


@pytest.mark.parametrize("interruption", ["rename", "restore"])
def test_recovery_repeats_incomplete_rollback_even_after_backup_was_renamed(
    db, tmp_path, monkeypatch, interruption
):
    plan = installation(db, tmp_path)
    before = (db.workspace / "config.json").read_bytes()
    installer.apply_plan(plan, restart=False)
    (db.workspace / "config.json").write_text('{"device":"gpu"}')
    original_rename, original_restore = installer.os.rename, installer.restore

    def rename(source, target):
        original_rename(source, target)
        if Path(source) == Path(plan["backup"]) and interruption == "rename":
            raise SystemExit("synthetic loss after rollback rename")

    def restore(workspace, directory):
        (workspace / "data/app.sqlite").write_bytes(b"synthetic interrupted restore")
        raise SystemExit("synthetic loss during restore")

    monkeypatch.setattr(installer.os, "rename", rename)
    if interruption == "restore":
        monkeypatch.setattr(installer, "restore", restore)
    with pytest.raises(SystemExit):
        installer.recover_plan(plan)
    assert not Path(plan["backup"]).exists()
    assert (Path(plan["target"]) / "version").read_text() == "old"
    monkeypatch.setattr(installer.os, "rename", original_rename)
    monkeypatch.setattr(installer, "restore", original_restore)
    monkeypatch.setattr(installer.subprocess, "Popen", lambda *a, **kw: None)
    installer.recover_plan(plan)
    assert (db.workspace / "config.json").read_bytes() == before
    with db.connect() as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_failed_new_attempt_keeps_previous_confirmed_backup(db, tmp_path, monkeypatch):
    from telegram_search.updates import service as service_module

    plan = installation(db, tmp_path)
    installer.atomic_json(Path(plan["directory"]) / "plan.json", plan)
    installer.apply_plan(plan, restart=False)
    installer.atomic_json(
        Path(plan["directory"]) / "commit.json", {"state": "updated", "nonce": plan["nonce"]}
    )
    backup = Path(plan["target"]).parent / (".bts-backup-" + plan["nonce"])
    backup.mkdir()
    (backup / "version").write_text("old")
    new_nonce = "b" * 32
    installer.atomic_json(
        Path(plan["directory"]).parent / "status.json",
        {"state": "failed", "installation_nonce": new_nonce},
    )
    (Path(plan["directory"]).parent / new_nonce).mkdir()
    monkeypatch.setattr(service_module, "application_root", lambda: Path(plan["target"]))
    service = UpdateService(db.workspace)
    service._cleanup_cache()
    assert backup.is_dir()
    assert (Path(plan["directory"]) / "backup.sqlite").is_file()
    service.close()


def test_health_child_waits_for_durable_pid_before_opening_workspace(db, tmp_path, monkeypatch):
    plan = installation(db, tmp_path)
    directory = Path(plan["directory"])
    installer.atomic_json(directory / "transaction.json", {"phase": "starting"})
    waited = []

    def register_child(seconds):
        waited.append(seconds)
        installer.atomic_json(
            directory / "transaction.json",
            {
                "phase": "starting",
                "child_pid": os.getpid(),
                "child_created": installer.psutil.Process().create_time(),
            },
        )

    monkeypatch.setattr(installer.time, "sleep", register_child)
    installer.wait_for_startup(db.workspace, plan["nonce"])
    assert waited == [0.1]
    installer.atomic_json(directory / "transaction.json", {"phase": "rolling_back"})
    with pytest.raises(UserError):
        installer.wait_for_startup(db.workspace, plan["nonce"])
