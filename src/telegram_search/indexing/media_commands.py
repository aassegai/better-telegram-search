import threading

from filelock import FileLock

from telegram_search.indexing.media import MediaService
from telegram_search.indexing.service import SemanticService
from telegram_search.shared.errors import UserError


def media_command(db, args):
    with FileLock(db.workspace / ".writer.lock", timeout=0):
        lock = threading.RLock()
        semantic = SemanticService(db, lock, start_background=False)
        media = MediaService(db, lock, semantic, start_background=False)
        try:
            if args.command == "prepare-media":
                media.prepare(
                    args.kind, offline=args.offline, profile=args.profile, reindex=args.reindex
                )
                media.preparation.join()
                if media.status()["preparation_state"] != "ready":
                    raise UserError("Не удалось подготовить модель медиа.")
            else:
                if args.retry:
                    media.control("retry")
                media.control("resume")
                while True:
                    worked = media._ocr_run()
                    worked |= media._image_batch()
                    worked |= media._ocr_embeddings()
                    if not worked:
                        break
            return media.status()
        finally:
            media.shutdown()
            semantic.shutdown()
