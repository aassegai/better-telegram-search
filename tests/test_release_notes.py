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
    assert notes.update_notes(request)["heading_removed"] is True
    assert len(patches) == 1


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
