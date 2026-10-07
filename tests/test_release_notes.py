"""Synthetic release body edits; no GitHub access or private data."""

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

script_root = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(script_root))
try:
    spec = importlib.util.spec_from_file_location(
        "release_notes", script_root / "update_release_notes.py"
    )
    notes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(notes)
finally:
    sys.path.pop(0)

HEADING = "### Проверки и ограничения"


def test_removes_section_and_subsections_preserving_next_section():
    before = "Introduction\n\n### Downloads\n\nKeep this.\n\n"
    after = "### Help\n\nKeep that too.\n"
    body = before + HEADING + "\n\nRemove.\n\n#### Details\nRemove too.\n\n" + after
    assert notes.remove_section(body, HEADING) == before + after
    assert notes.remove_section(before + after, HEADING) == before + after


def test_ignores_fenced_heading_and_removes_last_section():
    before = "Example\n\n```md\n" + HEADING + "\n```\n\n"
    assert notes.remove_section(before + HEADING + "\n\nRemove.\n", HEADING) == before
    assert notes.remove_section(before, HEADING) == before


@pytest.fixture
def release_case(monkeypatch):
    request = {
        "repository": "synthetic/repository",
        "tag": "v0.1.1",
        "tag_commit": "a" * 40,
        "operation": "remove_section",
        "heading": HEADING,
    }
    release = {
        "id": 42,
        "tag_name": request["tag"],
        "draft": False,
        "body": "Keep.\n\n" + HEADING + "\n\nRemove.\n",
        "html_url": "https://example.test/release",
        "assets": [{"id": 10}],
    }
    patches = []

    def api(endpoint, *, method="GET", payload=None):
        if method == "PATCH":
            assert endpoint.endswith("releases/42")
            assert set(payload) == {"body"}
            patches.append(payload)
            release.update(payload)
        return copy.deepcopy(release)

    monkeypatch.setattr(notes.publisher, "api", api)
    monkeypatch.setattr(notes.publisher, "tag_commit", lambda *args: request["tag_commit"])
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    return request, release, patches


def test_only_body_updated_with_idempotent_retry(release_case):
    request, release, patches = release_case
    assert notes.update_notes(request)["state"] == "updated"
    assert release["body"] == "Keep.\n\n"
    assert release["assets"] == [{"id": 10}]
    assert notes.update_notes(request)["operation"] == "remove_section"
    assert len(patches) == 1


def test_batch_metadata_failure_does_not_hide_confirmed_notes(monkeypatch, capsys):
    import json

    monkeypatch.setattr(
        notes,
        "load_request",
        lambda: [{"repository": "synthetic", "tag": "v1.0.0"}, {"tag": "v2.0.0"}],
    )
    monkeypatch.setattr(
        notes, "update_notes", lambda request: {"state": "updated", "tag": request["tag"]}
    )

    def unavailable(*args):
        raise RuntimeError("HTTP503")

    monkeypatch.setattr(notes.publisher, "write_report", unavailable)
    notes.main()
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "updated" and len(report["releases"]) == 2
    assert report["report_warning"] == "HTTP503"


@pytest.mark.parametrize("invalid", ["tag", "draft", "race"])
def test_rejects_wrong_tag_draft_or_concurrent_body_edit(monkeypatch, release_case, invalid):
    request, release, patches = release_case
    if invalid == "tag":
        monkeypatch.setattr(notes.publisher, "tag_commit", lambda *args: "b" * 40)
    elif invalid == "draft":
        release["draft"] = True
    else:
        initial = notes.publisher.api
        calls = 0

        def racing_api(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                release["body"] += "Concurrent edit.\n"
            return initial(*args, **kwargs)

        monkeypatch.setattr(notes.publisher, "api", racing_api)
    with pytest.raises(ValueError):
        notes.update_notes(request)
    assert not patches


def test_replaces_body_only_with_expected_original_and_retries_idempotently(release_case):
    import hashlib

    request, release, patches = release_case
    request.update(
        operation="replace_body",
        body="### What's new\n\n- Synthetic feature.\n",
        expected_body_sha256=hashlib.sha256(release["body"].encode()).hexdigest(),
    )
    notes.update_notes(request)
    notes.update_notes(request)
    assert len(patches) == 1 and release["body"] == request["body"]
    release["body"] = "Later human edit"
    with pytest.raises(ValueError, match="changed since"):
        notes.update_notes(request)
    assert len(patches) == 1
