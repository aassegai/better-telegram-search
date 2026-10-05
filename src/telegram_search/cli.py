import argparse
import json
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn
from filelock import FileLock

from .database import Database
from .diagnostics import doctor
from .errors import UserError
from .importer import ImportService


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальный поиск по Telegram Desktop (CPU)")
    parser.add_argument("--workspace", type=Path, default=Path("workspace"))
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("setup", "doctor", "rebuild", "compact"):
        commands.add_parser(name)
    run = commands.add_parser("run")
    run.add_argument("--port", type=int, default=8765)
    run.add_argument("--no-browser", action="store_true")
    run.add_argument("--frontend", type=Path)
    run.add_argument("--workspace", type=Path, dest="run_workspace")
    load = commands.add_parser("import")
    load.add_argument("json_path")
    load.add_argument("--source-root")
    load.add_argument("--scope", default="default")
    load.add_argument("--target-chat-id")
    load.add_argument("--prefer-imported", action="store_true")
    load.add_argument("--create-new", action="store_true")
    args = parser.parse_args()
    if getattr(args, "run_workspace", None) is not None:
        args.workspace = args.run_workspace
    db = Database(args.workspace)
    try:
        db.initialize()
        if args.command == "run":
            from .api import create_app

            if not 1 <= args.port <= 65535:
                raise UserError("Порт должен быть от 1 до 65535.")
            if not args.no_browser:
                timer = threading.Timer(1.0, webbrowser.open, [f"http://127.0.0.1:{args.port}"])
                timer.daemon = True
                timer.start()
            uvicorn.run(
                create_app(args.workspace, args.frontend),
                host="127.0.0.1",
                port=args.port,
                workers=1,
                access_log=False,
            )
        elif args.command == "import":
            importer = ImportService(db)
            try:
                job = importer.prepare(
                    args.json_path,
                    args.source_root,
                    args.scope,
                    args.target_chat_id,
                    "prefer_imported" if args.prefer_imported else "preserve",
                    args.create_new,
                )
                result = importer.run(job)
                # Do not print chat names, source paths, IDs, or message contents.
                report = {
                    key: result[key]
                    for key in (
                        "state",
                        "processed",
                        "added",
                        "unchanged",
                        "updated",
                        "conflicts",
                        "missing_media",
                        "invalid_media",
                        "error",
                        "warnings",
                    )
                }
                print(json.dumps(report, ensure_ascii=False, indent=2))
                if result["state"] != "completed":
                    sys.exit(1)
            finally:
                importer.shutdown()
        elif args.command == "doctor":
            print(json.dumps(doctor(db), ensure_ascii=False, indent=2))
        elif args.command == "rebuild":
            with FileLock(db.workspace / ".writer.lock", timeout=0):
                db.rebuild()
            print("FTS5 перестроен.")
        elif args.command == "compact":
            with FileLock(db.workspace / ".writer.lock", timeout=0):
                db.compact()
            print("База уплотнена.")
        else:
            print("Workspace создан. Устройство: CPU. Модели не загружаются.")
    except (UserError, OSError) as exc:
        print(
            str(exc) if isinstance(exc, UserError) else "Ошибка доступа к локальным файлам.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
