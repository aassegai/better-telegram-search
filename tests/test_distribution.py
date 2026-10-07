import hashlib
import io
import json
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from telegram_search.config import runtime
from telegram_search.config.browser import browser_worker, open_browser
from telegram_search.inference.ocr import OcrEngine
from telegram_search.shared.errors import UserError


def test_frozen_paths_and_worker_dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "telegram-search"))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "user"))
    assert runtime.default_workspace() == tmp_path / "user/BetterTelegramSearch/workspace"
    assert runtime.frontend_directory() == tmp_path / "bundle/frontend"
    assert runtime.ocr_command("--runtime") == [sys.executable, "--internal-ocr", "--runtime"]
    monkeypatch.setenv("XDG_DATA_HOME", "relative")
    assert runtime.default_workspace().is_absolute()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "windows"))
    assert runtime.default_workspace() == tmp_path / "windows/BetterTelegramSearch/workspace"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert runtime.default_workspace().parts[-4:] == (
        "Library",
        "Application Support",
        "BetterTelegramSearch",
        "workspace",
    )


def test_source_worker_remains_a_python_module(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert runtime.default_workspace().name == "workspace"
    assert runtime.ocr_command("--runtime") == [
        sys.executable,
        "-m",
        "telegram_search.inference.ocr_worker",
        "--runtime",
    ]


def test_bundled_ocr_is_copied_offline_with_checksum(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    payload = b"public synthetic dictionary"
    directory = tmp_path / "bundle/tessdata"
    directory.mkdir(parents=True)
    (directory / "test.traineddata").write_bytes(payload)
    engine = OcrEngine.__new__(OcrEngine)
    engine.root = tmp_path / "workspace/models/ocr"
    engine.manifest = {
        "files": [
            {
                "name": "test.traineddata",
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ]
    }
    monkeypatch.setattr(engine, "_download", lambda *args: pytest.fail("Network used offline"))
    engine.prepare(offline=True)
    assert (engine.root / "test.traineddata").read_bytes() == payload
    (engine.root / "test.traineddata").unlink()
    (directory / "test.traineddata").write_bytes(b"broken")
    with pytest.raises(UserError, match="сборке повреждены"):
        engine.prepare(offline=True)
    assert not (engine.root / "test.traineddata").exists()


def test_frozen_ocr_subprocess_never_runs_python_switches(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(
            stdout=b"native runtime"
            if command[-1] == "--runtime"
            else json.dumps({"text": "synthetic", "confidence": 99}).encode()
        )

    monkeypatch.setattr("telegram_search.inference.ocr.subprocess.run", run)

    class Process:
        def __init__(self, command, **kwargs):
            commands.append(command)
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b'{"ready":true}\n{"text": "synthetic", "confidence": 99}\n')
            self.returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -1

        def wait(self, **kwargs):
            return self.returncode

    monkeypatch.setattr("telegram_search.inference.ocr_process.subprocess.Popen", Process)
    monkeypatch.setattr("telegram_search.inference.ocr.importlib.metadata.version", lambda x: "1")
    engine = OcrEngine(tmp_path)
    assert engine.recognize(b"synthetic")["text"] == "synthetic"
    engine.unload()
    assert all(command[1] == "--internal-ocr" for command in commands)


@pytest.mark.parametrize("server", [False, True])
def test_ocr_worker_json_supports_windows_stdout_encoding(monkeypatch, server):
    from telegram_search.inference.ocr_worker import main

    image = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(image, format="PNG")

    instances, resets = [], []

    class Api:
        def __init__(self, **kwargs):
            instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def SetImage(self, image):
            pass

        def Clear(self):
            pass

        def ClearAdaptiveClassifier(self):
            resets.append(1)

        def GetUTF8Text(self):
            return "Проверка архива"

        def MeanTextConf(self):
            return 99

    monkeypatch.setitem(
        sys.modules, "tesserocr", SimpleNamespace(PyTessBaseAPI=Api, PSM=SimpleNamespace(AUTO=3))
    )
    data = image.getvalue()
    frame = str(len(data)).encode() + b"\n" + data
    monkeypatch.setattr(
        sys, "stdin", SimpleNamespace(buffer=io.BytesIO(frame * 2 if server else data))
    )
    output = io.BytesIO()
    stdout = io.TextIOWrapper(output, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stdout)
    main(["synthetic", "512", "--server"] if server else ["synthetic", "512"])
    stdout.flush()
    values = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(instances) == 1
    assert len(values) == len(resets) == (2 if server else 1)
    assert all(value["text"] == "Проверка архива" for value in values)


def test_browser_launcher_preserves_parent_native_environment(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/frozen/native")
    monkeypatch.setattr(
        "telegram_search.config.browser.subprocess.Popen", lambda args, **kwargs: calls.append(args)
    )
    open_browser("http://127.0.0.1:8765")
    import os

    assert os.environ["LD_LIBRARY_PATH"] == "/frozen/native"
    assert calls == [[sys.executable, "--internal-browser", "http://127.0.0.1:8765"]]


def test_browser_helper_restores_system_environment_and_rejects_external_urls(monkeypatch):
    import os

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/frozen/native")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/system/native")
    monkeypatch.setattr(
        "telegram_search.config.browser.webbrowser.open",
        lambda url: os.environ.get("LD_LIBRARY_PATH"),
    )
    assert browser_worker("http://127.0.0.1:8765") == "/system/native"
    with pytest.raises(ValueError):
        browser_worker("https://example.com")
