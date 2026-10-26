"""Bounded, cancellable native Lance queries in a reusable CPU-only child."""

import json
import math
import multiprocessing
import os
import queue
import threading
import time

import psutil

from telegram_search.search.execution import checkpoint

MAX_FRAME_BYTES = 1024 * 1024


class AnnUnavailable(Exception):
    """A fixed, non-sensitive reason for falling back to the exact reader."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _serve(connection, threads):
    # Native diagnostics can contain local paths. The protocol carries only a
    # fixed failure marker, never a traceback, SQL, or archive contents.
    with open(os.devnull, "w") as quiet:
        os.dup2(quiet.fileno(), 1)
        os.dup2(quiet.fileno(), 2)
        os.environ["RAYON_NUM_THREADS"] = str(threads)
        os.environ["LANCE_CPU_THREADS"] = str(threads)
        os.environ["OMP_NUM_THREADS"] = str(threads)
        try:
            import lancedb

            while True:
                request = json.loads(connection.recv_bytes(MAX_FRAME_BYTES))
                try:
                    table = lancedb.connect(request["location"]).open_table(request["table"])
                    query = None
                    try:
                        table.checkout(request["version"])
                        query = (
                            table.search(request["query"])
                            .distance_type("cosine")
                            .nprobes(request["probes"])
                            .select(["id", "_distance"])
                            .limit(request["limit"])
                        )
                        if request["predicate"]:
                            query = query.where(request["predicate"], prefilter=True)
                        # fast_search would discard the unindexed append tail.
                        result = query.to_list()
                    finally:
                        query = None  # the builder also owns a reference to the checkout
                        del table
                    response = {"results": result}
                except Exception:
                    response = {"error": True}
                encoded = json.dumps(response, allow_nan=False).encode()
                if len(encoded) > MAX_FRAME_BYTES:
                    encoded = b'{"error":true}'
                connection.send_bytes(encoded)
        except (EOFError, OSError, ValueError):
            pass
        finally:
            connection.close()


class AnnProcess:
    def __init__(self, *, threads=4, memory_limit_mib=512, timeout=60):
        self.threads = threads
        self.memory_limit_mib = memory_limit_mib
        self.timeout = timeout
        self.process = None
        self.connection = None
        self.lock = threading.RLock()
        self.state_lock = threading.RLock()

    def close(self):
        # Independent from the request lock: shutdown/cancellation can interrupt
        # a native call even while its exchange thread is waiting on the pipe.
        with self.state_lock:
            process, connection = self.process, self.connection
            self.process = self.connection = None
            if process is not None:
                if process.is_alive():
                    process.kill()
                process.join(timeout=2)
                if not process.is_alive():
                    process.close()
            if connection is not None:
                connection.close()

    def _start(self):
        with self.state_lock:
            if self.process is not None and self.process.is_alive():
                return self.process, self.connection
            self.close()
            context = multiprocessing.get_context("spawn")
            connection, child = context.Pipe()
            process = context.Process(target=_serve, args=(child, self.threads), daemon=True)
            try:
                process.start()
            except BaseException:
                child.close()
                connection.close()
                raise
            child.close()
            self.process, self.connection = process, connection
            return process, connection

    def _check_memory(self, process):
        try:
            if psutil.Process(process.pid).memory_info().rss > self.memory_limit_mib * 1024**2:
                raise AnnUnavailable("ann_memory_limit")
        except psutil.Error:
            raise AnnUnavailable("ann_worker_failed") from None

    def query(self, *, location, table, version, query, predicate, probes, limit):
        request = json.dumps(
            {
                "location": str(location),
                "table": table,
                "version": version,
                "query": query.tolist(),
                "predicate": predicate,
                "probes": probes,
                "limit": limit,
            },
            allow_nan=False,
        ).encode()
        if len(request) > MAX_FRAME_BYTES:
            raise AnnUnavailable("ann_request_budget")
        with self.lock:
            checkpoint()
            process, connection = self._start()
            try:
                self._check_memory(process)
            except BaseException:
                self.close()
                raise
            replies = queue.Queue(maxsize=1)

            def exchange():
                try:
                    connection.send_bytes(request)
                    replies.put(json.loads(connection.recv_bytes(MAX_FRAME_BYTES)))
                except (EOFError, OSError, ValueError, TypeError):
                    replies.put(None)

            io = threading.Thread(target=exchange, name="ann-query-io", daemon=True)
            io.start()
            started = time.monotonic()
            try:
                while True:
                    checkpoint()
                    try:
                        response = replies.get(timeout=0.05)
                        break
                    except queue.Empty:
                        if time.monotonic() - started > self.timeout:
                            raise AnnUnavailable("ann_query_timeout") from None
                        self._check_memory(process)
                checkpoint()
                self._check_memory(process)
                if not isinstance(response, dict) or "results" not in response:
                    raise AnnUnavailable("ann_query_failed")
                results = response["results"]
                if (
                    not isinstance(results, list)
                    or len(results) > limit
                    or any(
                        not isinstance(row, dict)
                        or not isinstance(row.get("id"), str)
                        or not isinstance(row.get("_distance"), (int, float))
                        or not math.isfinite(row["_distance"])
                        for row in results
                    )
                ):
                    raise AnnUnavailable("ann_invalid_response")
                return results
            except BaseException:
                self.close()
                raise
            finally:
                io.join(timeout=2)
