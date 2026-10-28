"""Bounded adapter inspection/validation jobs; no archive writer lock or SQL."""

import copy
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from telegram_search.model_adapters.manifest import ROLES
from telegram_search.model_adapters.store import AdapterStore
from telegram_search.model_adapters.validation import inspect_adapter, validate_adapter
from telegram_search.shared.errors import UserError

TERMINAL = {"inspected", "passed", "failed", "cancelled"}


class AdapterJobs:
    def __init__(self, workspace):
        self.store = AdapterStore(workspace)
        self.lock = threading.RLock()
        self.jobs = OrderedDict()
        self.events = {}
        self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="custom-model")

    def catalog(self):
        models, errors = [], []
        try:
            records = self.store.registrations()
        except UserError as exc:
            records = {}
            errors.append(str(exc))
        for identity in records:
            try:
                manifest, _package, _record = self.store.get(identity)
                models.append(
                    {
                        **manifest.summary(),
                        "profile": "custom:" + identity,
                        "activation_supported": manifest.role
                        in {"text_embedding", "reranker", "image_embedding", "ocr"},
                    }
                )
            except (UserError, OSError, ValueError):
                errors.append("A registered adapter package is unavailable or damaged.")
        with self.lock:
            latest = next(reversed(self.jobs), None)
            return {
                "models": models,
                "errors": errors,
                "job": self.snapshot(latest) if latest else None,
            }

    def create(self, *, action, manifest, role, python=None, expected_identity=None, context=None):
        if action not in {"inspect", "validate"} or role not in ROLES:
            raise UserError("Invalid custom model operation.")
        if action == "validate" and (not python or not expected_identity):
            raise UserError("Inspect the adapter and select its Python interpreter first.")
        with self.lock:
            if self.closed:
                raise UserError("Custom model validation is stopping.")
            if any(job["state"] not in TERMINAL for job in self.jobs.values()):
                raise UserError("Wait for the current custom model check or cancel it.")
            identity = uuid.uuid4().hex
            self.jobs[identity] = {
                "id": identity,
                "state": "pending",
                "action": action,
                "role": role,
                "progress": None,
                "report": None,
                "error": None,
                "started_at": time.monotonic(),
            }
            self.events[identity] = threading.Event()
            while len(self.jobs) > 16:
                oldest, _ = self.jobs.popitem(last=False)
                self.events.pop(oldest, None)
            self.executor.submit(self._run, identity, manifest, python, expected_identity, context)
            return self.snapshot(identity)

    def _run(self, identity, path, python, expected_identity, context):
        event = self.events[identity]

        def check():
            if event.is_set():
                raise InterruptedError("Custom model check cancelled.")

        def progress(value):
            check()
            with self.lock:
                self.jobs[identity]["progress"] = value

        def saving():
            with self.lock:
                check()
                self.jobs[identity]["state"] = "saving"

        try:
            with self.lock:
                self.jobs[identity]["state"] = "running"
                action, role = self.jobs[identity]["action"], self.jobs[identity]["role"]
            check()
            if action == "inspect":
                report = inspect_adapter(
                    path, check=check, on_progress=progress, expected_role=role
                )
            else:
                report = validate_adapter(
                    self.store,
                    path,
                    python,
                    context=context,
                    register=True,
                    cancelled=event,
                    on_progress=progress,
                    before_register=saving,
                    expected_role=role,
                    expected_identity=expected_identity,
                )
            with self.lock:
                self.jobs[identity].update(
                    state=report["state"], report=report, finished_at=time.monotonic()
                )
        except InterruptedError:
            with self.lock:
                self.jobs[identity].update(
                    state="cancelled", progress=None, finished_at=time.monotonic()
                )
        except Exception as exc:
            error = (
                str(exc)
                if isinstance(exc, UserError)
                else "Could not check the local adapter package."
            )
            with self.lock:
                self.jobs[identity].update(
                    state="failed", error=error, finished_at=time.monotonic()
                )

    def snapshot(self, identity):
        with self.lock:
            if identity not in self.jobs:
                raise UserError("Custom model check not found.")
            result = copy.deepcopy(self.jobs[identity])
            result["elapsed_seconds"] = max(
                0, result.pop("finished_at", time.monotonic()) - result.pop("started_at")
            )
            return result

    def cancel(self, identity):
        with self.lock:
            job = self.jobs.get(identity)
            if not job:
                raise UserError("Custom model check not found.")
            if job["state"] == "saving":
                raise UserError(
                    "The custom model registration is being saved. Wait for completion."
                )
            if job["state"] not in TERMINAL:
                self.events[identity].set()
            return self.snapshot(identity)

    def shutdown(self):
        with self.lock:
            self.closed = True
            for identity, job in self.jobs.items():
                if job["state"] not in TERMINAL and job["state"] != "saving":
                    self.events[identity].set()
        self.executor.shutdown(wait=True, cancel_futures=True)
