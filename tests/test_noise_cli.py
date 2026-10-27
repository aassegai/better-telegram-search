import argparse
import json
import sys

import pytest
from conftest import export, load, message
from filelock import Timeout

from telegram_search import cli
from telegram_search.indexing.noise_commands import add_noise_parser, noise_command
from telegram_search.search.chunking.noise.contracts import AnalysisOptions
from telegram_search.shared.errors import UserError
from telegram_search.storage.noise import NoiseStore


def arguments(action, chat, job=None, *extra):
    parser = argparse.ArgumentParser()
    add_noise_parser(parser.add_subparsers(dest="command", required=True))
    values = ["noise", action, "--chat-id", chat]
    if job is not None:
        values.extend(("--job-id", job))
    return parser.parse_args([*values, *extra])


def test_cli_shared_workflow_keeps_private_forms_out_of_default_stdout(
    db, importer, tmp_path, monkeypatch, capsys
):
    chat = load(importer, export(tmp_path / "synthetic", [message(1, "бял"), message(2, "сука")]))[
        "chat_id"
    ]
    importer.shutdown()

    def command(*values):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "telegram-search",
                "--workspace",
                str(db.workspace),
                "noise",
                *values,
                "--chat-id",
                chat,
            ],
        )
        cli.main()
        output = capsys.readouterr()
        assert not output.err
        assert "бял" not in output.out and "сука" not in output.out
        assert str(tmp_path) not in output.out and chat not in output.out
        return json.loads(output.out)

    created = command("analyze")
    assert created["state"] == "ready"
    job = created["id"]
    assert command("list")["analyses"][0]["id"] == job
    assert command("status", "--job-id", job)["state"] == "ready"
    changed = command(
        "override", "--job-id", job, "--revision", "0", "--omit", "бял", "--keep", "сука"
    )
    assert changed["revision"] == 1 and changed["overrides_count"] == 2
    preview = command("preview", "--job-id", job, "--revision", "1")
    applied = command("apply", "--job-id", job, "--revision", "1")
    assert applied["policy_id"] == preview["policy_id"] and applied["scheduled_segments"] == 1
    assert command("apply", "--job-id", job, "--revision", "1")["already_applied"]


def test_cli_private_report_requires_opt_in(db, importer, tmp_path):
    chat = load(importer, export(tmp_path / "synthetic", [message(text="бял")]))["chat_id"]
    importer.shutdown()
    job = noise_command(db, arguments("analyze", chat))["id"]
    with pytest.raises(UserError, match="show-private"):
        noise_command(db, arguments("report", chat, job))
    result = noise_command(
        db, arguments("report", chat, job, "--show-private", "--decision", "review")
    )
    assert result["forms"][0]["form"] == "бял"
    assert noise_command(db, arguments("delete", chat, job))["deleted"]


def test_cli_writer_lease_prevents_recovering_jobs_owned_by_live_app(db, importer, tmp_path):
    chat = load(importer, export(tmp_path / "synthetic", [message()]))["chat_id"]
    store = NoiseStore(db)
    job, _ = store.create(chat, AnalysisOptions())
    with pytest.raises(Timeout):
        noise_command(db, arguments("status", chat, job))
    assert store.get(chat, job)["state"] == "queued"


def test_cli_failed_terminal_persistence_exits_nonzero_instead_of_waiting_forever(
    db, importer, tmp_path, monkeypatch, capsys
):
    chat = load(importer, export(tmp_path / "synthetic", [message(text="бял")]))["chat_id"]
    importer.shutdown()
    with db.connect() as conn:
        conn.execute(
            "CREATE TRIGGER synthetic_cli_failure BEFORE UPDATE OF state ON noise_analyses "
            "WHEN NEW.state IN ('ready','failed') "
            "BEGIN SELECT RAISE(ABORT,'private detail'); END"
        )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "telegram-search",
            "--workspace",
            str(db.workspace),
            "noise",
            "analyze",
            "--chat-id",
            chat,
        ],
    )
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["state"] == "failed" and result["error"] == "result_save_failed"
    assert "бял" not in output.out and "private detail" not in output.out


def test_cli_busy_recovery_is_a_bounded_safe_error(db, importer, tmp_path):
    chat = load(importer, export(tmp_path / "synthetic", [message()]))["chat_id"]
    importer.shutdown()
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(UserError, match="операцию с базой"):
            noise_command(db, arguments("list", chat))


def test_cli_interrupt_cancels_worker_and_releases_workspace_lease(
    db, importer, tmp_path, monkeypatch
):
    from telegram_search.indexing.noise_analysis import NoiseAnalysisService

    chat = load(importer, export(tmp_path / "synthetic", [message()]))["chat_id"]
    importer.shutdown()

    def held(self, job):
        while True:
            self._checkpoint(job)
            self.stop.wait(0.01)

    def interrupt(_seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(NoiseAnalysisService, "_analyze", held)
    monkeypatch.setattr("telegram_search.indexing.noise_commands.time.sleep", interrupt)
    with pytest.raises(UserError, match="отменена"):
        noise_command(db, arguments("analyze", chat))
    # Acquiring the next lease proves shutdown completed rather than merely
    # abandoning the still-running analysis thread.
    report = noise_command(db, arguments("list", chat))
    assert report["analyses"][0]["state"] == "cancelled"
