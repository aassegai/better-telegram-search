"""Transactional merge shared by Desktop exports and the Telegram source."""

import json
import time
from contextlib import nullcontext

from telegram_search.shared.errors import UserError
from telegram_search.shared.text import (
    content_hash,
    flatten_text,
    normalize_text,
    serialize,
    timestamp,
)
from telegram_search.storage.generations import bump_revision, invalidate_segments, utc_day


class CanonicalMessageWriter:
    def __init__(self, db):
        self.db = db

    def apply(self, job: dict, prepared: list, connection=None) -> dict:
        counters = dict.fromkeys(
            (
                "processed",
                "added",
                "unchanged",
                "updated",
                "conflicts",
                "missing_media",
                "invalid_media",
            ),
            0,
        )
        outcomes = []
        with self.db.connect() if connection is None else nullcontext(connection) as conn:
            changed_days = set()
            revision_changed = False
            for message, media in prepared:
                if type(message.get("id")) is not int:
                    raise UserError("У сообщения отсутствует числовой ID.")
                message_id = message["id"]
                if conn.execute(
                    "SELECT 1 FROM telegram_tombstones WHERE chat_id=? AND message_id=?",
                    (job["chat_id"], message_id),
                ).fetchone():
                    counters["processed"] += 1
                    counters["conflicts"] += 1
                    self._conflict(conn, job, message, media, None, "remote_deleted")
                    outcomes.append("conflict")
                    continue
                date = timestamp(message)
                edited = timestamp(message, "edited")
                signature = content_hash(message, media)
                old = conn.execute(
                    "SELECT * FROM messages WHERE chat_id=? AND message_id=?",
                    (job["chat_id"], message_id),
                ).fetchone()
                counters["processed"] += 1
                counters["missing_media"] += sum(item["status"] == "missing" for item in media)
                counters["invalid_media"] += sum(
                    item["status"] in {"corrupt", "unsafe"} for item in media
                )
                same_content = old and (
                    old["content_hash"] == signature
                    or self._same_content(conn, old, message, media)
                )
                if same_content:
                    counters["unchanged"] += 1
                    outcomes.append("unchanged")
                    if edited and edited > (old["edited_timestamp"] or 0):
                        conn.execute(
                            "UPDATE messages SET edited_timestamp=? WHERE rowid=?",
                            (edited, old["rowid"]),
                        )
                        revision_changed = True
                else:
                    if old:
                        previous_edit = old["edited_timestamp"]
                        newer = edited is not None and edited > (previous_edit or 0)
                        older = (
                            edited is not None
                            and previous_edit is not None
                            and edited < previous_edit
                        )
                        api_owned = conn.execute(
                            "SELECT 1 FROM telegram_message_provenance WHERE chat_id=? "
                            "AND message_id=?",
                            (job["chat_id"], message_id),
                        ).fetchone()
                        protected = api_owned and job.get("source") != "telegram" and not newer
                        if protected or (
                            not job.get("allow_older", False)
                            and (older or (not newer and job["policy"] != "prefer_imported"))
                        ):
                            counters["conflicts"] += 1
                            self._conflict(
                                conn,
                                job,
                                message,
                                media,
                                old,
                                "older_revision" if older else "uncertain_revision",
                            )
                            outcomes.append("conflict")
                            continue
                        counters["updated"] += 1
                        outcomes.append("updated")
                        self._prune_media(conn, job["chat_id"], message_id, media)
                    else:
                        counters["added"] += 1
                        outcomes.append("added")
                    revision_changed = True
                    changed_days.add(utc_day(date))
                    if old:
                        changed_days.add(utc_day(old["timestamp"]))
                    text = flatten_text(message.get("text"))
                    kind = message.get("type", "message")
                    conn.execute(
                        "INSERT INTO messages(chat_id,message_id,timestamp,edited_timestamp,"
                        "author_id,author,kind,text,text_normalized,reply_to,has_photo,content_hash,"
                        "raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(chat_id,message_id) DO UPDATE SET "
                        "timestamp=excluded.timestamp,edited_timestamp=excluded.edited_timestamp,"
                        "author_id=excluded.author_id,author=excluded.author,kind=excluded.kind,"
                        "text=excluded.text,text_normalized=excluded.text_normalized,"
                        "reply_to=excluded.reply_to,has_photo=excluded.has_photo,"
                        "content_hash=excluded.content_hash,raw_json=excluded.raw_json",
                        (
                            job["chat_id"],
                            message_id,
                            date,
                            edited,
                            message.get("from_id") or message.get("actor_id"),
                            message.get("from")
                            or message.get("actor")
                            or (old["author"] if old else message.get("from_id", "")),
                            kind,
                            text,
                            normalize_text(text) if kind == "message" else "",
                            message.get("reply_to_message_id"),
                            int("photo" in message),
                            signature,
                            serialize(message),
                        ),
                    )
                for item in media:
                    if item.get("preserved"):
                        continue
                    prior_ref = conn.execute(
                        "SELECT sha256,status FROM media_refs WHERE chat_id=? AND message_id=? "
                        "AND source_root_id=? AND relative_path=?",
                        (job["chat_id"], message_id, job["source_root_id"], item["relative_path"]),
                    ).fetchone()
                    if (
                        not prior_ref
                        or prior_ref["status"] != item["status"]
                        or (item["sha256"] and prior_ref["sha256"] != item["sha256"])
                    ):
                        revision_changed = True
                        changed_days.add(utc_day(date))
                    if item["sha256"]:
                        conn.execute(
                            "INSERT OR IGNORE INTO media_blobs VALUES(?,?)",
                            (item["sha256"], item["size"]),
                        )
                    conn.execute(
                        "INSERT INTO media_refs(chat_id,message_id,source_root_id,relative_path,"
                        "kind,sha256,status) VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(chat_id,message_id,source_root_id,relative_path) "
                        "DO UPDATE SET sha256=COALESCE(excluded.sha256,media_refs.sha256),"
                        "status=excluded.status",
                        (
                            job["chat_id"],
                            message_id,
                            job["source_root_id"],
                            item["relative_path"],
                            item["kind"],
                            item["sha256"],
                            item["status"],
                        ),
                    )
            assignments = ",".join(f"{key}={key}+?" for key in counters)
            if job.get("record_counters", True):
                conn.execute(
                    f"UPDATE imports SET {assignments} WHERE id=?", (*counters.values(), job["id"])
                )
            if revision_changed:
                bump_revision(conn, job["chat_id"])
            invalidate_segments(conn, job["chat_id"], changed_days, job.get("reason", "import"))
        return {**counters, "outcomes": outcomes}

    def classify(self, conn, chat_id: str, message: dict, media: list[dict], policy: str):
        if type(message.get("id")) is not int:
            raise UserError("У сообщения отсутствует числовой ID.")
        timestamp(message)
        if conn.execute(
            "SELECT 1 FROM telegram_tombstones WHERE chat_id=? AND message_id=?",
            (chat_id, message["id"]),
        ).fetchone():
            return "conflicts", "remote_deleted"
        edited = timestamp(message, "edited")
        old = conn.execute(
            "SELECT * FROM messages WHERE chat_id=? AND message_id=?", (chat_id, message["id"])
        ).fetchone()
        if old is None:
            return "added", None
        if old["content_hash"] == content_hash(message, media) or self._same_content(
            conn, old, message, media
        ):
            return "unchanged", None
        previous = old["edited_timestamp"]
        if edited is not None and previous is not None and edited < previous:
            return "conflicts", "older_revision"
        if conn.execute(
            "SELECT 1 FROM telegram_message_provenance WHERE chat_id=? AND message_id=?",
            (chat_id, message["id"]),
        ).fetchone() and not (edited and edited > (previous or 0)):
            return "conflicts", "uncertain_revision"
        if (edited is not None and edited > (previous or 0)) or policy == "prefer_imported":
            return "updated", None
        return "conflicts", "uncertain_revision"

    @staticmethod
    def _prune_media(conn, chat_id: str, message_id: int, incoming: list[dict]) -> None:
        for ref in conn.execute(
            "SELECT * FROM media_refs WHERE chat_id=? AND message_id=?", (chat_id, message_id)
        ).fetchall():
            compatible = any(
                item["kind"] == ref["kind"]
                and (
                    (item["sha256"] is not None and item["sha256"] == ref["sha256"])
                    or (item["sha256"] is None and item["relative_path"] == ref["relative_path"])
                )
                for item in incoming
            )
            if not compatible:
                conn.execute("DELETE FROM media_refs WHERE id=?", (ref["id"],))

    @staticmethod
    def _same_content(conn, old, incoming: dict, media: list[dict]) -> bool:
        # A restored file is not an edit; identical content may have a different filename.
        if content_hash(json.loads(old["raw_json"]), []) != content_hash(incoming, []):
            return False
        refs = conn.execute(
            "SELECT kind,relative_path,sha256 FROM media_refs WHERE chat_id=? AND message_id=?",
            (old["chat_id"], old["message_id"]),
        ).fetchall()
        if {ref["kind"] for ref in refs} != {item["kind"] for item in media}:
            return False
        for item in media:
            same_kind = [ref for ref in refs if ref["kind"] == item["kind"]]
            known_hashes = {ref["sha256"] for ref in same_kind if ref["sha256"] is not None}
            if item["sha256"] is not None and known_hashes:
                if item["sha256"] not in known_hashes:
                    return False
            elif not any(ref["relative_path"] == item["relative_path"] for ref in same_kind):
                return False
        return True

    @staticmethod
    def _conflict(conn, job, message, media, old, reason):
        if job.get("record_counters", True):
            # A tombstone report must not retain the deleted message body.
            conn.execute(
                "INSERT INTO import_conflicts(import_id,message_id,reason,incoming_json,"
                "base_content_hash,incoming_media_json) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(import_id,message_id) DO NOTHING",
                (
                    job["id"],
                    message["id"],
                    reason,
                    serialize({"id": message["id"], "date_unixtime": str(timestamp(message))})
                    if reason == "remote_deleted"
                    else serialize(message),
                    old["content_hash"] if old else None,
                    "[]" if reason == "remote_deleted" else serialize(media),
                ),
            )
        else:
            conn.execute(
                "INSERT INTO telegram_sync_conflicts VALUES(?,?,?,?) "
                "ON CONFLICT(chat_id,message_id) DO UPDATE SET reason=excluded.reason,"
                "observed_at=excluded.observed_at",
                (job["chat_id"], message["id"], reason, int(time.time())),
            )
