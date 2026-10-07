"""A reusable, isolated OCR child with bounded per-image IPC and timeouts."""

import os
import queue
import subprocess
import threading

from telegram_search.shared.errors import UserError

MAX_IMAGE_BYTES = 32 * 1024**2
MAX_RESPONSE_BYTES = 1024 * 1024


class OcrProcess:
    def __init__(self, command, *, threads, timeout, recycle_after=128):
        self.command = command
        self.threads, self.timeout, self.recycle_after = threads, timeout, recycle_after
        self.lock = threading.RLock()
        self.process = None
        self.completed = 0

    def unload(self):
        with self.lock:
            process, self.process = self.process, None
            if process is None:
                return
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=5)
            finally:
                for stream in (process.stdin, process.stdout):
                    stream.close()

    def recognize(self, data):
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
            raise UserError("OCR не смог обработать изображение за заданное время.")
        with self.lock:
            if self.process is not None and (
                self.process.poll() is not None or self.completed >= self.recycle_after
            ):
                self.unload()
            if self.process is None:
                self.process = subprocess.Popen(
                    self.command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env={
                        **os.environ,
                        "OMP_THREAD_LIMIT": str(self.threads),
                        "OMP_NUM_THREADS": str(self.threads),
                    },
                )
                self.completed = 0
            process = self.process
            result = queue.Queue(maxsize=1)

            def exchange():
                try:
                    process.stdin.write(str(len(data)).encode("ascii") + b"\n")
                    process.stdin.write(data)
                    process.stdin.flush()
                    response = process.stdout.readline(MAX_RESPONSE_BYTES + 1)
                    if not response.endswith(b"\n") or len(response) > MAX_RESPONSE_BYTES:
                        raise ValueError("OCR response budget")
                    result.put(response)
                except (OSError, ValueError):
                    result.put(None)

            io = threading.Thread(target=exchange, name="ocr-io", daemon=True)
            io.start()
            try:
                response = result.get(timeout=self.timeout)
                if response is None:
                    raise ValueError("OCR child closed")
                self.completed += 1
                return response
            except (queue.Empty, ValueError) as exc:
                self.unload()
                raise UserError("OCR не смог обработать изображение за заданное время.") from exc
            finally:
                io.join(timeout=5)
