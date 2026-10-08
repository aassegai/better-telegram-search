import os
import subprocess
from pathlib import Path

import pytest

from telegram_search.security.privacy import repository_warning
from telegram_search.shared.errors import UserError
from telegram_search.storage.database import Database


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", f"core.excludesFile={os.devnull}", *args],
        capture_output=True,
        check=False,
    )


def test_gitignore_private_data_and_source_allowlist(tmp_path):
    rules = Path(__file__).resolve().parents[1] / ".gitignore"
    (tmp_path / ".gitignore").write_text(rules.read_text())
    git(tmp_path, "init")
    private = [
        "workspace/data/app.sqlite",
        "workspace/data/app.sqlite-wal",
        "exports/chat/result.json",
        "exports/chat/photo.jpg",
        "test_chat_export/result.json",
        "telegram_search_requirements_and_plan (1).md",
        "telegram_search_onnx_runtime_agent.md",
        "plans/telegram_search_requirements_and_plan (1).md",
        "plans/telegram_search_onnx_runtime_agent.md",
        "plans/telegram_auto_sync_agent.md",
        "private/telegram.toml",
        "private/telegram/test.session",
        "test.session-journal",
        "test.session-wal",
        "test.session-shm",
        "models/weights.bin",
        ".env",
        "frontend/node_modules/react/index.js",
        "frontend/dist/index.html",
        "frontend/test-results/example.png",
        "frontend/playwright-report/index.html",
    ]
    public = [
        "uv.lock",
        "frontend/package-lock.json",
        ".env.example",
        ".env.dev.example",
        "configs/models.toml",
        "tests/fixtures/synthetic/chat.json",
        "tests/fixtures/synthetic/photo.png",
        "frontend/src/assets/icon.png",
        "src/telegram_search/storage/schema.sql",
    ]
    for item in private:
        assert git(tmp_path, "check-ignore", "--no-index", "-q", item).returncode == 0, item
    for item in public:
        assert git(tmp_path, "check-ignore", "--no-index", "-q", item).returncode == 1, item


def test_workspace_requires_shared_ignore_before_writing(tmp_path):
    git(tmp_path, "init")
    unprotected = tmp_path / "unsafe-data"
    with pytest.raises(UserError, match="исключён"):
        Database(unprotected).initialize()
    assert not unprotected.exists()
    (tmp_path / ".git" / "info" / "exclude").write_text("unsafe-data/\n")
    assert repository_warning(unprotected)
    (tmp_path / ".gitignore").write_text("/unsafe-data/\n")
    assert repository_warning(unprotected) is None
    Database(unprotected).initialize()
    git(tmp_path, "add", "-f", "unsafe-data/config.json")
    assert repository_warning(unprotected)


def test_repository_warning_never_executes_fsmonitor(tmp_path):
    if os.name == "nt":
        pytest.skip("synthetic shell hook regression is POSIX only")
    git(tmp_path, "init")
    marker = tmp_path / "hook-ran"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nprintf '\\0'\n")
    hook.chmod(0o755)
    git(tmp_path, "config", "core.fsmonitor", str(hook))
    assert repository_warning(tmp_path / "exports")
    assert not marker.exists()
