import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import export, message
from fastapi.testclient import TestClient

from telegram_search.backend.api import create_app
from telegram_search.ingestion.importer import ImportService
from telegram_search.ingestion.inspection import ExportReader, scan_export
from telegram_search.ingestion.progress import ImportProgress, progress_snapshot
from telegram_search.shared.errors import UserError
from telegram_search.shared.text import serialize


def test_one_header_pass_counts_messages_and_hashes_even_with_trailing_metadata(
    tmp_path, monkeypatch
):
    path = tmp_path / "export.json"
    data = json.dumps(
        {
            "messages": [message(1), message(2), {"id": 3, "type": "service"}],
            "id": 100,
            "name": "synthetic",
            "type": "personal_chat",
        }
    ).encode()
    path.write_bytes(data)
    original = Path.open
    opens = []

    def opened(self, *args, **kwargs):
        if self == path:
            opens.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)
    metadata, fingerprint, total = scan_export(path)
    assert metadata == {"id": 100, "name": "synthetic", "type": "personal_chat"}
    assert fingerprint == hashlib.sha256(data).hexdigest() and total == 3
    assert len(opens) == 1


def test_source_changes_during_header_pass_are_rejected(tmp_path, monkeypatch):
    path = export(tmp_path / "source", [message()])
    original = ExportReader.read
    changed = False

    def read(self, size=-1):
        nonlocal changed
        data = original(self, size)
        if data and not changed:
            changed = True
            path.write_bytes(path.read_bytes() + b" ")
        return data

    monkeypatch.setattr(ExportReader, "read", read)
    with pytest.raises(UserError, match="JSON изменился"):
        scan_export(path)


def test_http_preview_is_visible_and_pausable_before_metadata_finishes(tmp_path, monkeypatch):
    path = export(tmp_path / "source", [message(i) for i in range(1, 6)])
    entered, release = threading.Event(), threading.Event()
    original = ExportReader.read
    held = False

    def read(self, size=-1):
        nonlocal held
        data = original(self, size)
        if data and not held:
            held = True
            entered.set()
            assert release.wait(10)
        return data

    monkeypatch.setattr(ExportReader, "read", read)
    with TestClient(create_app(tmp_path / "workspace"), base_url="http://127.0.0.1") as client:
        headers = {"X-Session-Token": client.get("/api/session").json()["token"]}
        try:
            began = time.perf_counter()
            response = client.post(
                "/api/import-previews", json={"json_path": str(path)}, headers=headers
            )
            assert response.status_code == 202 and time.perf_counter() - began < 2
            pid = response.json()["id"]
            assert entered.wait(2)
            status = client.get(f"/api/import-previews/{pid}").json()
            assert status["progress"]["phase"] == "metadata"
            assert status["progress"]["total"] == path.stat().st_size
            assert status["preparing"]
            assert client.get("/api/chats").json() == []
            assert client.get("/api/settings").status_code == 200
            paused = client.post(f"/api/import-previews/{pid}/pause", headers=headers).json()
            assert paused["state"] == "paused" and paused["progress"]["eta_seconds"] is None
            release.set()
            client.app.state.importer.futures[pid].result(timeout=5)
            client.post(f"/api/import-previews/{pid}/resume", headers=headers)
            report = client.app.state.importer.futures[pid].result(timeout=5)
            assert report["state"] == "ready" and report["total_messages"] == 5
            assert report["progress"]["phase"] == "preview_ready"
            assert report["progress"]["completed"] == report["progress"]["total"] == 5
            assert client.get("/api/chats").json() == []
            assert not report["preparing"]
        finally:
            release.set()


def test_background_apply_verification_can_pause_before_any_canonical_write(importer, db, tmp_path):
    path = export(tmp_path / "source", [message(i) for i in range(1, 6)])
    pid = importer.previews.create(str(path))
    assert importer.previews.run(pid)["state"] == "ready"
    entered, release = threading.Event(), threading.Event()
    held = False

    def observe(event):
        nonlocal held
        if event["phase"] == "verify_json" and not held:
            held = True
            entered.set()
            assert release.wait(10)

    importer.progress_observer = observe
    try:
        status = importer.previews.enqueue_apply(pid)
        assert status["operation"] == "apply"
        assert entered.wait(2)
        with db.connect() as conn:
            for table in ("chats", "source_roots", "imports", "messages"):
                assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert importer.previews.control(pid, "pause")["state"] == "paused"
        release.set()
        assert importer.futures[pid].result(timeout=5)["state"] == "paused"
        importer.previews.control(pid, "resume")
        applied = importer.futures[pid].result(timeout=5)
        assert applied["state"] == "applied"
        job = importer.futures[applied["applied_import_id"]].result(timeout=5)
        assert job["state"] == "completed" and job["processed"] == 5
        assert job["progress"]["phase"] == "import_completed"
        assert job["progress"]["completed"] == job["progress"]["total"] == 5
    finally:
        release.set()


def test_changed_json_cannot_create_an_import_through_background_apply(importer, db, tmp_path):
    path = export(tmp_path / "source", [message()])
    pid = importer.previews.create(str(path))
    assert importer.previews.run(pid)["state"] == "ready"
    export(path.parent, [message(2)])
    importer.previews.enqueue_apply(pid)
    result = importer.futures[pid].result(timeout=5)
    assert result["state"] == "failed" and "JSON изменился" in result["error"]
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM imports").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM chats").fetchone()[0] == 0


def test_failed_import_progress_uses_committed_checkpoint_and_retry_keeps_it(
    importer, tmp_path, monkeypatch
):
    importer.batch_size = 1
    path = export(tmp_path / "source", [message(i) for i in range(1, 6)])
    jid = importer.prepare(str(path))
    original = importer._apply_batch
    fail = True

    def apply(job, batch):
        if fail and batch[0][0]["id"] == 3:
            raise UserError("synthetic failure")
        return original(job, batch)

    monkeypatch.setattr(importer, "_apply_batch", apply)
    result = importer.run(jid)
    assert result["state"] == "failed"
    assert result["processed"] == result["progress"]["completed"] == 2
    assert result["progress"]["total"] == 5
    assert result["progress"]["rate"] is None and result["progress"]["eta_seconds"] is None
    fail = False
    importer.control(jid, "resume")
    result = importer.futures[jid].result(timeout=5)
    assert result["state"] == "completed" and result["processed"] == 5
    assert result["added"] == 5


def test_rate_needs_measured_samples_and_attachment_bytes_do_not_count_as_messages(monkeypatch):
    import telegram_search.ingestion.progress as module

    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "time", lambda: 1000 + clock[0])
    events = []
    importer = SimpleNamespace(stopping=threading.Event(), progress_observer=events.append)
    progress = ImportProgress(importer, "preview")
    progress.phase("checking_messages", total=100)
    for completed in (10, 20):
        clock[0] += 1.25
        progress.update(completed)
        assert events[-1]["rate"] is None
    clock[0] += 1.25
    progress.activity("checking_media", 1000000, 2000000)
    assert events[-1]["completed"] == 20
    clock[0] += 1.25
    progress.update(30)
    assert events[-1]["rate"] == pytest.approx(6)
    assert events[-1]["eta_seconds"] == pytest.approx(70 / 6)
    paused = progress_snapshot(serialize(progress.value), "paused", now=time.time() + 1000)
    assert paused["completed"] == 30 and paused["rate"] is None and paused["eta_seconds"] is None


def test_cli_renders_shared_progress_to_stderr_and_keeps_json_report_private(
    tmp_path, monkeypatch, capsys
):
    from telegram_search import cli

    path = export(tmp_path / "source", [message(1, "PRIVATE_MESSAGE_TEXT"), message(2)])
    monkeypatch.setattr(
        cli.sys,
        "argv",
        [
            "telegram-search",
            "--workspace",
            str(tmp_path / "workspace"),
            "import",
            str(path),
        ],
    )
    monkeypatch.setattr(cli.sys.stderr, "isatty", lambda: True)
    cli.main()
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["state"] == "completed" and report["processed"] == 2
    assert "metadata" in captured.err and "writing_messages" in captured.err
    assert "PRIVATE_MESSAGE_TEXT" not in captured.err and str(path) not in captured.err
    assert "PRIVATE_MESSAGE_TEXT" not in captured.out and str(path) not in captured.out


def test_v14_paused_import_migrates_and_resumes_its_committed_position(
    db, importer, tmp_path, monkeypatch
):
    path = export(tmp_path / "source", [message(i) for i in range(1, 6)])
    jid = importer.prepare(str(path))
    original = importer._apply_batch

    def pause_after_commit(job, batch):
        result = original(job, batch)
        importer.control(jid, "pause")
        return result

    monkeypatch.setattr(importer, "_apply_batch", pause_after_commit)
    paused = importer.run(jid)
    assert paused["state"] == "paused" and paused["processed"] == 2
    importer.shutdown()
    with db.connect() as conn:
        for table in (
            "search_tab_state",
            "search_tabs",
            "search_snapshot_owners",
            "search_history",
            "search_snapshots",
        ):
            conn.execute(f'DROP TABLE "{table}"')
        for table, columns in (
            ("imports", ("total_messages", "progress_json")),
            (
                "import_previews",
                ("total_messages", "progress_json", "preparation_json", "operation"),
            ),
        ):
            for column in columns:
                conn.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        conn.execute("DELETE FROM schema_migrations WHERE version>=15")
    db.initialize()
    restored = ImportService(db, batch_size=2)
    try:
        old = restored.get(jid)
        assert old["state"] == "paused" and old["processed"] == 2 and old["progress"] is None
        restored.control(jid, "resume")
        result = restored.futures[jid].result(timeout=5)
        assert result["state"] == "completed" and result["processed"] == result["added"] == 5
        with db.connect() as conn:
            assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 5
    finally:
        restored.shutdown()
