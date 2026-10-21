"""Build/status writers must survive a concurrent diagnostic branch update."""

import base64
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "build_reports", Path(__file__).parents[1] / "scripts/report_builds.py"
)
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


def existing(run, sha):
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {"sha": sha, "content": base64.b64encode(json.dumps({"run_id": run}).encode()).decode()}
        ),
    )


def test_build_report_reloads_sha_after_conflict(monkeypatch):
    responses = iter([existing("100", "before"), existing("100", "after")])
    monkeypatch.setattr(reporter.subprocess, "run", lambda *_, **__: next(responses))
    monkeypatch.setattr(reporter.time, "sleep", lambda *_: None)
    writes = []

    def write(*args, payload):
        writes.append(payload)
        if len(writes) == 1:
            raise RuntimeError("branch changed (HTTP 409)")

    monkeypatch.setattr(reporter, "gh", write)
    result = {"run_id": "101", "commit": "a" * 40, "result": "pending"}
    assert reporter.write_report("synthetic/repo", result)
    assert [value["sha"] for value in writes] == ["before", "after"]
    assert json.loads(base64.b64decode(writes[-1]["content"])) == result


def test_older_build_cannot_replace_newer_report(monkeypatch):
    monkeypatch.setattr(reporter.subprocess, "run", lambda *_, **__: existing("102", "current"))
    monkeypatch.setattr(reporter, "gh", lambda *_args, **_kwargs: pytest.fail("Stale write"))
    assert not reporter.write_report("synthetic/repo", {"run_id": "101", "commit": "a" * 40})


def test_build_report_access_errors_surface_without_retry(monkeypatch):
    monkeypatch.setattr(reporter.subprocess, "run", lambda *_, **__: existing("100", "current"))
    writes = []

    def denied(*args, **kwargs):
        writes.append(args)
        raise RuntimeError("Resource not accessible (HTTP 403)")

    monkeypatch.setattr(reporter, "gh", denied)
    with pytest.raises(RuntimeError, match="403"):
        reporter.write_report("synthetic/repo", {"run_id": "101", "commit": "a" * 40})
    assert len(writes) == 1


def test_github_command_includes_the_failure_reason(monkeypatch):
    def failed(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr="branch changed (HTTP 409)")

    monkeypatch.setattr(reporter.subprocess, "run", failed)
    with pytest.raises(RuntimeError, match="branch changed.*409"):
        reporter.gh("api", "synthetic")
