import os
import shutil
import subprocess
from pathlib import Path


def repository_warning(path: Path) -> str | None:
    path = path.resolve()
    repo = next(
        (
            parent
            for parent in [path, *path.parents]
            if (parent / ".git").is_file() or (parent / ".git" / "HEAD").is_file()
        ),
        None,
    )
    if repo is None:
        return None
    if shutil.which("git"):
        relative = path.relative_to(repo).as_posix() + "/"
        try:
            tracked = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.untrackedCache=false",
                    "ls-files",
                    "-z",
                    "--",
                    relative,
                ],
                capture_output=True,
                timeout=10,
                check=False,
            )
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    f"core.excludesFile={os.devnull}",
                    "check-ignore",
                    "--no-index",
                    "-v",
                    "-z",
                    "--stdin",
                ],
                input=relative.encode() + b"\0",
                capture_output=True,
                timeout=10,
                check=False,
            )
            fields = result.stdout.split(b"\0")
            if (
                not tracked.stdout
                and tracked.returncode == 0
                and result.returncode == 0
                and len(fields) >= 4
                and not fields[2].startswith(b"!")
                and fields[0].endswith(b".gitignore")
            ):
                return None
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "Папка внутри Git-репозитория не исключена из Git. Добавьте её в .gitignore."
