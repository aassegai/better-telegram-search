import argparse
import json
import os
import sys
import threading
from pathlib import Path

import uvicorn
from filelock import FileLock, Timeout

from telegram_search.config.browser import open_browser
from telegram_search.config.diagnostics import doctor
from telegram_search.config.runtime import default_workspace
from telegram_search.ingestion.importer import ImportService
from telegram_search.shared.errors import UserError
from telegram_search.storage.database import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальный поиск по Telegram Desktop (ONNX)")
    parser.add_argument("--workspace", type=Path, default=default_workspace())
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
    load.add_argument(
        "--preview", action="store_true", help="Только проверить, без изменения сообщений"
    )
    prepare = commands.add_parser("prepare-model")
    prepare.add_argument("--profile", choices=["small", "base", "berta"])
    prepare.add_argument("--reindex", action="store_true")
    prepare.add_argument("--offline", action="store_true")
    prepare.add_argument("--repair", action="store_true")
    prepare.add_argument("--local-bundle", type=Path)
    indexing = commands.add_parser("index")
    indexing.add_argument("--retry", action="store_true")
    media = commands.add_parser("prepare-media")
    media.add_argument("--kind", choices=["ocr", "images"], required=True)
    media.add_argument("--offline", action="store_true")
    media.add_argument("--profile", choices=["clip", "siglip2"])
    media.add_argument("--reindex", action="store_true")
    media_index = commands.add_parser("index-media")
    media_index.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    if getattr(args, "run_workspace", None) is not None:
        args.workspace = args.run_workspace
    db = Database(args.workspace)
    try:
        if args.command == "run" and os.environ.get("BTS_UPDATE_HEALTHCHECK"):
            from telegram_search.updates.installer import wait_for_startup

            wait_for_startup(args.workspace, os.environ["BTS_UPDATE_HEALTHCHECK"])
        db.initialize()
        if args.command == "run":
            from telegram_search.backend.api import create_app

            if not 1 <= args.port <= 65535:
                raise UserError("Порт должен быть от 1 до 65535.")
            app = create_app(args.workspace, args.frontend)
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=args.port,
                    workers=1,
                    access_log=False,
                )
            )
            app.state.update_shutdown = lambda: setattr(server, "should_exit", True)
            app.state.update_port = args.port
            if not args.no_browser:

                def open_when_ready():
                    import time

                    while not server.started and not server.should_exit:
                        time.sleep(0.1)
                    if server.started:
                        open_browser(f"http://127.0.0.1:{args.port}")

                threading.Thread(target=open_when_ready, daemon=True).start()
            server.run()
        elif args.command == "import":
            importer = ImportService(db)
            try:
                prepare = importer.previews.create if args.preview else importer.prepare
                job = prepare(
                    args.json_path,
                    args.source_root,
                    args.scope,
                    args.target_chat_id,
                    "prefer_imported" if args.prefer_imported else "preserve",
                    args.create_new,
                )
                result = importer.previews.run(job) if args.preview else importer.run(job)
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
                if result["state"] != ("ready" if args.preview else "completed"):
                    sys.exit(1)
            finally:
                importer.shutdown()
        elif args.command == "doctor":
            print(json.dumps(doctor(db), ensure_ascii=False, indent=2))
        elif args.command == "rebuild":
            with FileLock(db.workspace / ".writer.lock", timeout=0):
                db.rebuild()
            print("FTS5 перестроен.")
        elif args.command in {"prepare-media", "index-media"}:
            from telegram_search.indexing.media_commands import media_command

            print(json.dumps(media_command(db, args), ensure_ascii=False, indent=2))
        elif args.command in {"prepare-model", "index", "compact"}:
            from telegram_search.indexing.commands import semantic_command

            print(json.dumps(semantic_command(db, args), ensure_ascii=False, indent=2))
        else:
            print("Workspace создан. Устройство: CPU. Модели не загружаются.")
    except (UserError, OSError, Timeout) as exc:
        print(
            str(exc)
            if isinstance(exc, UserError)
            else (
                "Workspace занят другим процессом."
                if isinstance(exc, Timeout)
                else "Ошибка доступа к локальным файлам."
            ),
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
