"""Publish only checksum-verified outputs from the CPU export jobs."""

import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_report(report, key, spec):
    expected = spec["files"][0]
    if (
        not isinstance(report, dict)
        or report.get("model") != key
        or report.get("sha256") != expected["sha256"]
        or type(report.get("bytes")) is not int
        or report["bytes"] != expected["bytes"]
        or report.get("source") != spec["revision"]
        or report.get("provider") != "CPUExecutionProvider"
        or report.get("precision") != spec["precision"]
        or report.get("onnxruntime") not in spec["validated_runtime_versions"]
        or not isinstance(report.get("probes"), list)
        or len(report["probes"]) != 4
    ):
        raise RuntimeError("Model validation report mismatch")
    for probe in report["probes"]:
        if not isinstance(probe, dict):
            raise RuntimeError("Model validation probe mismatch")
        for field, lower, upper in (("min_cosine", 0.9999, 1), ("max_abs", 0, 0.003)):
            value = probe.get(field)
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not lower <= value <= upper
            ):
                raise RuntimeError("Model reference parity failed")
        if (
            type(probe.get("batch")) is not int
            or not 1 <= probe["batch"] <= 3
            or type(probe.get("length")) is not int
            or not 1 <= probe["length"] <= 512
        ):
            raise RuntimeError("Model validation probe mismatch")
    if {probe["batch"] for probe in report["probes"]} != {1, 2, 3} or not any(
        probe["length"] == 512 for probe in report["probes"]
    ):
        raise RuntimeError("Variable batch and maximum length probes are required")


def verify(artifacts, specs):
    verified = []
    for key, spec in specs.items():
        path = artifacts / f"{key}-fp16.onnx"
        expected = spec["files"][0]
        if (
            path.is_symlink()
            or path.stat().st_size != expected["bytes"]
            or checksum(path) != expected["sha256"]
        ):
            raise RuntimeError("Model asset checksum mismatch")
        report_path = artifacts / f"{key}-validation.json"
        report = json.loads(report_path.read_text())
        validate_report(report, key, spec)
        verified.extend([path, report_path])
    return verified


def gh(*args, **kwargs):
    return subprocess.run(["gh", *args], check=True, **kwargs)


def publish(artifacts):
    repo = Path(__file__).resolve().parents[2]
    request = json.loads((repo / ".github/model-bundles/v1.json").read_text())
    tag = request["tag"]
    if tag != "model-bundles-v1" or request["models"] != ["berta", "giga"]:
        raise RuntimeError("Unrecognized model publication request")
    config = repo / "src/telegram_search/config"
    specs = {
        "berta": json.loads((config / "models.json").read_text())["berta"],
        "giga": json.loads((config / "rerank_model.json").read_text()),
    }
    verified = verify(artifacts, specs)
    existing = subprocess.run(
        ["gh", "release", "view", tag, "--json", "assets,isPrerelease"],
        capture_output=True,
        text=True,
    )
    if existing.returncode:
        gh(
            "release",
            "create",
            tag,
            "--prerelease",
            "--title",
            "ONNX model bundles v1",
            "--target",
            os.environ["GITHUB_SHA"],
            "--notes-file",
            str(repo / "tools/model-export/RELEASE_NOTES.md"),
        )
        names = set()
    else:
        release = json.loads(existing.stdout)
        if not release["isPrerelease"]:
            raise RuntimeError("Model assets must remain a prerelease")
        names = {item["name"] for item in release["assets"]}
    for path in verified:
        if path.name in names:
            # Immutable exports: retries may reuse identical assets, never overwrite another graph.
            with tempfile.TemporaryDirectory() as temporary:
                gh("release", "download", tag, "--pattern", path.name, "--dir", temporary)
                if checksum(Path(temporary) / path.name) != checksum(path):
                    raise RuntimeError("An existing model asset differs; use a new bundle version")
        else:
            gh("release", "upload", tag, str(path))


if __name__ == "__main__":
    publish(Path(sys.argv[1]))
