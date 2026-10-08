"""Model-asset publication uses public synthetic files and never contacts GitHub."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

module_spec = importlib.util.spec_from_file_location(
    "model_publication", Path(__file__).parents[1] / "tools/model-export/publish.py"
)
publisher = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(publisher)


@pytest.fixture
def publication(tmp_path, monkeypatch):
    recipe = tmp_path / "tools/model-export"
    recipe.mkdir(parents=True)
    monkeypatch.setattr(publisher, "__file__", str(recipe / "publish.py"))
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    request_dir = tmp_path / ".github/model-bundles"
    request_dir.mkdir(parents=True)
    (request_dir / "v1.json").write_text(
        json.dumps({"tag": "model-bundles-v1", "models": ["berta", "giga"]})
    )
    configs = tmp_path / "src/telegram_search/config"
    configs.mkdir(parents=True)
    artifacts = tmp_path / "synthetic-assets"
    artifacts.mkdir()
    specs, reports = {}, {}
    for key in ("berta", "giga"):
        payload = f"public synthetic {key} graph".encode()
        (artifacts / f"{key}-fp16.onnx").write_bytes(payload)
        checksum = hashlib.sha256(payload).hexdigest()
        specs[key] = {
            "files": [{"bytes": len(payload), "sha256": checksum}],
            "revision": "b" * 40,
            "precision": "mixed-fp16",
            "validated_runtime_versions": ["1.30.0"],
        }
        reports[key] = {
            "model": key,
            "sha256": checksum,
            "bytes": len(payload),
            "source": "b" * 40,
            "provider": "CPUExecutionProvider",
            "precision": "mixed-fp16",
            "onnxruntime": "1.30.0",
            "probes": [
                {"batch": batch, "length": length, "max_abs": 0.0002, "min_cosine": 0.999999}
                for batch, length in ((3, 20), (1, 20), (2, 70), (1, 512))
            ],
        }
        (artifacts / f"{key}-validation.json").write_text(json.dumps(reports[key]))
    (configs / "models.json").write_text(json.dumps({"berta": specs["berta"]}))
    (configs / "rerank_model.json").write_text(json.dumps(specs["giga"]))
    remote, calls = {}, []
    release = {"exists": False}

    def github(command, **kwargs):
        assert command[0] == "gh"
        calls.append(command)
        action = command[2]
        if action == "view":
            return SimpleNamespace(
                returncode=0 if release["exists"] else 1,
                stdout=json.dumps(
                    {"isPrerelease": True, "assets": [{"name": name} for name in remote]}
                ),
            )
        if action == "create":
            assert "--prerelease" in command and command[3] == "model-bundles-v1"
            release["exists"] = True
        elif action == "upload":
            path = Path(command[4])
            assert path.name not in remote
            remote[path.name] = path.read_bytes()
        elif action == "download":
            name = command[command.index("--pattern") + 1]
            (Path(command[command.index("--dir") + 1]) / name).write_bytes(remote[name])
        else:
            raise AssertionError(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(publisher.subprocess, "run", github)
    return artifacts, specs, reports, remote, calls


def test_models_publish_verified_prerelease_and_retry_without_overwriting(publication):
    artifacts, _, _, remote, calls = publication
    publisher.publish(artifacts)
    assert len(remote) == 4
    publisher.publish(artifacts)
    assert sum(command[2] == "create" for command in calls) == 1
    assert sum(command[2] == "upload" for command in calls) == 4
    assert sum(command[2] == "download" for command in calls) == 4
    remote["berta-fp16.onnx"] = b"changed graph"
    with pytest.raises(RuntimeError, match="existing model asset differs"):
        publisher.publish(artifacts)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_abs", float("nan")),
        ("max_abs", float("inf")),
        ("max_abs", -0.1),
        ("max_abs", True),
        ("max_abs", "0.0001"),
        ("max_abs", None),
        ("max_abs", 0.1),
        ("min_cosine", float("nan")),
        ("min_cosine", float("inf")),
        ("min_cosine", 1.0001),
        ("min_cosine", 0.5),
        ("min_cosine", True),
        ("min_cosine", None),
        ("length", 513),
        ("batch", False),
    ],
)
def test_bad_parity_reports_block_all_github_operations(publication, field, value):
    artifacts, _, reports, _, calls = publication
    report = copy.deepcopy(reports["giga"])
    report["probes"][0][field] = value
    (artifacts / "giga-validation.json").write_text(json.dumps(report))
    with pytest.raises(RuntimeError):
        publisher.publish(artifacts)
    assert calls == []


@pytest.mark.parametrize("damage", ["missing_metric", "missing_probe", "short_only", "hash"])
def test_incomplete_checks_and_corrupt_models_block_publication(publication, damage):
    artifacts, _, reports, _, calls = publication
    report = copy.deepcopy(reports["berta"])
    if damage == "missing_metric":
        del report["probes"][0]["max_abs"]
    elif damage == "missing_probe":
        report["probes"].pop()
    elif damage == "short_only":
        report["probes"][-1]["length"] = 20
    else:
        (artifacts / "berta-fp16.onnx").write_bytes(b"modified graph")
    (artifacts / "berta-validation.json").write_text(json.dumps(report))
    with pytest.raises(RuntimeError):
        publisher.publish(artifacts)
    assert calls == []
