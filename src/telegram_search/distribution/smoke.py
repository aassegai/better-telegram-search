"""Offline smoke test of the actual frozen executable, using synthetic data only."""

import base64
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from telegram_search.config.runtime import bundled_directory, frozen

# Identity graph, generated with ONNX helper; no weights, training dependencies or network.
IDENTITY_ONNX = (
    "CAg6TAoQCgF4EgF5IghJZGVudGl0eRIOcG9ydGFibGUtc21va2VaEwoBeBIOCgwIARIICgIIAQoCCAJi"
    "EwoBeRIOCgwIARIICgIIAQoCCAJCBAoAEA0="
)


def main():
    if not frozen():
        raise RuntimeError("Self-test must run from the portable executable")
    if sys.platform == "win32":
        import ctypes

        if ctypes.windll.kernel32.GetACP() != 65001:
            raise RuntimeError("Windows UTF-8 process code page missing")
    for module in ("torch", "torchvision", "transformers", "sentence_transformers"):
        if importlib.util.find_spec(module) is not None:
            raise RuntimeError("Excluded ML dependency in bundle")
    probe = subprocess.run(
        [sys.executable, "--internal-ocr", "--runtime"], capture_output=True, timeout=20
    )
    if probe.returncode:
        raise RuntimeError("Native OCR runtime failed:\n" + probe.stderr.decode(errors="replace"))
    import numpy as np
    import onnxruntime as ort
    from safetensors.numpy import load, save
    from tokenizers import Tokenizer, models, pre_tokenizers

    from telegram_search.inference.ocr import OcrEngine
    from telegram_search.ingestion.importer import ImportService
    from telegram_search.search.lexical import SearchService
    from telegram_search.search.vectors import VectorStore
    from telegram_search.storage.database import Database
    from telegram_search.updates.installer import build_info

    gpu_build = build_info().get("variant") == "gpu"
    if gpu_build:
        from telegram_search.inference.providers import check_bundled_cuda

        check_bundled_cuda()

    values = np.array([[3.0, 4.0]], dtype=np.float32)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    session = ort.InferenceSession(
        base64.b64decode(IDENTITY_ONNX), options, providers=["CPUExecutionProvider"]
    )
    if session.get_providers() != ["CPUExecutionProvider"]:
        raise RuntimeError("CPU provider contract")
    np.testing.assert_array_equal(session.run(None, {"x": values})[0], values)
    np.testing.assert_array_equal(load(save({"test": values}))["test"], values)
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "test": 1}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    if tokenizer.encode("test").ids != [1]:
        raise RuntimeError("Native tokenizer")
    with tempfile.TemporaryDirectory(prefix="bts-smoke-") as temporary:
        root = Path(temporary)
        db = Database(root / "workspace Тест поиск 中文")
        db.initialize()
        source = root / "result.json"
        source.write_text(
            json.dumps(
                {
                    "id": 100,
                    "name": "Synthetic build smoke",
                    "type": "personal_chat",
                    "messages": [
                        {
                            "id": 1,
                            "type": "message",
                            "date_unixtime": "1750000000",
                            "from": "Synthetic",
                            "from_id": "user1",
                            "text": "проверка архива",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        importer = ImportService(db)
        try:
            if importer.run(importer.prepare(str(source)))["state"] != "completed":
                raise RuntimeError("Synthetic import")
        finally:
            importer.shutdown()
        if not SearchService(db).search("архива")["results"]:
            raise RuntimeError("SQLite FTS5 search")
        vectors = VectorStore(db.workspace)
        vectors.upsert(
            "a" * 64,
            2,
            [{"id": "test", "chat_id": "a", "utc_day": "2025-06-15", "generation": 1}],
            values,
        )
        if vectors.exact("a" * 64, 2, values[0], ["test"], 1)[0]["id"] != "test":
            raise RuntimeError("LanceDB prefiltered search")
        ocr = OcrEngine(db.workspace, threads=1)
        ocr.prepare(offline=True)
        recognized = ocr.recognize((bundled_directory("smoke") / "ocr-smoke.png").read_bytes())
        if not all(word in recognized["text"] for word in ("12345", "67890", "ПОИСК")):
            raise RuntimeError("Native Russian/English OCR smoke")
        # Free Lance handles before temporary-directory cleanup (important on Windows).
        del vectors
        _update_check(db.workspace, root)
        _http_check(db.workspace, root)
    print(
        json.dumps(
            {
                "sqlite_fts5": True,
                "lancedb": True,
                "onnx_cpu": True,
                "tokenizers": True,
                "safetensors": True,
                "ocr_rus_eng": True,
                "frontend_http": True,
                "csrf": True,
                "torch_absent": True,
                "unicode_paths": True,
                "update_installer": True,
                "device_selection": True,
                **({"cuda_libraries": True} if gpu_build else {}),
            }
        )
    )


def _update_check(workspace, root):
    from telegram_search.inference.providers import Execution
    from telegram_search.updates.installer import apply_plan, atomic_json, tree_digest

    execution = Execution("cpu")
    if execution.provider != "CPUExecutionProvider":
        raise RuntimeError("Device selection")
    directory = workspace / "cache/updates" / ("a" * 32)
    directory.mkdir(parents=True)
    atomic_json(directory.parent / "status.json", {"state": "installing"})
    target = root / "synthetic-application"
    target.mkdir()
    (target / "version").write_text("old")
    stage = root / "synthetic-stage"
    stage.mkdir()
    (stage / "version").write_text("new")
    backup = root / "synthetic-backup"
    apply_plan(
        {
            "workspace": str(workspace),
            "target": str(target),
            "stage": str(stage),
            "backup": str(backup),
            "directory": str(directory),
            "stage_digest": tree_digest(stage),
        },
        restart=False,
    )
    if (target / "version").read_text() != "new" or (backup / "version").read_text() != "old":
        raise RuntimeError("Update installation/backup contract")


def _http_check(workspace, root):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://127.0.0.1:{port}"
    with (root / "server.log").open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "--workspace",
                str(workspace),
                "run",
                "--port",
                str(port),
                "--no-browser",
            ],
            stdout=log,
            stderr=log,
            env={**os.environ, "BTS_DISABLE_UPDATE_CHECK": "1"},
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Bundled server exited during startup")
                try:
                    with opener.open(url + "/api/session", timeout=1) as response:
                        session = json.load(response)
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.2)
            else:
                raise RuntimeError("Bundled server readiness timeout")
            with opener.open(url, timeout=5) as response:
                html = response.read().decode("utf-8")
                assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', html)
                if not assets:
                    raise RuntimeError("Bundled frontend missing")
                if response.headers.get("X-Content-Type-Options") != "nosniff":
                    raise RuntimeError("Security headers missing")
            for asset in assets:
                with opener.open(url + asset, timeout=5) as response:
                    if not response.read() or response.status != 200:
                        raise RuntimeError("Bundled frontend asset missing")
                    content_type = response.headers.get_content_type()
                    if asset.endswith(".css") and content_type != "text/css":
                        raise RuntimeError("Stylesheet MIME type")
                    if asset.endswith(".js") and content_type not in {
                        "application/javascript",
                        "text/javascript",
                    }:
                        raise RuntimeError("JavaScript MIME type")
            query = urllib.parse.urlencode({"q": "архива", "mode": "words"})
            with opener.open(url + "/api/search?" + query, timeout=5) as response:
                if not json.load(response)["results"]:
                    raise RuntimeError("Bundled HTTP search")
            request = urllib.request.Request(
                url + "/api/rebuild", data=b"{}", headers={"Content-Type": "application/json"}
            )
            try:
                opener.open(request, timeout=5).close()
            except urllib.error.HTTPError as exc:
                if exc.code != 403:
                    raise
            else:
                raise RuntimeError("CSRF protection missing")
            request.add_header("X-Session-Token", session["token"])
            opener.open(request, timeout=5).close()
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
