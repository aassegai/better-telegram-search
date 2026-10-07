"""A reusable, isolated OCR child with bounded per-image IPC and timeouts."""

import os
import queue
import subprocess
import threading
import time

import psutil

from telegram_search.shared.errors import UserError

MAX_IMAGE_BYTES = 32 * 1024**2
MAX_RESPONSE_BYTES = 1024 * 1024


class OcrProcess:
    def __init__(
        self,
        command,
        *,
        threads,
        timeout,
        recycle_after=1024,
        memory_limit_mib=1024,
        region_batch=8,
        ready_handshake=False,
    ):
        self.command = command
        self.threads, self.timeout, self.recycle_after = threads, timeout, recycle_after
        self.lock = threading.RLock()
        self.state_lock = threading.RLock()
        self.process = None
        self.completed = 0
        self.memory_limit_mib = memory_limit_mib
        self.region_batch = region_batch
        self.starts = 0
        self.last_stats = {}
        self.generation = 0
        self.ready_handshake = ready_handshake

    def unload(self, *, cancel=True):
        # Separate from the request lock: shutdown can kill a hung inference now.
        with self.state_lock:
            if cancel:
                self.generation += 1
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

    def recognize(self, data, *, generation=None):
        return self._request([data], batch=False, generation=generation)[0]

    def recognize_many(self, images):
        return self._request(images, batch=True)

    def _request(self, images, *, batch, generation=None):
        if (
            not 1 <= len(images) <= 4
            or any(
                not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES
                for data in images
            )
            or sum(len(data) for data in images) > MAX_IMAGE_BYTES
        ):
            raise UserError("OCR не смог обработать изображение за заданное время.")
        with self.lock:
            started = time.monotonic()
            with self.state_lock:
                if generation is not None and generation != self.generation:
                    raise UserError("OCR остановлен.")
                rss = 0
                if self.process is not None and self.process.poll() is None:
                    try:
                        rss = psutil.Process(self.process.pid).memory_info().rss
                    except psutil.Error:
                        pass
                if self.process is not None and (
                    self.process.poll() is not None
                    or self.completed >= self.recycle_after
                    or rss > self.memory_limit_mib * 1024**2
                ):
                    self.unload(cancel=False)
                cold = self.process is None
                if cold:
                    self.process = subprocess.Popen(
                        self.command,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        env={
                            **os.environ,
                            "OMP_THREAD_LIMIT": str(self.threads),
                            "OMP_NUM_THREADS": str(self.threads),
                            "OCR_REGION_BATCH": str(self.region_batch),
                            "OCR_READY_HANDSHAKE": "1" if self.ready_handshake else "0",
                        },
                    )
                    self.completed = 0
                    self.starts += 1
                process = self.process
            result = queue.Queue(maxsize=1)
            cold_seconds = [0.0]

            def exchange():
                try:
                    if cold and self.ready_handshake:
                        ready = process.stdout.readline(64)
                        if ready not in (b'{"ready":true}\n', b'{"ready":true}\r\n'):
                            raise ValueError("OCR readiness")
                        cold_seconds[0] = time.monotonic() - started
                    if batch:
                        process.stdin.write(f"B{len(images)}:{self.region_batch}\n".encode("ascii"))
                    for data in images:
                        process.stdin.write(str(len(data)).encode("ascii") + b"\n")
                        process.stdin.write(data)
                    process.stdin.flush()
                    responses = []
                    for _ in images:
                        response = process.stdout.readline(MAX_RESPONSE_BYTES + 1)
                        if not response.endswith(b"\n") or len(response) > MAX_RESPONSE_BYTES:
                            raise ValueError("OCR response budget")
                        responses.append(response)
                    result.put(responses)
                except (OSError, ValueError):
                    result.put(None)

            io = threading.Thread(target=exchange, name="ocr-io", daemon=True)
            io.start()
            try:
                response = result.get(timeout=self.timeout)
                if response is None:
                    raise ValueError("OCR child closed")
                self.completed += len(images)
                self.last_stats = {
                    "cold_start": int(cold),
                    "process_starts": self.starts,
                    "child_rss_bytes": rss,
                    "request_seconds": time.monotonic() - started,
                    "cold_start_seconds": cold_seconds[0],
                }
                return response
            except (queue.Empty, ValueError) as exc:
                self.unload(cancel=False)
                raise UserError("OCR не смог обработать изображение за заданное время.") from exc
            finally:
                io.join(timeout=5)
