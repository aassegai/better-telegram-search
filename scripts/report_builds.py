"""CI-only publication of build metadata to a separate branch, readable via Git SSH."""

import base64
import json
import os
import subprocess
from pathlib import Path


def gh(*args, payload=None):
    command = ["gh", *args]
    if payload is not None:
        command += ["--input", "-"]
    result = subprocess.run(
        command,
        input=json.dumps(payload) if payload is not None else None,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


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
    content = subprocess.run(
        ["gh", "api", f"repos/{repository}/contents/latest.json?ref={branch}"],
        capture_output=True,
        text=True,
    )
    payload = {
        "message": f"Build report for {os.environ['GITHUB_SHA'][:12]}",
        "branch": branch,
        "content": base64.b64encode(json.dumps(result, indent=2).encode()).decode(),
    }
    if content.returncode == 0:
        payload["sha"] = json.loads(content.stdout)["sha"]
    gh("api", "--method", "PUT", f"repos/{repository}/contents/latest.json", payload=payload)
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as out:
        out.write(
            f"Native CPU/GPU builds: {result['result']}\n\n"
            f"[Unpublished release]({release['url']})\n\n"
        )
        for name, report in sorted(builds.items()):
            out.write(f"- {name}: {report['state']}\n")


if __name__ == "__main__":
    main()
