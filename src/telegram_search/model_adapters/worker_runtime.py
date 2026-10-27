"""Standalone stdlib worker. Launched by an explicitly selected external Python.

Keep this file independent of application/optional runtime imports: frozen builds
ship its source as data. Adapter stdout/stderr never becomes application logging.
"""

import base64
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import queue
import re
import signal
import stat
import sys
import threading
import time
from array import array
from pathlib import Path

MAX_WIRE = 64 * 1024**2


def run(root, expected_identity):
    wire = sys.stdout.buffer
    sys.stdout = sys.stderr
    write_lock = threading.Lock()

    def emit(value):
        raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode() + b"\n"
        if len(raw) > MAX_WIRE:
            raise ValueError("response_size")
        with write_lock:
            wire.write(raw)
            wire.flush()

    def regular(path):
        for parent in (path, *path.parents):
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("package_link")
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("package_file")

    requests = queue.Queue(maxsize=2)
    cancelled = threading.Event()
    active = [None]

    def read():
        try:
            while True:
                raw = sys.stdin.buffer.readline(MAX_WIRE + 1)
                if not raw or len(raw) > MAX_WIRE or not raw.endswith(b"\n"):
                    break
                item = json.loads(raw)
                if (
                    isinstance(item, dict)
                    and item.get("cancel") == active[0]
                    and active[0] is not None
                ):
                    cancelled.set()
                else:
                    requests.put(item)
        except Exception:
            pass
        finally:
            cancelled.set()
            # Parent death closes this pipe. Native calls may never checkpoint.
            if os.name != "nt" and os.getpgrp() == os.getpid():
                os.killpg(os.getpgrp(), signal.SIGKILL)
            os._exit(4)

    threading.Thread(target=read, name="adapter-input", daemon=True).start()

    manifest_path = root / "manifest.json"
    regular(manifest_path)
    canonical = manifest_path.read_bytes()
    if len(canonical) > 1024**2 or hashlib.sha256(canonical).hexdigest() != expected_identity:
        raise ValueError("manifest_identity")
    manifest = json.loads(canonical)
    allowed = {item["path"] for item in manifest["files"]} | {"manifest.json"}
    for path in root.rglob("*"):
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("package_link")
        if not path.is_dir() and path.relative_to(root).as_posix() not in allowed:
            raise ValueError("package_extra_file")
    for item in manifest["files"]:
        path = root / item["path"]
        regular(path)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != item["sha256"] or path.stat().st_size != item["bytes"]:
            raise ValueError("package_checksum")
    versions = {name: importlib.metadata.version(name) for name in manifest["dependencies"]}
    if versions != manifest["dependencies"]:
        raise ValueError("dependency_version")
    file, factory = manifest["entrypoint"].split(":")
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("bts_user_model_adapter", root / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    adapter = getattr(module, factory)()
    methods = {
        "text_embedding": {"encode_text", "count_tokens"},
        "image_embedding": {"encode_images", "encode_image_queries"},
        "reranker": {"rerank"},
        "ocr": {"recognize"},
        "asr": {"transcribe"},
    }
    for method in {"info", "validate", "load", "unload", "close", *methods[manifest["role"]]}:
        if not callable(getattr(adapter, method, None)):
            raise ValueError("missing_method")

    class Context:
        def __init__(self, settings, request_id):
            self.package_root = root
            self.device = settings["device"]
            self.gpu_device_id = settings["gpu_device_id"]
            self.gpu_memory_limit_mib = settings["gpu_memory_limit_mib"]
            self.cpu_threads = settings["cpu_threads"]
            self.batch_size = settings["batch_size"]
            self.offline = True
            self._id, self._last_progress = request_id, 0.0

        def checkpoint(self):
            if cancelled.is_set():
                raise InterruptedError("cancelled")

        def is_cancelled(self):
            return cancelled.is_set()

        def progress(self, completed, total=None, *, unit="items", phase="inference"):
            self.checkpoint()
            if (
                type(completed) not in (int, float)
                or not math.isfinite(completed)
                or completed < 0
                or (
                    total is not None
                    and (
                        type(total) not in (int, float)
                        or not math.isfinite(total)
                        or total < completed
                    )
                )
                or unit not in {"items", "tokens", "seconds", "bytes"}
                or not isinstance(phase, str)
                or not re.fullmatch(r"[a-z0-9_]{1,64}", phase)
            ):
                raise ValueError("progress_contract")
            now = time.monotonic()
            if now - self._last_progress >= 0.05 or completed == total:
                emit(
                    {
                        "id": self._id,
                        "event": "progress",
                        "completed": completed,
                        "total": total,
                        "unit": unit,
                        "phase": phase,
                    }
                )
                self._last_progress = now

    def images(items):
        return [
            {**item, "pixels": base64.b64decode(item["pixels"], validate=True)} for item in items
        ]

    def audio(item):
        pcm = array("f")
        pcm.frombytes(base64.b64decode(item["pcm_f32le"], validate=True))
        if sys.byteorder != "little":
            pcm.byteswap()
        return {key: value for key, value in item.items() if key != "pcm_f32le"} | {"pcm": pcm}

    loaded = False
    while True:
        request = requests.get()
        if request is None:
            break
        request_id = request["id"]
        operation, payload = request["operation"], request["payload"]
        active[0] = request_id
        cancelled.clear()
        context = Context(request["context"], request_id)
        try:
            if context.device not in manifest["devices"]:
                raise ValueError("device_unavailable")
            if operation == "info":
                result = {
                    "capabilities": adapter.info(),
                    "environment": {
                        "python": sys.version.split()[0],
                        "implementation": sys.implementation.name,
                        "dependencies": versions,
                    },
                }
            elif operation == "validate":
                adapter.validate(context)
                result = {"valid": True}
            elif operation == "load":
                adapter.load(context)
                loaded = True
                result = {"loaded": True}
            elif operation == "unload":
                adapter.unload(context)
                loaded = False
                result = {"loaded": False}
            elif operation == "close":
                adapter.close(context)
                result = {"closed": True}
            elif operation == "count_tokens":
                result = adapter.count_tokens(payload["texts"], context)
            elif not loaded:
                raise ValueError("model_not_loaded")
            elif operation == "encode_text":
                result = adapter.encode_text(payload["items"], payload["operation"], context)
            elif operation == "encode_image_queries":
                result = adapter.encode_image_queries(payload["items"], context)
            elif operation == "encode_images":
                result = adapter.encode_images(images(payload["images"]), context)
            elif operation == "rerank":
                result = adapter.rerank(payload["query"], payload["candidates"], context)
            elif operation == "recognize":
                result = adapter.recognize(images(payload["images"]), context)
            elif operation == "transcribe":
                result = adapter.transcribe(audio(payload["audio"]), payload["language"], context)
            else:
                raise ValueError("unknown_operation")
            context.checkpoint()
            emit({"id": request_id, "result": result})
        except Exception as exc:
            # Exception text may include input text, credentials or private paths.
            message = str(exc).lower()
            out_of_memory = (
                isinstance(exc, MemoryError)
                or ("available memory of" in message and "smaller than requested" in message)
                or any(
                    term in message
                    for term in (
                        "out of memory",
                        "bad_alloc",
                        "failed to allocate",
                        "cublas_status_alloc_failed",
                        "cudnn_status_alloc_failed",
                        "cuda_error_out_of_memory",
                        "cudaerrormemoryallocation",
                    )
                )
            )
            emit(
                {
                    "id": request_id,
                    "error": "cancelled"
                    if isinstance(exc, InterruptedError)
                    else "out_of_memory"
                    if out_of_memory
                    else "adapter_failure",
                }
            )
        finally:
            active[0] = None
        if operation == "close":
            break


if __name__ == "__main__":
    try:
        run(Path(sys.argv[1]), sys.argv[2])
    except Exception:
        # Import/dependency errors do not expose module paths or user code output.
        sys.exit(3)
