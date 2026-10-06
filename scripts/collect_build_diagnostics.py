"""Fetch job logs after run completion; GitHub can reject logs while a run is active.

CI runs on public synthetic inputs only. Logs are already masked by GitHub Actions.
The metadata branch makes diagnostics accessible with Git SSH without a local API token.
"""

import base64
import json
import os
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
    endpoint = f"repos/{repository}/contents/latest.json"
    source = gh("api", endpoint + "?ref=build-status")
    report = json.loads(base64.b64decode(source["content"]))
    expected = os.environ.get("BTS_COMPLETED_RUN_ID")
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
            ["gh", "api", f"repos/{repository}/actions/jobs/{job['id']}/logs"],
            text=True,
            capture_output=True,
        )
        failures[job["name"]] = (
            logs.stdout.splitlines()[-120:]
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


if __name__ == "__main__":
    main()
