"""Guard terminal CPU-job publication against transient/permanent SQLite errors."""

import sqlite3

from telegram_search.storage.contention import database_busy


def publish_result(service, job, write):
    attempted_failure = False
    while True:
        try:
            with service.lock:
                if job.cancelled.is_set() or service.stop.is_set():
                    job.state, job.error = "cancelled", None
                with service.db.connection_timeout(0.2), service.db.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    write(conn)
                service.jobs.pop(job.id, None)
            break
        except KeyError:
            break
        except sqlite3.Error as exc:
            if database_busy(exc):
                if service.stop.wait(0.1):
                    break  # Restart recovery marks durable work interrupted.
                continue
            if not attempted_failure:
                attempted_failure = True
                job.state, job.error, job.summary = "failed", "result_save_failed", None
                continue
            with service.lock:
                service.persistence_error = True
                service.queue.clear()
                for owned in service.jobs.values():
                    owned.state, owned.error, owned.summary = "failed", "result_save_failed", None
                    owned.publish_failed = True
                    owned.progress["phase"] = "failed"
            return
    with service.lock:
        service.jobs.pop(job.id, None)
