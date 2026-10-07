import threading

from filelock import FileLock

from telegram_search.indexing.service import SemanticService
from telegram_search.indexing.worker import SegmentWorker
from telegram_search.shared.errors import UserError


def semantic_command(db, args):
    with FileLock(db.workspace / ".writer.lock", timeout=0):
        service = SemanticService(db, threading.RLock(), start_background=False)
        try:
            if args.command == "prepare-model":
                service.prepare(
                    args.profile,
                    reindex=args.reindex,
                    offline=args.offline,
                    repair=args.repair,
                    local_bundle=args.local_bundle,
                )
                service.preparation.join()
                if service.status()["preparation_state"] != "ready":
                    raise UserError(service.status()["error"] or "Модель не подготовлена.")
            elif args.command == "index":
                if service.encoder is None:
                    raise UserError("Сначала выполните prepare-model.")
                service.control("retry" if args.retry else "resume")
                service.cleanup()
                worker = SegmentWorker(
                    db,
                    service.encoder,
                    service._vector_store(),
                    service.lock,
                    batch_size=db.settings.embedding_batch,
                    should_stop=service.stop.is_set,
                )
                while True:
                    with db.connect() as conn:
                        work = conn.execute(
                            "SELECT w.id FROM index_work w JOIN chats c ON c.id=w.chat_id "
                            "WHERE w.state='pending' AND c.text_paused=0 "
                            "ORDER BY w.created_at,w.id LIMIT 1"
                        ).fetchone()
                    if not work:
                        break
                    result = worker.run(work[0])
                    if result["state"] == "failed":
                        raise UserError(result["error"])
                    if result["state"] in {"pending", "paused", "unavailable"}:
                        raise UserError("Индексирование остановлено до публикации сегмента.")
            else:
                service.cleanup(compact=True)
                db.compact()
            status = service.status()
            return {
                key: status[key]
                for key in (
                    "preparation_state",
                    "profile",
                    "ready_segments",
                    "total_segments",
                    "works",
                )
            }
        finally:
            service.shutdown()
