"""Bounded, cancellable native Lance queries in a reusable CPU-only child."""

import json
import math
import multiprocessing
import os
import queue
import threading
import time
from dataclasses import asdict

import psutil

from telegram_search.search.execution import checkpoint

MAX_FRAME_BYTES = 1024 * 1024


def _exit_with_parent():
    """A crashed launcher must not leave a detached native writer computing."""
    parent = multiprocessing.parent_process()
    if parent is None:
        return

    def watch():
        from multiprocessing.connection import wait

        try:
            wait([parent.sentinel])
        except (OSError, ValueError):
            pass
        os._exit(1)

    threading.Thread(target=watch, name="ann-parent-watch", daemon=True).start()


class AnnUnavailable(Exception):
    """A fixed, non-sensitive reason for falling back to the exact reader."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _serve(connection, threads, background=False):
    _exit_with_parent()
    # Native diagnostics can contain local paths. The protocol carries only a
    # fixed failure marker, never a traceback, SQL, or archive contents.
    with open(os.devnull, "w") as quiet:
        os.dup2(quiet.fileno(), 1)
        os.dup2(quiet.fileno(), 2)
        os.environ["RAYON_NUM_THREADS"] = str(threads)
        os.environ["LANCE_CPU_THREADS"] = str(threads)
        os.environ["OMP_NUM_THREADS"] = str(threads)
        if background:
            try:
                process = psutil.Process()
                process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 10)
            except (psutil.Error, AttributeError):
                pass
        try:
            import lancedb

            while True:
                request = json.loads(connection.recv_bytes(MAX_FRAME_BYTES))
                try:
                    table = lancedb.connect(request["location"]).open_table(request["table"])
                    query = None
                    try:
                        if request.get("operation") == "build":
                            from telegram_search.search.ann import AnnOptions, build_index

                            response = {
                                "status": build_index(
                                    table, request["dimension"], AnnOptions(**request["options"])
                                )
                            }
                        elif request.get("operation") == "optimize":
                            from telegram_search.search.vector_files import optimize_files

                            # Concurrent readers and interrupted writers may still
                            # own old files. Automatic maintenance retains them.
                            response = optimize_files(table)
                        else:
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
                            response = {"results": query.to_list()}
                    finally:
                        query = None  # the builder also owns a reference to the checkout
                        del table
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
    def __init__(self, *, threads=4, memory_limit_mib=512, timeout=60, background=False):
        self.threads = threads
        self.memory_limit_mib = memory_limit_mib
        self.timeout = timeout
        self.background = background
        self.process = None
        self.connection = None
        self.lock = threading.RLock()
        self.state_lock = threading.RLock()
        self.stopping = False

    def close(self):
        # Independent from the request lock: shutdown/cancellation can interrupt
        # a native call even while its exchange thread is waiting on the pipe.
        with self.state_lock:
            process, connection = self.process, self.connection
            self.stopping = True
            if process is not None:
                try:
                    if process.is_alive():
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass  # It exited between the liveness check and kill.
                    process.join(timeout=2)
                    if process.is_alive():
                        return False
                except (OSError, ValueError, AssertionError):
                    # Failure to observe termination is not proof of it. Keep
                    # the handle and writer fence so the owner can retry.
                    return False
                try:
                    process.close()
                except (OSError, ValueError):
                    pass  # Termination was proven before releasing this handle.
            if connection is not None:
                try:
                    connection.close()
                except (OSError, ValueError):
                    pass
            self.process = self.connection = None
            self.stopping = False
            return True

    def _start(self):
        with self.state_lock:
            # Cancellation and process creation share this lock. A maintenance
            # cancel that saw no child cannot be followed by a late writer start.
            checkpoint()
            if self.stopping and not self.close():
                raise AnnUnavailable("ann_worker_stopping")
            if self.process is not None and self.process.is_alive():
                return self.process, self.connection
            if not self.close():
                raise AnnUnavailable("ann_worker_stopping")
            context = multiprocessing.get_context("spawn")
            connection, child = context.Pipe()
            process = context.Process(
                target=_serve, args=(child, self.threads, self.background), daemon=True
            )
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
        with self.lock:
            return self._query(
                location=location,
                table=table,
                version=version,
                query=query,
                predicate=predicate,
                probes=probes,
                limit=limit,
            )

    def _query(self, *, location, table, version, query, predicate, probes, limit):
        response = self._request(
            {
                "location": str(location),
                "table": table,
                "version": version,
                "query": query.tolist(),
                "predicate": predicate,
                "probes": probes,
                "limit": limit,
            }
        )
        results = response.get("results")
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
            self.close()
            raise AnnUnavailable("ann_invalid_response")
        return results

    def build(self, *, location, table, dimension, options):
        with self.lock:
            return self._build(location=location, table=table, dimension=dimension, options=options)

    def _build(self, *, location, table, dimension, options):
        response = self._request(
            {
                "operation": "build",
                "location": str(location),
                "table": table,
                "dimension": dimension,
                "options": asdict(options),
            }
        )
        status = response.get("status")
        if not isinstance(status, dict) or type(status.get("ready")) is not bool:
            self.close()
            raise AnnUnavailable("ann_build_failed")
        return status

    def optimize(self, *, location, table):
        response = self._request(
            {"operation": "optimize", "location": str(location), "table": table}
        )
        if response.get("optimized") is not True:
            self.close()
            raise AnnUnavailable("ann_maintenance_failed")
        return response

    def _request(self, request):
        operation = request.get("operation", "query")
        request = json.dumps(request, allow_nan=False).encode()
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
                if not isinstance(response, dict) or response.get("error"):
                    raise AnnUnavailable(
                        "ann_build_failed" if operation == "build" else "ann_query_failed"
                    )
                return response
            except BaseException:
                self.close()
                raise
            finally:
                io.join(timeout=2)
