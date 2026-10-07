"""Fetch job logs after run completion; GitHub can reject logs while a run is active.

CI runs on public synthetic inputs only. Logs are already masked by GitHub Actions.
The metadata branch makes diagnostics accessible with Git SSH without a local API token.
"""

import base64
import json
import os
import re
import subprocess


def gh(*args, payload=None):
    command = ["gh", *args]
    if payload is not None:
        command += ["--input", "-"]
    result = subprocess.run(
        command,
        input=json.dumps(payload) if payload else None,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def main():
    repository = os.environ["GITHUB_REPOSITORY"]
    expected = os.environ.get("BTS_COMPLETED_RUN_ID")
    if expected:
        completed_run = gh("api", f"repos/{repository}/actions/runs/{expected}")
        if completed_run["path"].split("@", 1)[0] == ".github/workflows/checks.yml":
            collect_checks(repository, completed_run)
            return
    else:
        recent = gh("api", f"repos/{repository}/actions/workflows/checks.yml/runs?per_page=5")
        for completed_run in recent["workflow_runs"]:
            if completed_run["status"] == "completed" and completed_run["head_branch"] == "main":
                collect_checks(repository, completed_run)
                break
    endpoint = f"repos/{repository}/contents/latest.json"
    source = gh("api", endpoint + "?ref=build-status")
    report = json.loads(base64.b64decode(source["content"]))
    if expected and str(expected) != report["run_id"]:
        print("A newer build report exists; older diagnostics skipped.")
        return
    run = gh("api", f"repos/{repository}/actions/runs/{report['run_id']}")
    if run["status"] != "completed":
        print("Build run is still active; completed diagnostics skipped.")
        return
    if (
        run["head_branch"] != "main"
        or run["head_repository"]["full_name"] != repository
        or run["path"].split("@", 1)[0] != ".github/workflows/builds.yml"
    ):
        raise RuntimeError("Diagnostics require this repository's main native-build workflow")
    jobs = gh("api", f"repos/{repository}/actions/runs/{report['run_id']}/jobs?per_page=100")
    failures = {}
    complete = True
    for job in jobs["jobs"]:
        if job["conclusion"] != "failure":
            continue
        logs = subprocess.run(
            [
                "gh",
                "api",
                "--allow-escape-sequences",
                f"repos/{repository}/actions/jobs/{job['id']}/logs",
            ],
            text=True,
            capture_output=True,
        )
        safe_logs = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", logs.stdout)
        safe_logs = "".join(ch for ch in safe_logs if ch in "\n\t" or 32 <= ord(ch) != 127)
        failures[job["name"]] = (
            safe_logs.splitlines()[-120:]
            if logs.returncode == 0
            else ["GitHub log API: " + logs.stderr[:1000]]
        )
        if logs.returncode:
            complete = False
    report["failures"] = failures
    report["diagnostics_complete"] = complete
    # Avoid overwriting a report that moved to a newer run during the requests.
    latest = gh("api", endpoint + "?ref=build-status")
    if latest["sha"] != source["sha"]:
        print("Build report changed during diagnostics; this update skipped.")
        return
    gh(
        "api",
        "--method",
        "PUT",
        endpoint,
        payload={
            "branch": "build-status",
            "sha": source["sha"],
            "message": f"Completed diagnostics for run {report['run_id']}",
            "content": base64.b64encode(json.dumps(report, indent=2).encode()).decode(),
        },
    )
    print(json.dumps({"run_id": report["run_id"], "failed_jobs": len(failures)}))


def collect_checks(repository, run):
    if (
        run["status"] != "completed"
        or run["head_branch"] != "main"
        or run["head_repository"]["full_name"] != repository
    ):
        raise RuntimeError("Diagnostics require completed checks on this repository's main branch")
    jobs = gh("api", f"repos/{repository}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
    failures = {}
    for job in jobs:
        if job["conclusion"] != "failure":
            continue
        logs = subprocess.run(
            [
                "gh",
                "api",
                "--allow-escape-sequences",
                f"repos/{repository}/actions/jobs/{job['id']}/logs",
            ],
            text=True,
            capture_output=True,
        )
        safe_logs = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", logs.stdout)
        safe_logs = "".join(ch for ch in safe_logs if ch in "\n\t" or 32 <= ord(ch) != 127)
        failures[job["name"]] = (
            safe_logs.splitlines()[-160:]
            if logs.returncode == 0
            else ["GitHub log API unavailable"]
        )
    report = {
        "run_id": str(run["id"]),
        "commit": run["head_sha"],
        "result": run["conclusion"],
        "failures": failures,
    }
    endpoint = f"repos/{repository}/contents/checks-{run['head_sha']}.json"
    existing = subprocess.run(
        ["gh", "api", endpoint + "?ref=build-status"], text=True, capture_output=True
    )
    payload = {
        "branch": "build-status",
        "message": f"Checks diagnostics for {run['head_sha'][:12]}",
        "content": base64.b64encode(json.dumps(report, indent=2).encode()).decode(),
    }
    if existing.returncode == 0:
        payload["sha"] = json.loads(existing.stdout)["sha"]
    gh("api", "--method", "PUT", endpoint, payload=payload)
    print(json.dumps({"run_id": report["run_id"], "failed_jobs": len(failures)}))


if __name__ == "__main__":
    main()
