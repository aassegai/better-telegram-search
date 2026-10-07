"""Edit explicitly requested published release bodies, preserving tags and assets."""

import hashlib
import json
import os
import re
from pathlib import Path

import publish_release as publisher


def load_request():
    publisher.require(os.environ["GITHUB_REF"] == "refs/heads/main", "Main required")
    version = os.environ.get("BTS_RELEASE_VERSION")
    if version:
        publisher.require(re.fullmatch(r"\d+\.\d+\.\d+", version), "Invalid version")
        path = Path(f".github/release-notes/v{version}.json")
    else:
        publisher.require(os.environ["GITHUB_EVENT_NAME"] == "push", "Explicit request required")
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        files = publisher.git(
            "diff", "--name-only", event["before"], event["after"], "--", ".github/release-notes"
        ).splitlines()
        publisher.require(1 <= len(files) <= 20, "Notes requests required")
        requests = [read_request(Path(name)) for name in files]
        return requests[0] if len(requests) == 1 else requests
    return read_request(path)


def read_request(path):
    publisher.require(
        re.fullmatch(r"\.github/release-notes/v\d+\.\d+\.\d+\.json", path.as_posix()),
        "Invalid request path",
    )
    publisher.require(
        not path.is_symlink() and path.resolve().is_relative_to(Path.cwd().resolve()),
        "Request must be local",
    )
    request = json.loads(path.read_text(encoding="utf-8"))
    publisher.require(request["tag"] == path.stem, "Tag mismatch")
    publisher.require(
        request["repository"] == os.environ["GITHUB_REPOSITORY"], "Repository mismatch"
    )
    publisher.require(
        re.fullmatch(r"[0-9a-f]{40}", request["tag_commit"]), "Full commit SHA required"
    )
    publisher.require(
        request["operation"] in {"remove_section", "replace_body"}, "Unsupported operation"
    )
    if request["operation"] == "remove_section":
        publisher.require(re.fullmatch(r"#{1,6} [^\r\n]+", request["heading"]), "Invalid heading")
    else:
        publisher.require(
            re.fullmatch(r"[a-f0-9]{64}", request["expected_body_sha256"]), "Body checksum required"
        )
        publisher.require(
            isinstance(request["body"], str) and 0 < len(request["body"]) <= 20_000, "Invalid body"
        )
    return request


def remove_section(body, heading):
    """Keep surrounding sections and ignore heading-like lines in fenced code."""
    lines = body.splitlines(keepends=True)
    level = len(heading.split(" ", 1)[0])
    start = end = None
    fence = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        marker = re.match(r"^(`{3,}|~{3,})", stripped)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        if stripped == heading and start is None:
            start = index
        elif start is not None:
            next_heading = re.match(r"^(#{1,6})\s+", stripped)
            if next_heading and len(next_heading[1]) <= level:
                end = index
                break
    if start is None:
        return body
    return "".join(lines[:start] + lines[end if end is not None else len(lines) :])


def update_notes(request):
    repository, tag = request["repository"], request["tag"]
    publisher.require(
        publisher.tag_commit(repository, tag) == request["tag_commit"], "Existing tag mismatch"
    )
    release = publisher.api(f"repos/{repository}/releases/tags/{tag}")
    publisher.require(
        release["tag_name"] == tag and release["draft"] is False, "Published release required"
    )
    endpoint = f"repos/{repository}/releases/{release['id']}"
    original = release.get("body") or ""
    if request["operation"] == "replace_body":
        updated = request["body"]
        publisher.require(
            original == updated
            or hashlib.sha256(original.encode()).hexdigest() == request["expected_body_sha256"],
            "Published notes changed since this request was prepared",
        )
    else:
        updated = remove_section(original, request["heading"])
    if updated != original:
        fresh = publisher.api(endpoint)
        publisher.require(
            fresh.get("body") == release.get("body")
            and fresh["tag_name"] == tag
            and fresh["draft"] is False,
            "Release changed during notes verification",
        )
        publisher.api(endpoint, method="PATCH", payload={"body": updated})
    confirmed = publisher.api(endpoint)
    publisher.require(
        confirmed.get("body", "") == updated
        and confirmed["tag_name"] == tag
        and confirmed["draft"] is False,
        "Notes update not confirmed",
    )
    publisher.require(
        publisher.tag_commit(repository, tag) == request["tag_commit"], "Tag changed during update"
    )
    return {
        "state": "updated",
        "tag": tag,
        "url": confirmed["html_url"],
        "operation": request["operation"],
        "body_sha256": hashlib.sha256(updated.encode()).hexdigest(),
        "run_id": os.environ["GITHUB_RUN_ID"],
    }


def main():
    request = load_request()
    if isinstance(request, list):
        results = [update_notes(item) for item in request]
        report = {"state": "updated", "releases": results}
        try:
            publisher.write_report(request[0]["repository"], "release-notes.json", report)
        except Exception as error:
            report["report_warning"] = str(error)
        print(json.dumps(report))
        return
    try:
        result = update_notes(request)
    except Exception as error:
        try:
            publisher.write_report(
                request["repository"],
                "release-notes.json",
                {"state": "failed", "tag": request["tag"], "error": str(error)},
            )
        except Exception:
            pass
        raise
    try:
        publisher.write_report(request["repository"], "release-notes.json", result)
    except Exception as error:
        result["report_warning"] = str(error)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
