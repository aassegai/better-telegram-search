"""App-owned files never follow symlinks, Windows junctions or other reparse points."""

import stat
from pathlib import Path

from telegram_search.telegram_sync.models import SourceFailure


def owned_path(workspace: Path, relative: str | Path) -> Path:
    path = workspace / relative
    if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(workspace):
        raise SourceFailure("storage")
    current = workspace
    for part in path.relative_to(workspace).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        ):
            raise SourceFailure("storage")
    if path.resolve() != path or not path.resolve().is_relative_to(workspace):
        raise SourceFailure("storage")
    return path
