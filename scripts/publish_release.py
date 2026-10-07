"""Publish one explicitly requested, verified draft without local API credentials.

Only checked-in release requests on main authorize publication. All files and
native reports are checked before the draft is promoted; tags are never replaced.
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from telegram_search.inference.gpu_manifest import validate as validate_gpu_manifest  # noqa: E402

CHECKS = {
    "sqlite_fts5",
    "lancedb",
    "onnx_cpu",
    "tokenizers",
    "safetensors",
    "ocr_rus_eng",
    "frontend_http",
    "csrf",
    "torch_absent",
    "unicode_paths",
}
PLATFORMS = {
    "linux-x86_64": "tar.gz",
    "linux-arm64": "tar.gz",
    "win32-x86_64": "zip",
    "darwin-x86_64": "zip",
    "darwin-arm64": "zip",
}
GPU_PLATFORMS = {"linux-x86_64-gpu": "tar.xz", "win32-x86_64-gpu": "zip"}
NEW_CHECKS = {"update_installer", "device_selection"}


def platforms(request):
    numbers = tuple(int(part) for part in request["tag"][1:].split("."))
    return {**PLATFORMS, **GPU_PLATFORMS} if numbers >= (0, 3, 0) else PLATFORMS


PUBLICATION_FILES = {
    "README.md",
    "README.en.md",
    "docs/portable-builds.en.md",
    "docs/portable-builds.md",
    "scripts/publish_release.py",
    ".github/workflows/publish-release.yml",
    "tests/test_release_publication.py",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def api(endpoint, *, payload=None, method="GET", missing=False):
    command = ["gh", "api", "--method", method, endpoint]
    if payload is not None:
        command += ["--input", "-"]
    result = subprocess.run(
        command,
        input=json.dumps(payload) if payload is not None else None,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        if missing and "(HTTP 404)" in result.stderr:
            return None
        raise RuntimeError("GitHub API failed: " + result.stderr[:1000])
    return json.loads(result.stdout)


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def read_report(repository, name):
    source = api(f"repos/{repository}/contents/{name}?ref=build-status", missing=True)
    return (
        (json.loads(base64.b64decode(source["content"])), source["sha"]) if source else (None, None)
    )


def write_report(repository, name, value, sha=None):
    if sha is None:
        _, sha = read_report(repository, name)
    payload = {
        "branch": "build-status",
        "message": f"Release status: {value.get('tag', 'update')}",
        "content": base64.b64encode(json.dumps(value, indent=2).encode()).decode(),
    }
    if sha:
        payload["sha"] = sha
    api(f"repos/{repository}/contents/{name}", method="PUT", payload=payload)


def load_request():
    require(os.environ["GITHUB_REF"] == "refs/heads/main", "Publication requires main")
    version = os.environ.get("BTS_RELEASE_VERSION")
    if version:
        require(re.fullmatch(r"\d+\.\d+\.\d+", version), "Invalid release version")
        path = Path(f".github/releases/v{version}.json")
    else:
        require(os.environ["GITHUB_EVENT_NAME"] == "push", "Explicit version required")
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        files = git(
            "diff", "--name-only", event["before"], event["after"], "--", ".github/releases"
        ).splitlines()
        require(len(files) == 1, "Push must request exactly one release")
        path = Path(files[0])
    require(
        re.fullmatch(r"\.github/releases/v\d+\.\d+\.\d+\.json", path.as_posix()),
        "Invalid request path",
    )
    require(
        not path.is_symlink() and path.resolve().is_relative_to(Path.cwd().resolve()),
        "Request must be local",
    )
    request = json.loads(path.read_text(encoding="utf-8"))
    # The commit carrying the explicit request also carries the release README.
    # This avoids a self-referential commit SHA in the request and stays stable
    # when the workflow is retried from a newer main.
    request["release_commit"] = git("log", "-1", "--format=%H", "--", path.as_posix())
    require(request["tag"] == path.stem, "Request tag mismatch")
    require(request["repository"] == os.environ["GITHUB_REPOSITORY"], "Repository mismatch")
    for key in ("build_commit", "release_commit"):
        require(re.fullmatch(r"[0-9a-f]{40}", request[key]), "Full commit SHA required")
    require(re.fullmatch(r"[1-9]\d*", request["build_run_id"]), "Invalid build run")
    for commit in (request["build_commit"], request["release_commit"]):
        require(git("rev-parse", f"{commit}^{{commit}}") == commit, "Commit missing")
        version = tomllib.loads(git("show", f"{commit}:pyproject.toml"))["project"]["version"]
        require(request["tag"] == "v" + version, "Project version mismatch")
    changes = git(
        "diff", "--name-only", request["build_commit"], request["release_commit"]
    ).splitlines()
    require(
        all(name in PUBLICATION_FILES or name == path.as_posix() for name in changes),
        "Release code differs from verified build",
    )
    return request


def verify_build(report, request):
    require(report is not None, "Build report missing")
    require(
        report["commit"] == request["build_commit"] and report["run_id"] == request["build_run_id"],
        "Wrong build provenance",
    )
    require(
        report["result"] == "success" and set(report["builds"]) == set(platforms(request)),
        "All CPU and GPU native builds required",
    )
    for platform, build in report["builds"].items():
        checks = CHECKS | (NEW_CHECKS if len(platforms(request)) == 7 else set())
        if platform.endswith("-gpu"):
            checks |= {"cuda_libraries"}
        require(
            build["state"] == "ready" and checks <= build["checks"].keys(),
            "Native report incomplete",
        )
        require(all(value is True for value in build["checks"].values()), "Native check failed")
        require(re.fullmatch(r"[0-9a-f]{64}", build["sha256"]), "Invalid archive checksum")
        require(
            type(build["bytes"]) is int and 0 < build["bytes"] < 2 * 1024**3,
            "Invalid archive size",
        )


def verify_manifest(manifest, request, platform, build, archive):
    variant = "gpu" if platform.endswith("-gpu") else "cpu"
    system, arch = platform.removesuffix("-gpu").split("-", 1)
    require(
        manifest["commit"] == request["build_commit"] and manifest["source_dirty"] is False,
        "Unverified source tree",
    )
    require(
        manifest["platform"] == system
        and manifest["arch"] == arch
        and manifest.get("variant", "cpu") == variant
        and manifest["artifact"] == archive,
        "Wrong platform artifact",
    )
    require(
        manifest["sha256"] == build["sha256"] and manifest["bytes"] == build["bytes"],
        "Manifest checksum mismatch",
    )
    require(manifest["checks"] == build["checks"], "Manifest checks mismatch")


def tag_commit(repository, tag):
    ref = api(f"repos/{repository}/git/ref/tags/{tag}", missing=True)
    if not ref:
        return None
    obj = ref["object"]
    for _ in range(5):
        if obj["type"] == "commit":
            return obj["sha"]
        require(obj["type"] == "tag", "Release tag must resolve to a commit")
        obj = api(f"repos/{repository}/git/tags/{obj['sha']}")["object"]
    raise ValueError("Nested tag limit exceeded")


def ensure_tag(repository, tag, commit):
    target = tag_commit(repository, tag)
    if target is not None:
        require(target == commit, "Release tag already targets another commit")
        return
    try:
        api(
            f"repos/{repository}/git/refs",
            method="POST",
            payload={"ref": "refs/tags/" + tag, "sha": commit},
        )
    except RuntimeError:
        # Creating a ref is atomic. Another writer may have won the race;
        # accept only the same approved commit and never replace the ref.
        target = tag_commit(repository, tag)
        if target is None:
            raise
        require(target == commit, "Release tag already targets another commit")
    require(tag_commit(repository, tag) == commit, "Release tag creation not confirmed")


def asset_fingerprint(release):
    # Downloads increment counters; only compare immutable file identity/data.
    return sorted(
        (item["id"], item["name"], item["size"], item.get("digest"), item["state"])
        for item in release["assets"]
    )


def verify_assets(release, report, request):
    version = request["tag"][1:]
    targets = platforms(request)
    prefixes = {key: f"better-telegram-search-{version}-{key}" for key in targets}
    expected = {
        name + suffix
        for key, name in prefixes.items()
        for suffix in ("." + targets[key], ".json", ".sha256")
    }
    cores = {}
    if tuple(int(part) for part in version.split(".")) >= (0, 3, 1):
        for target, prefix in prefixes.items():
            if not target.endswith("-gpu"):
                continue
            core = report["builds"][target].get("core", {})
            require(core.get("gpu_core_verified") is True, "GPU core verification missing")
            require(
                core.get("checks") == report["builds"][target]["checks"],
                "GPU core checks must match the verified full package",
            )
            runtime = validate_gpu_manifest(core.get("gpu_runtime"), target.split("-", 1)[0])
            require(runtime["version"] == version, "GPU runtime version mismatch")
            cores[target] = core
            expected.update(
                prefix + "-core" + suffix
                for suffix in (
                    "." + targets[target],
                    ".json",
                    ".sha256",
                )
            )
            expected.update(item["asset"] for item in runtime["files"])
    assets = {item["name"]: item for item in release["assets"]}
    require(
        set(assets) == expected and len(release["assets"]) == len(expected) <= 100,
        "Release must contain exactly the required archives with reports and checksums",
    )
    require(all(item["state"] == "uploaded" for item in assets.values()), "Upload incomplete")
    require(
        all(
            0 < assets[prefixes[target] + "-core.json"]["size"] <= 100_000
            and 0 < assets[prefixes[target] + "-core.sha256"]["size"] <= 300
            and 0 < core["bytes"] < 2 * 1024**3
            for target, core in cores.items()
        ),
        "Unexpected GPU core metadata size",
    )
    require(
        all(
            0 < assets[name]["size"] <= maximum
            for prefix in prefixes.values()
            for name, maximum in ((prefix + ".json", 100_000), (prefix + ".sha256", 300))
        ),
        "Unexpected metadata size",
    )
    with tempfile.TemporaryDirectory(prefix="bts-publish-") as temporary:
        root = Path(temporary)
        subprocess.run(
            [
                "gh",
                "release",
                "download",
                release["tag_name"],
                "--repo",
                request["repository"],
                "--dir",
                temporary,
                "--pattern",
                "*.json",
                "--pattern",
                "*.sha256",
            ],
            check=True,
        )
        for platform, prefix in prefixes.items():
            build = report["builds"][platform]
            archive = prefix + "." + platforms(request)[platform]
            require(assets[archive]["size"] == build["bytes"], "Archive byte size mismatch")
            manifest = json.loads((root / (prefix + ".json")).read_text())
            verify_manifest(manifest, request, platform, build, archive)
            require(
                (root / (prefix + ".sha256")).read_text().strip()
                == build["sha256"] + "  " + archive,
                "Checksum file mismatch",
            )
            digest = assets[archive].get("digest")
            if digest is None:
                subprocess.run(
                    [
                        "gh",
                        "release",
                        "download",
                        release["tag_name"],
                        "--repo",
                        request["repository"],
                        "--dir",
                        temporary,
                        "--pattern",
                        archive,
                    ],
                    check=True,
                )
                with (root / archive).open("rb") as incoming:
                    digest = "sha256:" + hashlib.file_digest(incoming, "sha256").hexdigest()
            require(digest == "sha256:" + build["sha256"], "Uploaded archive SHA-256 mismatch")
        for target, core in cores.items():
            prefix = prefixes[target] + "-core"
            archive = prefix + "." + targets[target]
            manifest = json.loads((root / (prefix + ".json")).read_text())
            verify_manifest(manifest, request, target, core, archive)
            require(manifest == core, "GPU core report mismatch")
            require(
                assets[archive]["size"] == core["bytes"]
                and assets[archive].get("digest") == "sha256:" + core["sha256"],
                "GPU core archive mismatch",
            )
            require(
                (root / (prefix + ".sha256")).read_text().strip()
                == core["sha256"] + "  " + archive,
                "GPU core checksum mismatch",
            )
            for item in core["gpu_runtime"]["files"]:
                asset = assets[item["asset"]]
                require(
                    asset["size"] == item["compressed_bytes"]
                    and asset.get("digest") == "sha256:" + item["compressed_sha256"],
                    "GPU library checksum or size mismatch",
                )
    return assets


def publish(request):
    repository = request["repository"]
    report, _ = read_report(repository, "latest.json")
    verify_build(report, request)
    run = api(f"repos/{repository}/actions/runs/{request['build_run_id']}")
    require(
        run["status"] == "completed"
        and run["conclusion"] == "success"
        and run["head_sha"] == request["build_commit"],
        "Build run not successful",
    )
    require(
        run["head_repository"]["full_name"] == repository
        and run["head_branch"] == "main"
        and run["path"].split("@", 1)[0] == ".github/workflows/builds.yml",
        "Wrong build workflow",
    )
    releases = api(f"repos/{repository}/releases?per_page=100")
    candidates = [
        item
        for item in releases
        if item["html_url"] == request["draft_url"] or item["tag_name"] == request["tag"]
    ]
    require(len(candidates) == 1, "Requested release must be unique")
    release = candidates[0]
    if release["draft"]:
        require(
            release["target_commitish"] == request["build_commit"],
            "Draft targets a different source",
        )
    else:
        require(
            release["tag_name"] == request["tag"] and not release["prerelease"],
            "Published release mismatch",
        )
    target = tag_commit(repository, request["tag"])
    require(
        target is None or target == request["release_commit"],
        "Release tag already targets another commit",
    )
    assets = verify_assets(release, report, request)
    fresh = api(f"repos/{repository}/releases/{release['id']}")
    require(
        asset_fingerprint(fresh) == asset_fingerprint(release)
        and fresh["draft"] == release["draft"],
        "Release changed during verification",
    )
    if release["draft"]:
        ensure_tag(repository, request["tag"], request["release_commit"])
        release = api(
            f"repos/{repository}/releases/{release['id']}",
            method="PATCH",
            payload={
                "tag_name": request["tag"],
                "target_commitish": request["release_commit"],
                "name": request["title"],
                "body": request["notes"],
                "draft": False,
                "prerelease": False,
                "make_latest": "true",
            },
        )
    require(
        not release["draft"] and release["tag_name"] == request["tag"],
        "Release publication not confirmed",
    )
    require(
        tag_commit(repository, request["tag"]) == request["release_commit"],
        "Published tag mismatch",
    )
    published_assets = {item["name"]: item for item in release["assets"]}
    require(
        set(published_assets) == set(assets)
        and asset_fingerprint(release) == asset_fingerprint(fresh),
        "Published assets mismatch",
    )
    result = {
        "state": "published",
        "tag": request["tag"],
        "release": release["html_url"],
        "release_commit": request["release_commit"],
        "build_commit": request["build_commit"],
        "archives_verified": len(platforms(request)),
        "publishing_run_id": os.environ["GITHUB_RUN_ID"],
    }
    # Publication has succeeded. A concurrent build/status update must never
    # turn that result into "failed" or be overwritten by an older report.
    for attempt in range(3):
        try:
            latest, sha = read_report(repository, "latest.json")
            if (
                latest is None
                or latest["run_id"] != request["build_run_id"]
                or latest["commit"] != request["build_commit"]
            ):
                break
            latest.update(
                draft=False,
                release=release["html_url"],
                published_tag=request["tag"],
                release_commit=request["release_commit"],
            )
            for platform, build in latest["builds"].items():
                extension = platforms(request)[platform]
                name = f"better-telegram-search-{request['tag'][1:]}-{platform}.{extension}"
                build["url"] = published_assets[name]["browser_download_url"]
            write_report(repository, "latest.json", latest, sha)
            break
        except Exception as exc:
            if "(HTTP 409)" in str(exc) and attempt < 2:
                continue
            result["report_warning"] = str(exc)[:1000]
            break
    return result


def main():
    request = load_request()
    repository = request["repository"]
    write_report(
        repository,
        "release.json",
        {
            "state": "publishing",
            "tag": request["tag"],
            "publishing_run_id": os.environ["GITHUB_RUN_ID"],
        },
    )
    try:
        result = publish(request)
    except Exception as exc:
        write_report(
            repository,
            "release.json",
            {
                "state": "failed",
                "tag": request["tag"],
                "error": str(exc)[:1500],
                "publishing_run_id": os.environ["GITHUB_RUN_ID"],
            },
        )
        raise
    try:
        write_report(repository, "release.json", result)
    except Exception as exc:
        result["report_warning"] = (
            result.get("report_warning", "") + " Publication journal unavailable: " + str(exc)
        )[:1500]
    print(json.dumps(result))


if __name__ == "__main__":
    main()
