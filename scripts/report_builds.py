"""CI-only publication of build metadata to a separate branch, readable via Git SSH."""

import base64
import json
import os
import subprocess
import time
from pathlib import Path


def gh(*args, payload=None):
    command = ["gh", *args]
    if payload is not None:
        command += ["--input", "-"]
    try:
        result = subprocess.run(
            command,
            input=json.dumps(payload) if payload is not None else None,
            text=True,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("GitHub command failed: " + (exc.stderr or "")[:1000]) from None
    return json.loads(result.stdout) if result.stdout.strip() else None


def write_report(repository, result):
    endpoint = f"repos/{repository}/contents/latest.json"
    for attempt in range(3):
        content = subprocess.run(
            ["gh", "api", endpoint + "?ref=build-status"], capture_output=True, text=True
        )
        payload = {
            "message": f"Build report for {result['commit'][:12]}",
            "branch": "build-status",
            "content": base64.b64encode(json.dumps(result, indent=2).encode()).decode(),
        }
        if content.returncode == 0:
            current = json.loads(content.stdout)
            previous = json.loads(base64.b64decode(current["content"]))
            if int(previous["run_id"]) > int(result["run_id"]):
                return False
            payload["sha"] = current["sha"]
        elif "(HTTP 404)" not in content.stderr:
            raise RuntimeError("Cannot read build report: " + content.stderr[:1000])
        try:
            gh("api", "--method", "PUT", endpoint, payload=payload)
            return True
        except RuntimeError as exc:
            if "(HTTP 409)" not in str(exc) or attempt == 2:
                raise
            time.sleep(0.25 * 2**attempt)


def main():
    repository = os.environ["GITHUB_REPOSITORY"]
    tag = os.environ["BTS_RELEASE_TAG"]
    release = gh("release", "view", tag, "--json", "assets,url,isDraft")
    assets = {item["name"]: item["url"] for item in release["assets"]}
    expected = {
        "linux-x86_64",
        "linux-arm64",
        "win32-x86_64",
        "darwin-x86_64",
        "darwin-arm64",
        "linux-x86_64-gpu",
        "win32-x86_64-gpu",
    }
    builds = {}
    cores = {}
    for path in Path("build-reports").glob("*.json"):
        report = json.loads(path.read_text())
        name = (
            report["platform"]
            + "-"
            + report["arch"]
            + ("-gpu" if report.get("variant") == "gpu" else "")
        )
        if "-core." in report["artifact"]:
            cores[name] = report
            continue
        builds[name] = {
            "state": "ready",
            "sha256": report["sha256"],
            "bytes": report["bytes"],
            "checks": report["checks"],
            "url": assets[report["artifact"]],
        }
    for name, core in cores.items():
        builds[name]["core"] = core
    for name in expected - builds.keys():
        builds[name] = {
            "state": "pending" if os.environ["BTS_BUILD_RESULT"] == "pending" else "failed",
            "url": None,
        }
    jobs = gh(
        "api", f"repos/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}/jobs?per_page=100"
    )
    result = {
        "commit": os.environ["GITHUB_SHA"],
        "run_id": os.environ["GITHUB_RUN_ID"],
        "result": os.environ["BTS_BUILD_RESULT"],
        "draft": release["isDraft"],
        "release": release["url"],
        "builds": builds,
        "jobs": [
            {
                "name": job["name"],
                "conclusion": job["conclusion"],
                "url": job["html_url"],
                "steps": [
                    {"name": step["name"], "conclusion": step["conclusion"]}
                    for step in job.get("steps", [])
                ],
            }
            for job in jobs["jobs"]
        ],
        "workflow": f"https://github.com/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
    }
    failures = {}
    for job in jobs["jobs"]:
        if job["conclusion"] != "failure":
            continue
        logs = subprocess.run(
            ["gh", "api", f"repos/{repository}/actions/jobs/{job['id']}/logs"],
            text=True,
            capture_output=True,
        )
        if logs.returncode == 0:
            failures[job["name"]] = logs.stdout.splitlines()[-60:]
    result["failures"] = failures
    branch = "build-status"
    lookup = subprocess.run(
        ["gh", "api", f"repos/{repository}/git/ref/heads/{branch}"], capture_output=True, text=True
    )
    if lookup.returncode:
        gh(
            "api",
            "--method",
            "POST",
            f"repos/{repository}/git/refs",
            payload={"ref": f"refs/heads/{branch}", "sha": os.environ["GITHUB_SHA"]},
        )
    write_report(repository, result)
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as out:
        out.write(
            f"Native CPU/GPU builds: {result['result']}\n\n"
            f"[Unpublished release]({release['url']})\n\n"
        )
        for name, report in sorted(builds.items()):
            out.write(f"- {name}: {report['state']}\n")


if __name__ == "__main__":
    main()
