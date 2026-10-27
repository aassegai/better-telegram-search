"""Managed JSON RPC to a trusted adapter's separate Python environment."""

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from telegram_search.model_adapters.contracts import (
    MAX_WIRE_BYTES,
    OPERATIONS,
    number,
    validate_request,
    validate_result,
)
from telegram_search.model_adapters.manifest import strict_json
from telegram_search.search.execution import checkpoint
from telegram_search.shared.errors import UserError

_failed_cleanup = set()
_failed_cleanup_lock = threading.Lock()
STARTUP_STAGES = (
    "process_started",
    "bootstrap_started",
    "dll_reset",
    "python_started",
    "worker_started",
    "package_verified",
    "adapter_ready",
)


@dataclass(frozen=True)
class AdapterContext:
    device: str = "cpu"
    gpu_device_id: int = 0
    gpu_memory_limit_mib: int = 4096
    cpu_threads: int = 4
    batch_size: int = 4

    def validate(self):
        if self.device not in {"cpu", "gpu"}:
            raise UserError("Select an explicit CPU or GPU device for the custom model.")
        for key, low, high in (
            ("gpu_device_id", 0, 63),
            ("gpu_memory_limit_mib", 256, 131072),
            ("cpu_threads", 1, 32),
            ("batch_size", 1, 128),
        ):
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise UserError("Invalid custom model resource limits.")


def worker_environment():
    # Do not hand application/Telegram/HF credentials or PYTHONPATH to a model.
    # This is defense in depth, not a security sandbox for user-selected code.
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "TMP",
        "TEMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "CUDA_PATH",
        "CUDA_HOME",
        "CUDA_VISIBLE_DEVICES",
    }
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    if getattr(sys, "frozen", False):
        for key in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
            original = os.environ.get(key + "_ORIG")
            if original:
                result[key] = original
            else:
                result.pop(key, None)
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            result["PATH"] = os.pathsep.join(
                entry
                for entry in result.get("PATH", "").split(os.pathsep)
                if entry and not Path(entry).absolute().is_relative_to(Path(bundle).absolute())
            )
    result.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return result


class AdapterWorker:
    def __init__(
        self, manifest, package, python, runner, *, context=None, on_progress=None, cancelled=None
    ):
        with _failed_cleanup_lock:
            previous = tuple(_failed_cleanup)
        for worker in previous:
            worker._abort()
        self.manifest = manifest
        self.context = context or AdapterContext()
        self.context.validate()
        if self.context.device not in manifest.data["devices"]:
            raise UserError("The custom model does not support the selected device.")

        def check():
            checkpoint()
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("Custom model operation cancelled.")

        manifest.verify_files(package, closed=True, check=check)
        python = Path(python).expanduser().absolute()
        if not python.is_file() or not os.access(python, os.X_OK):
            raise UserError("Select an executable Python interpreter for the adapter environment.")
        self.events = queue.Queue(maxsize=128)
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.sequence = 0
        self._cleanup_lock = threading.Lock()
        self._cleanup_done = False
        self._group_stopped = False
        self.on_progress = on_progress
        self.startup_phase = "process_started"
        self.job = None
        command = [str(python), "-I", "-B", "-u", str(runner), str(package), manifest.identity]
        if os.name == "nt" and getattr(sys, "frozen", False):
            command = [
                sys.executable,
                "--internal-adapter",
                str(python),
                str(runner),
                str(package),
                manifest.identity,
            ]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=package,
                env=worker_environment(),
                start_new_session=os.name != "nt",
                # CREATE_SUSPENDED is a Win32 flag not exported by subprocess.
                creationflags=(subprocess.CREATE_NO_WINDOW | 0x00000004) if os.name == "nt" else 0,
            )
            if os.name == "nt":
                from telegram_search.model_adapters.windows_job import WindowsJob

                self.job = WindowsJob(self.process)
        except OSError as exc:
            if hasattr(self, "process"):
                self.process.kill()
                self.process.wait(timeout=2)
                self.process.stdin.close()
                self.process.stdout.close()
            raise UserError("Could not start the custom model Python environment.") from exc
        self.reader = threading.Thread(target=self._read, name="adapter-output", daemon=True)
        try:
            self.reader.start()
        except BaseException:
            self._abort()
            raise

    def _put(self, value):
        while not self.closed.is_set():
            try:
                self.events.put(value, timeout=0.05)
                return
            except queue.Full:
                pass

    def _read(self):
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(MAX_WIRE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_WIRE_BYTES or not line.endswith(b"\n"):
                    self._put(("failure", "size"))
                    return
                self._put(("message", strict_json(line)))
        except Exception:
            self._put(("failure", "protocol"))
        finally:
            self._put(("failure", "exited"))

    def _send(self, request):
        try:
            value = json.dumps(request, allow_nan=False, separators=(",", ":")).encode() + b"\n"
        except (ValueError, TypeError, RecursionError) as exc:
            raise UserError("Invalid custom model request.") from exc
        if len(value) > MAX_WIRE_BYTES:
            raise UserError("Custom model batch is too large; reduce its size.")

        def write():
            try:
                self.process.stdin.write(value)
                self.process.stdin.flush()
            except (OSError, ValueError):
                self._put(("failure", "write"))

        # A worker stuck in import/native code may not read stdin. Even a large
        # request must remain cancellable rather than block the caller on write.
        threading.Thread(target=write, name="adapter-request", daemon=True).start()

    def _abort(self):
        self.closed.set()
        with self._cleanup_lock:
            if self._cleanup_done:
                return
            try:
                self._stop_process()
            except (OSError, subprocess.TimeoutExpired) as exc:
                with _failed_cleanup_lock:
                    _failed_cleanup.add(self)
                raise UserError(
                    "Could not stop the previous custom model worker. "
                    "Retry cleanup before loading another model."
                ) from exc
            else:
                self._cleanup_done = True
                with _failed_cleanup_lock:
                    _failed_cleanup.discard(self)

    def _stop_process(self):
        if self.job and not self._group_stopped:
            self.job.close()  # Includes descendants even if the worker already exited.
            self._group_stopped = True
        elif os.name != "nt" and not self._group_stopped:
            try:
                # The owned group can outlive its primary process and hold pipes.
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Darwin returns EPERM for a group containing only zombies.
                # Keep the leader unreaped while checking membership: its PID
                # cannot be recycled into another process group during cleanup.
                if sys.platform != "darwin" or not self._group_exited():
                    raise
            self._group_stopped = True
        elif self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=2)

        def close_streams():
            for stream in (self.process.stdin, self.process.stdout):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

        # Do not let an inherited pipe/BufferedIO lock undo the RPC deadline.
        cleanup = threading.Thread(target=close_streams, name="adapter-pipes", daemon=True)
        cleanup.start()
        cleanup.join(timeout=0.1)

    def _group_exited(self):
        import psutil

        try:
            try:
                if psutil.Process(self.process.pid).status() != psutil.STATUS_ZOMBIE:
                    return False
            except psutil.ZombieProcess:
                pass
            except psutil.NoSuchProcess:
                return False
            for process in psutil.process_iter():
                try:
                    if (
                        os.getpgid(process.pid) == self.process.pid
                        and process.status() != psutil.STATUS_ZOMBIE
                    ):
                        return False
                except (ProcessLookupError, psutil.NoSuchProcess):
                    continue
        except psutil.Error:
            # Unknown membership is not proof of a dead group. Keep the original
            # killpg PermissionError, so _abort retains this worker for cleanup.
            return False
        try:
            # A live descendant might fork while process_iter takes its snapshot.
            # Signal zero detects a newly visible, signalable group member.
            os.killpg(self.process.pid, 0)
        except (PermissionError, ProcessLookupError):
            return True
        return False

    def request(self, operation, payload=None, *, timeout=60, cancelled=None):
        if not number(timeout) or not 0.05 <= timeout <= 3600:
            raise UserError("Invalid custom model timeout.")
        payload = {} if payload is None else payload
        common = {"info", "validate", "load", "unload", "close"}
        if operation not in common:
            validate_request(self.manifest, operation, payload)
        elif payload != {}:
            raise UserError("Invalid custom model lifecycle request.")
        deadline = time.monotonic() + timeout

        def check():
            checkpoint()
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("Custom model operation cancelled.")
            if time.monotonic() >= deadline:
                raise UserError("Custom model timed out; its worker was stopped.")
            if self.closed.is_set():
                raise UserError("Custom model worker is closed. Prepare the model again.")

        while not self.lock.acquire(timeout=0.05):
            check()
        try:
            check()
            self.sequence += 1
            request_id = self.sequence
            self._send(
                {
                    "id": request_id,
                    "operation": operation,
                    "payload": payload,
                    "context": asdict(self.context),
                }
            )
            while True:
                check()
                try:
                    kind, message = self.events.get(timeout=0.05)
                except queue.Empty:
                    continue
                if kind != "message":
                    raise UserError(
                        "Custom model worker failed. "
                        "Check its interpreter, files and dependency versions."
                    )
                if isinstance(message, dict) and message.get("event") == "startup":
                    phase = message.get("phase")
                    if set(message) != {"event", "phase"} or phase not in STARTUP_STAGES:
                        raise UserError("Custom model worker returned an invalid response.")
                    if STARTUP_STAGES.index(phase) > STARTUP_STAGES.index(self.startup_phase):
                        self.startup_phase = phase
                    continue
                if not isinstance(message, dict) or message.get("id") != request_id:
                    raise UserError("Custom model worker returned an invalid response.")
                if message.get("event") == "progress":
                    self._progress(message)
                    continue
                if "error" in message:
                    if message["error"] == "out_of_memory":
                        raise UserError("Custom model out of memory; reduce the batch size.")
                    raise UserError(
                        "Custom model operation failed. Check the adapter implementation."
                    )
                if "result" not in message:
                    raise UserError("Custom model worker returned an invalid response.")
                result = message["result"]
                check()
                if operation == "info":
                    return self._info(result)
                if operation in common:
                    expected = {
                        "validate": {"valid": True},
                        "load": {"loaded": True},
                        "unload": {"loaded": False},
                        "close": {"closed": True},
                    }[operation]
                    if result != expected:
                        raise UserError("Invalid custom model lifecycle response.")
                    return result
                return validate_result(self.manifest, operation, payload, result)
        except BaseException as exc:
            # Cancellation must terminate a native call, not just abandon its reply.
            exc.add_note("Adapter startup: " + self.startup_phase)
            self._abort()
            raise
        finally:
            self.lock.release()

    def _info(self, result):
        if not isinstance(result, dict) or not isinstance(result.get("capabilities"), dict):
            raise UserError("Custom model capabilities are invalid.")
        caps = result["capabilities"]
        if (
            type(caps.get("adapter_api_version")) is not int
            or caps["adapter_api_version"] != 1
            or caps.get("role") != self.manifest.role
            or caps.get("devices") != self.manifest.data["devices"]
            or not isinstance(caps.get("operations"), list)
            or not all(isinstance(v, str) for v in caps["operations"])
            or set(caps["operations"]) != OPERATIONS[self.manifest.role]
            or not isinstance(result.get("environment"), dict)
            or result["environment"].get("dependencies") != self.manifest.data["dependencies"]
        ):
            raise UserError("Custom model capabilities differ from the manifest.")
        # Do not expose arbitrary adapter-provided prose or local paths in status.
        return {
            "capabilities": {
                k: caps[k] for k in ("adapter_api_version", "role", "devices", "operations")
            },
            "environment": result["environment"],
        }

    def _progress(self, message):
        done, total = message.get("completed"), message.get("total")
        if (
            not number(done)
            or done < 0
            or (total is not None and (not number(total) or total < done))
            or message.get("unit") not in {"items", "tokens", "seconds", "bytes"}
            or not isinstance(message.get("phase"), str)
            or not re.fullmatch(r"[a-z0-9_]{1,64}", message["phase"])
        ):
            raise UserError("Custom model progress is invalid.")
        if self.on_progress:
            self.on_progress({k: message[k] for k in ("completed", "total", "unit", "phase")})

    def close(self):
        if not self.closed.is_set() and self.lock.acquire(blocking=False):
            self.lock.release()
            try:
                self.request("close", timeout=1)
            except (UserError, InterruptedError):
                pass
        self._abort()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
