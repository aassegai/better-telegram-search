"""Exercise publication gates with synthetic assets; never contact GitHub."""

import copy
import fnmatch
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "release_publication", Path(__file__).parents[1] / "scripts/publish_release.py"
)
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


@pytest.fixture
def release_case(monkeypatch):
    request = {
        "repository": "synthetic/repository",
        "tag": "v0.1.0",
        "build_commit": "a" * 40,
        "release_commit": "b" * 40,
        "build_run_id": "123",
        "draft_url": "https://example.test/draft",
        "title": "Synthetic release",
        "notes": "Synthetic public notes",
    }
    report = {"commit": request["build_commit"], "run_id": "123", "result": "success", "builds": {}}
    release = {
        "id": 42,
        "tag_name": "build-synthetic",
        "target_commitish": request["build_commit"],
        "html_url": request["draft_url"],
        "draft": True,
        "prerelease": False,
        "assets": [],
    }
    files, patches, written = {}, [], []
    for platform, extension in publisher.PLATFORMS.items():
        prefix = "better-telegram-search-0.1.0-" + platform
        archive = prefix + "." + extension
        payload = b"Public synthetic archive"
        build = {
            "state": "ready",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            "checks": dict.fromkeys(publisher.CHECKS, True),
        }
        report["builds"][platform] = build
        system, arch = platform.split("-", 1)
        manifest = {
            "commit": request["build_commit"],
            "source_dirty": False,
            "platform": system,
            "arch": arch,
            "artifact": archive,
            "sha256": build["sha256"],
            "bytes": build["bytes"],
            "checks": build["checks"],
        }
        files[archive] = payload
        files[prefix + ".json"] = json.dumps(manifest).encode()
        files[prefix + ".sha256"] = (build["sha256"] + "  " + archive + "\n").encode()
    for name, payload in files.items():
        release["assets"].append(
            {
                "id": len(release["assets"]) + 1,
                "name": name,
                "size": len(payload),
                "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "state": "uploaded",
                "download_count": 0,
                "browser_download_url": "https://example.test/files/" + name,
            }
        )

    def download(command, **kwargs):
        assert command[:3] == ["gh", "release", "download"]
        patterns = [command[i + 1] for i, value in enumerate(command) if value == "--pattern"]
        root = Path(command[command.index("--dir") + 1])
        for name, payload in files.items():
            if any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
                (root / name).write_bytes(payload)
                next(item for item in release["assets"] if item["name"] == name)[
                    "download_count"
                ] += 1

    def api(endpoint, *, method="GET", payload=None, **kwargs):
        if "/actions/runs/" in endpoint:
            return {
                "status": "completed",
                "conclusion": "success",
                "head_sha": request["build_commit"],
                "head_repository": {"full_name": request["repository"]},
                "head_branch": "main",
                "path": ".github/workflows/builds.yml",
            }
        if "/git/ref/tags/" in endpoint:
            return (
                None
                if "_tag_commit" not in release
                else {"object": {"type": "commit", "sha": release["_tag_commit"]}}
            )
        if endpoint.endswith("git/refs"):
            assert method == "POST" and payload["ref"] == "refs/tags/" + request["tag"]
            release["_tag_commit"] = payload["sha"]
            return {"object": {"type": "commit", "sha": payload["sha"]}}
        if endpoint.endswith("releases?per_page=100"):
            return [copy.deepcopy(release)]
        assert endpoint.endswith("releases/42")
        if method == "PATCH":
            patches.append(payload)
            release.update(payload)
            release["html_url"] = "https://example.test/releases/v0.1.0"
        return copy.deepcopy(release)

    monkeypatch.setattr(publisher.subprocess, "run", download)
    monkeypatch.setattr(publisher, "api", api)
    monkeypatch.setattr(
        publisher, "read_report", lambda *args: (copy.deepcopy(report), "metadata-sha")
    )
    monkeypatch.setattr(
        publisher, "write_report", lambda *args: written.append(copy.deepcopy(args))
    )
    monkeypatch.setenv("GITHUB_RUN_ID", "456")
    return request, report, release, files, patches, written


def test_publication_verifies_files_before_promoting_and_retry_is_idempotent(release_case):
    request, _, _, _, patches, written = release_case
    result = publisher.publish(request)
    assert result["state"] == "published" and result["archives_verified"] == 5
    assert len(patches) == 1 and patches[0]["target_commitish"] == request["release_commit"]
    assert patches[0]["draft"] is False and patches[0]["body"] == request["notes"]
    assert written[0][2]["draft"] is False
    assert publisher.publish(request)["state"] == "published"
    assert len(patches) == 1


@pytest.mark.parametrize(
    "corruption", ["check", "commit", "digest", "manifest", "checksum", "extra", "missing"]
)
def test_publication_rejects_unverified_assets_without_writing_release(release_case, corruption):
    request, report, release, files, patches, _ = release_case
    if corruption == "check":
        report["builds"]["linux-x86_64"]["checks"]["torch_absent"] = False
    elif corruption == "commit":
        report["commit"] = "c" * 40
    elif corruption == "digest":
        release["assets"][0]["digest"] = "sha256:" + "c" * 64
    elif corruption == "manifest":
        name = next(name for name in files if name.endswith(".json"))
        manifest = json.loads(files[name])
        manifest["source_dirty"] = True
        files[name] = json.dumps(manifest).encode()
    elif corruption == "checksum":
        files[next(name for name in files if name.endswith(".sha256"))] = b"incorrect checksum"
    elif corruption == "extra":
        release["assets"].append({"name": "private-export.json"})
    else:
        release["assets"].pop()
    with pytest.raises(ValueError):
        publisher.publish(request)
    assert patches == []


def test_archive_without_github_digest_is_downloaded_and_hashed(release_case):
    request, report, release, _, _, _ = release_case
    for item in release["assets"]:
        item["digest"] = None
    assert len(publisher.verify_assets(release, report, request)) == 15


def test_concurrent_creation_of_conflicting_tag_does_not_publish(release_case, monkeypatch):
    request, _, release, _, patches, _ = release_case
    original_api = publisher.api

    def api(endpoint, **kwargs):
        if endpoint.endswith("git/refs"):
            release["_tag_commit"] = "c" * 40
            raise RuntimeError("Reference already exists (HTTP 422)")
        return original_api(endpoint, **kwargs)

    monkeypatch.setattr(publisher, "api", api)
    with pytest.raises(ValueError, match="Release tag already targets another commit"):
        publisher.publish(request)
    assert release["draft"] is True and patches == []


@pytest.mark.parametrize("new_build", [False, True])
def test_report_conflict_preserves_publication_and_newer_build(
    release_case, monkeypatch, new_build
):
    request, report, release, _, patches, written = release_case
    original_write = publisher.write_report
    conflicts = []

    def write(*args):
        if not conflicts:
            conflicts.append(True)
            if new_build:
                report["run_id"] = "789"
            raise RuntimeError("Concurrent report update (HTTP 409)")
        return original_write(*args)

    monkeypatch.setattr(publisher, "write_report", write)
    result = publisher.publish(request)
    assert result["state"] == "published" and release["draft"] is False and len(patches) == 1
    assert len(written) == (0 if new_build else 1)


def test_unavailable_build_report_after_publication_returns_published_with_warning(
    release_case, monkeypatch
):
    request, _, release, _, _, _ = release_case

    def write(*args):
        raise RuntimeError("Status API unavailable (HTTP 503)")

    monkeypatch.setattr(publisher, "write_report", write)
    result = publisher.publish(request)
    assert result["state"] == "published" and release["draft"] is False
    assert "HTTP 503" in result["report_warning"]


def test_main_reports_confirmed_publication_when_final_journal_is_unavailable(
    release_case, monkeypatch, capsys
):
    request, _, release, _, _, _ = release_case
    monkeypatch.setattr(publisher, "load_request", lambda: request)
    original_write = publisher.write_report

    def write(repository, name, value, *args):
        if name == "release.json" and value["state"] == "published":
            raise RuntimeError("Journal unavailable (HTTP 503)")
        return original_write(repository, name, value, *args)

    monkeypatch.setattr(publisher, "write_report", write)
    publisher.main()
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "published" and release["draft"] is False
    assert "HTTP 503" in result["report_warning"]


@pytest.mark.parametrize("changed", ["README.md", "src/telegram_search/cli.py"])
def test_request_uses_its_committed_revision_and_rejects_application_changes(
    monkeypatch, tmp_path, changed
):
    request = {
        "repository": "synthetic/repository",
        "tag": "v0.1.0",
        "build_commit": "a" * 40,
        "build_run_id": "123",
    }
    path = tmp_path / ".github/releases/v0.1.0.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(request))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_REPOSITORY", request["repository"])
    monkeypatch.setenv("BTS_RELEASE_VERSION", "0.1.0")

    def git(*args):
        if args[0] == "log":
            return "b" * 40
        if args[0] == "rev-parse":
            return args[1].split("^", 1)[0]
        if args[0] == "show":
            return '[project]\nversion = "0.1.0"'
        assert args[0] == "diff"
        return changed

    monkeypatch.setattr(publisher, "git", git)
    if changed.startswith("src/"):
        with pytest.raises(ValueError, match="Release code differs"):
            publisher.load_request()
    else:
        assert publisher.load_request()["release_commit"] == "b" * 40
