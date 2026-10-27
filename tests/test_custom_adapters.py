"""Public local-code fixtures exercise real isolated workers, never model downloads."""

import copy
import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from telegram_search.model_adapters.client import AdapterWorker
from telegram_search.model_adapters.contracts import validate_result
from telegram_search.model_adapters.manifest import AdapterManifest
from telegram_search.model_adapters.store import AdapterStore
from telegram_search.model_adapters.validation import (
    inspect_adapter,
    smoke_requests,
    validate_adapter,
)
from telegram_search.shared.errors import UserError

CODE = """
import os, time
ROLE = {role!r}
OPERATIONS = {{'text_embedding':['count_tokens','encode_text'],
    'image_embedding':['encode_images','encode_image_queries'],
    'reranker':['rerank'],'ocr':['recognize'],'asr':['transcribe']}}
print('PRIVATE_ADAPTER_OUTPUT_DO_NOT_LOG')
class Adapter:
    def info(self):
        return {{'adapter_api_version':1,'role':ROLE,'devices':['cpu'],
                 'operations':OPERATIONS[ROLE]}}
    def validate(self, context):
        assert 'BTS_TELEGRAM_API_HASH' not in os.environ
        assert os.environ['HF_HUB_OFFLINE'] == '1' and context.offline
    def load(self, context): pass
    def unload(self, context): pass
    def close(self, context): pass
    def count_tokens(self, texts, context): return [len(t.split())+2 for t in texts]
    def encode_text(self, items, operation, context):
        context.progress(0, len(items))
        if items[0]['text'] == 'HANG': time.sleep(60)
        if items[0]['text'] == 'ERROR': raise RuntimeError('PRIVATE_INPUT_AND_PATH')
        if 'OOM_TRIGGER' in items[0]['text'] and len(items) > 1: raise MemoryError()
        if items[0]['text'] == 'ORT_OOM':
            raise RuntimeError('Available memory of 100 is smaller than requested bytes of 200')
        rows = []
        for i in items:
            rows.append([0,1,0,0] if 'train' in i['text'].lower() else [1,0,0,0])
        return {{'ids':[i['id'] for i in items], 'dtype':'float32','vectors':rows}}
    def encode_images(self, images, context):
        assert isinstance(images[0]['pixels'], bytes)
        return {{'ids':[i['id'] for i in images], 'dtype':'float32',
                 'vectors':[[1,0,0,0] for i in images]}}
    def encode_image_queries(self, items, context): return self.encode_text(items, 'query', context)
    def rerank(self, query, candidates, context):
        return [{{'candidate_id':i['candidate_id'],'score':1.0}} for i in reversed(candidates)]
    def recognize(self, images, context):
        return [{{'id':i['id'],'text':'','regions':[]}} for i in images]
    def transcribe(self, audio, language, context):
        assert audio['pcm'].typecode == 'f' and len(audio['pcm']) == audio['samples']
        return {{'id':audio['id'],'text':'','language':'und','segments':[]}}
"""


def package(tmp_path, role="text_embedding"):
    root = tmp_path / "public-package"
    root.mkdir(exist_ok=True)
    code = CODE.format(role=role).encode()
    (root / "adapter.py").write_bytes(code)
    data = {
        "schema_version": 1,
        "adapter_api_version": 1,
        "id": "public-example",
        "role": role,
        "entrypoint": "adapter.py:Adapter",
        "revision": "test-v1",
        "supported_languages": ["ru", "en"],
        "runtime": {"name": "python", "version": "3"},
        "devices": ["cpu"],
        "dependencies": {},
        "input_contract": {
            "max_batch": 4,
            "query_max_tokens": 512,
            "document_max_tokens": 480,
            "sample_rate": 16000,
        },
        "output_contract": {
            "dimension": 4,
            "dtype": "float32",
            "normalization": "l2",
            "metric": "cosine",
            "higher_is_better": True,
            "bbox": "pixel-xyxy",
            "confidence_range": [0, 1],
        },
        "preprocessing": {"version": "public-test-v1", "query_prefix": "", "document_prefix": ""},
        "network": False,
        "files": [
            {"path": "adapter.py", "bytes": len(code), "sha256": hashlib.sha256(code).hexdigest()}
        ],
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, data


@pytest.mark.parametrize("role", ["text_embedding", "image_embedding", "reranker", "ocr", "asr"])
def test_real_workers_validate_all_roles_without_app_imports(tmp_path, role, monkeypatch, capfd):
    monkeypatch.setenv("BTS_TELEGRAM_API_HASH", "DO_NOT_PASS_TO_WORKER")
    path, _ = package(tmp_path, role)
    store = AdapterStore(tmp_path / "workspace")
    before = set(path.parent.iterdir())
    assert inspect_adapter(path)["code_executed"] is False
    assert set(path.parent.iterdir()) == before
    events = []
    result = validate_adapter(store, path, sys.executable, register=True, on_progress=events.append)
    assert result["state"] == "passed" and result["registered"]
    manifest, snapshot, registered = store.get(result["identity"])
    assert snapshot != path.parent and manifest.role == role
    assert registered["environment"]["dependencies"] == {}
    assert result["smoke"]["quality_evaluation"] is False
    assert not list(store.root.glob(".checking-*"))
    assert "PRIVATE" not in str(capfd.readouterr())
    if role == "text_embedding":
        assert len([event for event in events if event["phase"] == "inference"]) == 2
    assert not (tmp_path / "workspace" / "archive.sqlite").exists()


def test_inspection_never_executes_even_an_invalid_python_module(tmp_path):
    path, data = package(tmp_path)
    code = b"raise RuntimeError('must not run during inspection')\n"
    (path.parent / "adapter.py").write_bytes(code)
    data["files"][0].update(bytes=len(code), sha256=hashlib.sha256(code).hexdigest())
    path.write_text(json.dumps(data))
    assert inspect_adapter(path)["state"] == "inspected"
    with pytest.raises(UserError, match="worker failed"):
        validate_adapter(AdapterStore(tmp_path / "scratch"), path, sys.executable)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("adapter_api_version", 2),
        ("role", "unknown"),
        ("network", True),
        ("entrypoint", "https://example.com/adapter.py:Adapter"),
        ("entrypoint", "../adapter.py:Adapter"),
        ("devices", ["cpu", "cpu"]),
        ("dependencies", ["numpy"]),
    ],
)
def test_bad_manifests_reject_before_starting_a_worker(tmp_path, field, value):
    path, data = package(tmp_path)
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(UserError):
        inspect_adapter(path)


@pytest.mark.parametrize(
    "name",
    [
        "../adapter.py",
        "/adapter.py",
        "C:/adapter.py",
        "CON.py",
        "a./adapter.py",
        "x\\adapter.py",
        "MANIFEST.JSON",
    ],
)
def test_portable_boundaries_and_reserved_names(tmp_path, name):
    _, data = package(tmp_path)
    data["files"][0]["path"] = name
    with pytest.raises(UserError):
        AdapterManifest.parse(data)


def test_checksum_failure_and_extra_unpinned_code_are_not_imported(tmp_path):
    path, _ = package(tmp_path)
    (path.parent / "sitecustomize.py").write_text("raise RuntimeError('unpinned')")
    store = AdapterStore(tmp_path / "workspace")
    manifest, stage = store.stage(path)
    assert not (stage / "sitecustomize.py").exists()
    (stage / "adapter.py").write_text("changed bytes")
    with pytest.raises(UserError, match="checksum"):
        AdapterWorker(manifest, stage, sys.executable, store.runner())
    (path.parent / "adapter.py").write_text("changed bytes")
    with pytest.raises(UserError, match="checksum"):
        inspect_adapter(path)


def test_model_error_stops_only_its_worker_and_has_no_private_details(tmp_path):
    path, _ = package(tmp_path)
    store = AdapterStore(tmp_path / "workspace")
    manifest, stage = store.stage(path)
    with (
        AdapterWorker(manifest, stage, sys.executable, store.runner()) as first,
        AdapterWorker(manifest, stage, sys.executable, store.runner()) as second,
    ):
        first.request("load")
        second.request("load")
        with pytest.raises(UserError) as error:
            first.request(
                "encode_text", {"items": [{"id": "one", "text": "ERROR"}], "operation": "query"}
            )
        assert "PRIVATE" not in str(error.value)
        assert "Adapter failure: runtime_error" in error.value.__notes__
        assert "Adapter operation: encode_text" in error.value.__notes__
        assert "PRIVATE" not in str(error.value.__notes__)
        assert first.process.poll() is not None
        assert second.request("count_tokens", {"texts": ["still alive"]}) == [4]


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancel_terminate_a_noncooperative_call(tmp_path, cancel):
    path, _ = package(tmp_path)
    store = AdapterStore(tmp_path / "workspace")
    manifest, stage = store.stage(path)
    event = threading.Event()
    with AdapterWorker(manifest, stage, sys.executable, store.runner()) as worker:
        worker.request("load")
        timer = threading.Timer(0.2, event.set)
        if cancel:
            timer.start()
        start = time.monotonic()
        try:
            with pytest.raises(InterruptedError if cancel else UserError) as failure:
                worker.request(
                    "encode_text",
                    {"items": [{"id": "one", "text": "HANG"}], "operation": "query"},
                    timeout=10 if cancel else 0.2,
                    cancelled=event,
                )
            assert failure.value.__notes__ == [
                "Adapter startup: adapter_ready",
                "Adapter operation: encode_text",
            ]
        finally:
            if cancel:
                timer.join()
        assert time.monotonic() - start < 5
        assert worker.process.poll() is not None


@pytest.mark.parametrize(
    "event",
    [
        {"event": "startup", "phase": "PRIVATE_UNTRUSTED_STAGE"},
        {"event": "startup", "phase": []},
        {"event": "startup", "phase": "worker_started", "payload": "PRIVATE_UNTRUSTED_STAGE"},
    ],
)
def test_startup_diagnostics_reject_unknown_or_arbitrary_worker_output(tmp_path, event):
    path, _ = package(tmp_path)
    store = AdapterStore(tmp_path / "workspace")
    manifest, stage = store.stage(path)
    with AdapterWorker(manifest, stage, sys.executable, store.runner()) as worker:
        worker.request("load")
        assert worker.startup_phase == "adapter_ready"
        worker.events.put(("message", event))
        with pytest.raises(UserError, match="invalid response") as failure:
            worker.request("count_tokens", {"texts": ["public"]})
        assert "PRIVATE" not in str(failure.value)
        assert failure.value.__notes__ == [
            "Adapter startup: adapter_ready",
            "Adapter operation: count_tokens",
        ]
        assert worker.process.poll() is not None


def test_output_shape_ids_and_nonfinite_vectors_cannot_be_published(tmp_path):
    path, _ = package(tmp_path)
    manifest = AdapterManifest.read(path)
    operation, payload = list(smoke_requests(manifest))[1]
    valid = {
        "ids": [i["id"] for i in payload["items"]],
        "dtype": "float32",
        "vectors": [[1, 0, 0, 0]] * 2,
    }
    for mutation in ["reverse", "nan", "dtype", "zero", "shape", "overflow"]:
        bad = copy.deepcopy(valid)
        if mutation == "reverse":
            bad["ids"].reverse()
        elif mutation == "nan":
            bad["vectors"][0][0] = float("nan")
        elif mutation == "dtype":
            bad["dtype"] = "float64"
        elif mutation == "zero":
            bad["vectors"][0] = [0] * 4
        elif mutation == "overflow":
            bad["vectors"][0][0] = 1e38
        else:
            bad["vectors"][0].pop()
        with pytest.raises(UserError):
            validate_result(manifest, operation, payload, bad)


def test_cli_validation_registers_without_opening_an_archive_database(tmp_path):
    path, _ = package(tmp_path)
    workspace = tmp_path / "cli-workspace"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "telegram_search.cli",
            "--workspace",
            str(workspace),
            "adapters",
            "validate",
            str(path),
            "--python",
            sys.executable,
            "--register",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(result.stdout)["registered"] is True
    assert not list(workspace.glob("*.sqlite*"))
    assert not list(workspace.glob("*.db*"))


def test_worker_cleanup_is_idempotent_after_process_exit(tmp_path, monkeypatch):
    import os

    path, _ = package(tmp_path)
    store = AdapterStore(tmp_path / "workspace")
    manifest, stage = store.stage(path)
    worker = AdapterWorker(manifest, stage, sys.executable, store.runner())
    worker.request("load")
    worker.close()
    if os.name != "nt":
        monkeypatch.setattr(os, "killpg", lambda *_args: pytest.fail("stale group signalled"))
    worker.close()
    worker.close()


def test_git_workspace_must_be_ignored_before_adapter_files_are_written(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    path, _ = package(tmp_path)
    workspace = repo / "not-ignored"
    assert inspect_adapter(path)["state"] == "inspected"
    assert AdapterStore(workspace).registrations() == {}
    with pytest.raises(UserError, match="Git"):
        validate_adapter(AdapterStore(workspace), path, sys.executable, register=True)
    assert not workspace.exists()
    (repo / ".gitignore").write_text("/not-ignored/\n")
    assert validate_adapter(AdapterStore(workspace), path, sys.executable, register=True)[
        "registered"
    ]


def test_ort_arena_oom_preserves_safe_batch_backoff_signal(tmp_path):
    from telegram_search.inference.resources import memory_exhausted

    path, _ = package(tmp_path)
    store = AdapterStore(tmp_path / "workspace")
    manifest, stage = store.stage(path)
    with AdapterWorker(manifest, stage, sys.executable, store.runner()) as worker:
        worker.request("load")
        with pytest.raises(UserError) as error:
            worker.request(
                "encode_text", {"items": [{"id": "one", "text": "ORT_OOM"}], "operation": "query"}
            )
        assert memory_exhausted(error.value)
        assert "100" not in str(error.value)


def test_frozen_child_environment_restores_system_native_paths(monkeypatch):
    from telegram_search.model_adapters.client import worker_environment

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/bundle/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/system/lib")
    monkeypatch.setenv("HF_TOKEN", "not-forwarded")
    assert worker_environment()["LD_LIBRARY_PATH"] == "/system/lib"
    assert "HF_TOKEN" not in worker_environment()


def test_failed_cleanup_is_retained_and_retried_before_another_worker(tmp_path, monkeypatch):
    import os

    path, _ = package(tmp_path)
    store = AdapterStore(tmp_path / "workspace")
    manifest, stage = store.stage(path)
    first = AdapterWorker(manifest, stage, sys.executable, store.runner())
    first.request("load")
    calls = []
    target, attribute = (os, "killpg") if os.name != "nt" else (first.job, "close")
    original = getattr(target, attribute)

    def flaky(*args):
        calls.append("stop")
        if len(calls) == 1:
            raise OSError("public fixture")
        return original(*args)

    monkeypatch.setattr(target, attribute, flaky)
    try:
        with pytest.raises(UserError, match="stop the previous"):
            first._abort()
        assert not first._cleanup_done
        assert first.process.poll() is None
        with AdapterWorker(manifest, stage, sys.executable, store.runner()) as second:
            assert first._cleanup_done and first.process.poll() is not None
            assert second.request("count_tokens", {"texts": ["public"]}) == [3]
        assert len(calls) >= 2
    finally:
        monkeypatch.setattr(target, attribute, original)
        first.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
@pytest.mark.parametrize("member", ["zombie", "live", "new-live"])
def test_darwin_zombie_group_does_not_mask_a_live_worker(monkeypatch, member):
    import io
    import os
    from types import SimpleNamespace

    import psutil

    signals = []
    reaped = []
    process = SimpleNamespace(
        pid=123, stdin=io.BytesIO(), stdout=io.BytesIO(), wait=lambda **kw: reaped.append(True)
    )
    worker = object.__new__(AdapterWorker)
    worker.process, worker.job, worker._group_stopped = process, None, False
    primary = SimpleNamespace(pid=123, status=lambda: psutil.STATUS_ZOMBIE)
    child = SimpleNamespace(
        pid=124, status=lambda: psutil.STATUS_RUNNING if member == "live" else psutil.STATUS_ZOMBIE
    )
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(psutil, "Process", lambda pid: primary)
    monkeypatch.setattr(psutil, "process_iter", lambda: iter([primary, child]))
    monkeypatch.setattr(os, "getpgid", lambda pid: 123)

    def signal_group(pid, signal):
        assert not reaped  # Do not free the owned PID before group classification.
        signals.append(signal)
        if signal == 0 and member == "new-live":
            return
        raise PermissionError("Darwin empty signalable group")

    monkeypatch.setattr(os, "killpg", signal_group)
    if member == "zombie":
        worker._stop_process()
        assert worker._group_stopped and reaped == [True] and signals == [9, 0]
    else:
        with pytest.raises(PermissionError):
            worker._stop_process()
        assert not worker._group_stopped and not reaped


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_unreadable_darwin_group_is_retained_until_cleanup_can_retry(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace

    import psutil

    from telegram_search.model_adapters.client import _failed_cleanup

    path, _ = package(tmp_path)
    store = AdapterStore(tmp_path / "workspace")
    manifest, staged = store.stage(path)
    first = AdapterWorker(manifest, staged, sys.executable, store.runner())
    try:
        first.request("load")

        def denied(*args):
            raise PermissionError("Darwin classification requires verification")

        def unreadable():
            raise psutil.AccessDenied(first.process.pid)

        with monkeypatch.context() as injected:
            injected.setattr(sys, "platform", "darwin")
            injected.setattr(os, "killpg", denied)
            injected.setattr(psutil, "Process", lambda pid: SimpleNamespace(status=unreadable))
            with pytest.raises(UserError, match="stop the previous"):
                first._abort()
            assert first in _failed_cleanup and not first._cleanup_done
            assert first.process.poll() is None
        with AdapterWorker(manifest, staged, sys.executable, store.runner()) as second:
            assert first._cleanup_done and first.process.poll() is not None
            assert first not in _failed_cleanup
            assert second.request("count_tokens", {"texts": ["public"]}) == [3]
    finally:
        first.close()


def test_unpinned_snapshot_files_are_rejected_even_when_source_hash_is_valid(tmp_path):
    path, _ = package(tmp_path)
    store = AdapterStore(tmp_path / "workspace")
    manifest, stage = store.stage(path)
    (stage / "unlisted.py").write_text("raise RuntimeError('not pinned')")
    with pytest.raises(UserError, match="unpinned"):
        AdapterWorker(manifest, stage, sys.executable, store.runner())


@pytest.mark.parametrize("role", ["reranker", "ocr", "asr"])
def test_role_outputs_reject_bad_ids_nan_coordinates_and_timestamps(tmp_path, role):
    path, _ = package(tmp_path, role)
    manifest = AdapterManifest.read(path)
    operation, payload = next(smoke_requests(manifest))
    if role == "reranker":
        invalid = [
            [{"candidate_id": "first", "score": 1}] * 2,
            [
                {"candidate_id": "first", "score": float("nan")},
                {"candidate_id": "second", "score": 1},
            ],
        ]
    elif role == "ocr":
        invalid = [
            [
                {
                    "id": "image",
                    "text": "public",
                    "regions": [{"text": "public", "bbox": [0, 0, 9, 2]}],
                }
            ],
            [{"id": "image", "text": "public", "confidence": 1.1}],
        ]
    else:
        invalid = [
            {
                "id": "silence",
                "text": "public",
                "language": "en",
                "segments": [{"start": 0, "end": 2, "text": "public"}],
            },
            {
                "id": "silence",
                "text": "public",
                "language": "en",
                "segments": [{"start": float("nan"), "end": 0.1, "text": "public"}],
            },
        ]
    for result in invalid:
        with pytest.raises(UserError):
            validate_result(manifest, operation, payload, result)


def test_worker_dies_with_owner_during_a_noncooperative_call(tmp_path):
    import psutil

    def alive(process):
        try:
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    path, _ = package(tmp_path)
    store = AdapterStore(tmp_path / "workspace")
    manifest, stage = store.stage(path)
    ready = tmp_path / "owner-ready.json"
    program = """
import json, sys
from pathlib import Path
from telegram_search.model_adapters.client import AdapterWorker
from telegram_search.model_adapters.manifest import AdapterManifest
manifest = AdapterManifest.read(Path(sys.argv[1]) / 'manifest.json')
with AdapterWorker(manifest, Path(sys.argv[1]), sys.executable, Path(sys.argv[2])) as worker:
    worker.request('load')
    Path(sys.argv[3]).write_text(json.dumps({'pid':worker.process.pid}))
    worker.request('encode_text', {'items':[{'id':'public','text':'HANG'}], 'operation':'query'})
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", program, str(stage), str(store.runner()), str(ready)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert owner.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        child = psutil.Process(json.loads(ready.read_text())["pid"])
        owner.kill()
        owner.wait(timeout=3)
        deadline = time.monotonic() + 3
        while alive(child):
            assert time.monotonic() < deadline, "worker survived owner death"
            time.sleep(0.01)
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=3)
        if child and alive(child):
            child.kill()


def test_snapshot_registration_cannot_silently_replace_its_environment(tmp_path):
    path, _ = package(tmp_path)
    store = AdapterStore(tmp_path / "workspace")
    report = validate_adapter(store, path, sys.executable, register=True)
    original = store.registrations()
    manifest, staged = store.stage(path)
    changed = {**report, "environment": {**report["environment"], "python": "3.99.0"}}
    with pytest.raises(UserError, match="environment changed"):
        store.register(manifest, staged, python=sys.executable, report=changed)
    assert store.registrations() == original


def test_examples_have_current_pinned_manifests():
    root = Path(__file__).parents[1] / "examples/custom-adapters"
    for path in root.glob("*.manifest.json"):
        AdapterManifest.read(path).verify_files(root)


@pytest.mark.parametrize("category", [[], "PRIVATE_CATEGORY"])
def test_untrusted_failure_categories_cannot_enter_diagnostic_notes(tmp_path, category):
    path, _ = package(tmp_path)
    store = AdapterStore(tmp_path / "workspace")
    manifest, staged = store.stage(path)
    with AdapterWorker(manifest, staged, sys.executable, store.runner()) as worker:
        worker.request("load")
        worker.events.put(
            (
                "message",
                {
                    "id": worker.sequence + 1,
                    "error": "adapter_failure",
                    "category": category,
                },
            )
        )
        with pytest.raises(UserError, match="invalid response") as error:
            worker.request("count_tokens", {"texts": ["public"]})
        assert "PRIVATE" not in str(error.value.__notes__)
        assert worker.process.poll() is not None
