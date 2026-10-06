"""Locations and subprocess commands shared by source and portable applications."""

import os
import sys
from pathlib import Path


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def default_workspace() -> Path:
    if not frozen():
        return Path("workspace")
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        configured = Path(os.environ.get("XDG_DATA_HOME", ""))
        root = configured if configured.is_absolute() else Path.home() / ".local" / "share"
    return root / "BetterTelegramSearch" / "workspace"


def bundled_directory(name: str) -> Path | None:
    if frozen():
        return Path(sys._MEIPASS) / name
    return None


def frontend_directory() -> Path:
    return (
        bundled_directory("frontend") or Path(__file__).resolve().parents[3] / "frontend" / "dist"
    )


def ocr_command(*args: str) -> list[str]:
    if frozen():
        return [sys.executable, "--internal-ocr", *args]
    return [sys.executable, "-m", "telegram_search.inference.ocr_worker", *args]
