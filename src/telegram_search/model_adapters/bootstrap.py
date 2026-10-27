"""Frozen Windows helper: clear inherited DLL state without changing the server."""

import ctypes
import json
import os
import subprocess
import sys


def launch(python, runner, package, identity):
    from telegram_search.model_adapters.client import worker_environment

    def stage(name):
        # This is a fixed protocol event, never adapter-provided output.
        os.write(
            sys.stdout.fileno(), (json.dumps({"event": "startup", "phase": name}) + "\n").encode()
        )

    stage("bootstrap_started")

    # The helper is already in the parent's KILL_ON_JOB_CLOSE job. Its external
    # Python child inherits that job, and never sees the bundle's DLL directory.
    if not ctypes.windll.kernel32.SetDllDirectoryW(None):
        raise OSError("Could not reset the helper DLL search directory")
    stage("dll_reset")
    child = subprocess.Popen(
        [python, "-I", "-B", "-u", runner, package, identity],
        stdin=sys.stdin.buffer,
        stdout=sys.stdout.buffer,
        stderr=subprocess.DEVNULL,
        env=worker_environment(),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    stage("python_started")
    raise SystemExit(child.wait())
