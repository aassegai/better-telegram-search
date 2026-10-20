import os
import stat
from pathlib import Path, PurePosixPath

from telegram_search.shared.errors import UserError


def is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return bool(
        stat.S_ISLNK(info.st_mode)
        or getattr(path, "is_junction", lambda: False)()
        or getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def owned_vector_path(workspace: Path, relative: Path) -> Path:
    """Local vector files never follow symlinks or Windows junction/reparse points."""
    path = workspace / relative
    if ".." in relative.parts or not path.is_relative_to(workspace):
        raise UserError("Векторный индекс должен находиться внутри workspace.")
    current = workspace
    for part in path.relative_to(workspace).parts:
        current /= part
        if is_link_or_reparse(current):
            raise UserError("Векторный индекс не может содержать ссылки или junction.")
    resolved = path.resolve()
    if resolved != path or not resolved.is_relative_to(workspace):
        raise UserError("Векторный индекс должен находиться внутри workspace.")
    return path


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
