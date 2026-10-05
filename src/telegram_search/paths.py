import os
from pathlib import Path, PurePosixPath

from .errors import UserError


def relative_root(workspace: Path, source: Path) -> str:
    return os.path.relpath(source.resolve(), workspace.resolve())


def safe_media_path(root: Path, relative: str) -> Path:
    # Telegram JSON uses POSIX separators, but reject Windows paths on every OS too.
    relative = relative.replace("\\", "/")
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or ".." in path.parts or ":" in relative:
        raise UserError("Недопустимый путь вложения в экспорте.")
    root = root.resolve()
    candidate = (root / Path(*path.parts)).resolve()
    if not candidate.is_relative_to(root):
        raise UserError("Вложение выходит за пределы папки экспорта.")
    return candidate
