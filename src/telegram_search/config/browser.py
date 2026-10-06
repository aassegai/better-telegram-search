"""Open the system browser without leaking PyInstaller's native-library search path."""

import os
import subprocess
import sys
import webbrowser
from urllib.parse import urlsplit

from telegram_search.config.runtime import frozen


def open_browser(url: str):
    if not frozen():
        return webbrowser.open(url)
    # A separate helper keeps DLL/environment changes away from inference/server threads.
    subprocess.Popen(
        [sys.executable, "--internal-browser", url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


def browser_worker(url: str):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.port is None
    ):
        raise ValueError("Local browser URL required")
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetDllDirectoryW(None)
    else:
        original = os.environ.get("LD_LIBRARY_PATH_ORIG")
        if original:
            os.environ["LD_LIBRARY_PATH"] = original
        else:
            os.environ.pop("LD_LIBRARY_PATH", None)
    return webbrowser.open(url)
