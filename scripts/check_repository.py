"""Check tracked/staged paths without opening any user's private exports."""

import subprocess
import sys
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
paths = subprocess.check_output(["git", "-C", str(repo), "ls-files", "-z"]).decode().split("\0")
forbidden = {
    "workspace",
    "exports",
    "test_chat_export",
    "models",
    "cache",
    "data",
    "logs",
    "local",
    "artifacts",
    "backups",
    ".venv",
    "node_modules",
    "test-results",
    "playwright-report",
}
errors = []
for name in filter(None, paths):
    path = Path(name)
    reason = None
    if set(path.parts) & forbidden or name.startswith("frontend/dist/"):
        reason = "private/runtime directory"
    elif path.name.startswith("telegram_search_requirements_and_plan"):
        reason = "private requirements"
    elif path.name.startswith(".env") and not path.name.endswith(".example"):
        reason = "secrets file"
    elif path.suffix in {".sqlite", ".db"} or ".sqlite-" in path.name:
        reason = "private database"
    elif (repo / path).is_file() and (repo / path).stat().st_size > 10 * 1024 * 1024:
        reason = "file larger than 10 MiB"
    if reason:
        errors.append(f"{name}: {reason}")
if errors:
    print("\n".join(errors), file=sys.stderr)
    sys.exit(1)
print(f"Repository policy passed ({len(list(filter(None, paths)))} tracked files).")
