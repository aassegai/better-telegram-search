"""Durable content/version queue. Claims are short SQLite transactions, never inference."""

import uuid


class OcrQueue:
    def __init__(self, db):
        self.db = db
        # The workspace writer lease guarantees one application owns this queue.
        with db.connect() as conn:
            conn.execute(
                "UPDATE ocr_work SET state='pending',claim_token=NULL WHERE state='running'"
            )

    @staticmethod
    def activate(conn, version):
        active = conn.execute("SELECT version FROM ocr_queue_version WHERE id=1").fetchone()
        if active and active[0] == version:
            return
        conn.execute(
            "INSERT INTO ocr_queue_version VALUES(1,?) "
            "ON CONFLICT(id) DO UPDATE SET version=excluded.version",
            (version,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO ocr_work(sha256,version,state) "
            "SELECT DISTINCT r.sha256,?,CASE o.state WHEN 'ready' THEN 'done' "
            "WHEN 'failed' THEN 'failed' ELSE 'pending' END FROM media_refs r "
            "LEFT JOIN ocr_cache o ON o.sha256=r.sha256 AND o.version=? "
            "WHERE r.kind='photo' AND r.status='ready'",
            (version, version),
        )

    def claim(self, version, limit=1, *, configured=False, with_tokens=False, exclude=()):
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.activate(conn, version)
            if conn.execute("SELECT ocr_paused FROM media_state WHERE id=1").fetchone()[0]:
                return ([], self.db.settings.ocr_region_batch_size) if configured else []
            scope, parameters = "", ()
            exclusion = (
                (" AND w.sha256 NOT IN (" + ",".join("?" for _ in exclude) + ")") if exclude else ""
            )
            if configured:
                chat = conn.execute(
                    "SELECT c.id,c.ocr_batch,c.ocr_region_batch FROM ocr_work w "
                    "JOIN media_refs r ON r.sha256=w.sha256 JOIN chats c ON c.id=r.chat_id "
                    "WHERE w.version=? AND w.state='pending' AND r.kind='photo' "
                    "AND r.status='ready' AND c.ocr_paused=0"
                    + exclusion
                    + " ORDER BY w.sha256,c.id LIMIT 1",
                    (version, *exclude),
                ).fetchone()
                if not chat:
                    return [], self.db.settings.ocr_region_batch_size
                limit = min(
                    limit,
                    chat["ocr_batch"]
                    or max(self.db.settings.ocr_batch_size, self.db.settings.ocr_cpu_workers),
                )
                region_batch = chat["ocr_region_batch"] or self.db.settings.ocr_region_batch_size
                scope, parameters = " AND c.id=?", (chat["id"],)
            rows = conn.execute(
                "SELECT w.sha256 FROM ocr_work w WHERE w.version=? AND w.state='pending' "
                "AND EXISTS (SELECT 1 FROM media_refs r JOIN chats c ON c.id=r.chat_id "
                "WHERE r.sha256=w.sha256 AND r.status='ready' AND r.kind='photo' "
                "AND c.ocr_paused=0" + scope + ")" + exclusion + " ORDER BY w.sha256 LIMIT ?",
                (version, *parameters, *exclude, min(4, max(1, limit))),
            ).fetchall()
            token = uuid.uuid4().hex
            conn.executemany(
                "UPDATE ocr_work SET state='running',attempts=attempts+1,claim_token=? "
                "WHERE version=? AND sha256=?",
                ((token, version, row[0]) for row in rows),
            )
        values = [(row[0], token) if with_tokens else row[0] for row in rows]
        return (values, region_batch) if configured else values

    def release(self, version, claims):
        with self.db.connect() as conn:
            conn.executemany(
                "UPDATE ocr_work SET state='pending',claim_token=NULL "
                "WHERE version=? AND sha256=? AND claim_token=? AND state='running'",
                ((version, sha, token) for sha, token in claims),
            )
