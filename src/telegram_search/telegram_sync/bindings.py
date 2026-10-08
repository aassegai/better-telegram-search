"""Explicit, previewed dataset-to-account aliases with optimistic concurrency."""

import secrets
import time
import uuid
from datetime import UTC, datetime

from telegram_search.shared.errors import UserError
from telegram_search.shared.text import flatten_text, serialize, timestamp
from telegram_search.telegram_sync.adapter import safe_failure
from telegram_search.telegram_sync.models import ERRORS, desktop_peer
from telegram_search.telegram_sync.paths import owned_path


class DialogBindingService:
    def __init__(self, store, manager):
        self.store, self.manager = store, manager
        self.previews = {}

    async def preview(self, peer_key, chat_id=None, start_date=None):
        client = await self.manager.require_client()
        peer = self.manager.dialog_choices.get(peer_key)
        if not peer:
            raise UserError("Выберите диалог из списка Telegram заново.")
        account_id = self.manager.row()["account_user_id"]
        with self.store.db.connect() as conn:
            duplicate = conn.execute(
                "SELECT 1 FROM dialog_sync_bindings WHERE account_user_id=? AND peer_type=? AND "
                "peer_id=?",
                (account_id, peer.kind, peer.id),
            ).fetchone()
            if duplicate:
                raise UserError("Этот диалог уже подключён к синхронизации.")
            local = (
                conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
                if chat_id
                else None
            )
            if chat_id and not local:
                raise UserError("Выбранный диалог не найден.")
            rows = (
                conn.execute(
                    "SELECT * FROM messages WHERE chat_id=? ORDER BY message_id DESC LIMIT 20",
                    (chat_id,),
                ).fetchall()
                if chat_id
                else []
            )
            revision = local["revision"] if local else None
            expected = desktop_peer(local["external_id"], local["kind"]) if local else None
            baseline = max((row["message_id"] for row in rows), default=0)
        if expected and (expected.kind, expected.id) != (peer.kind, peer.id):
            raise UserError(
                "Тип или ID Telegram не совпадает с выгрузкой. Миграция группы требует отдельной "
                "привязки."
            )
        if not chat_id:
            try:
                since = int(datetime.fromisoformat(start_date).replace(tzinfo=UTC).timestamp())
                if not 0 <= since <= time.time() + 86400:
                    raise ValueError
            except (ValueError, TypeError):
                raise UserError("Для нового диалога выберите начальную дату загрузки.") from None
        else:
            since = None
        try:
            records = (
                await self.manager.rpc(client.get(peer, [row["message_id"] for row in rows]))
                if rows
                else []
            )
            if since is not None:
                baseline = await self.manager.rpc(client.before(peer, since))
        except Exception as exc:
            failure = safe_failure(exc)
            raise UserError(ERRORS.get(failure.code, ERRORS["unexpected"])) from None
        by_id = {record.data["id"]: record for record in records}
        matched, changed, mismatches = 0, 0, 0
        for row in rows:
            record = by_id.get(row["message_id"])
            if not record:
                continue  # Absence is neither identity proof nor deletion evidence.
            date_matches = timestamp(record.data) == row["timestamp"]
            author_matches = not row["author_id"] or row["author_id"] == record.data.get("from_id")
            text_matches = row["text"] == flatten_text(record.data.get("text"))
            media_matches = bool(row["has_photo"]) == record.downloadable
            different = not text_matches or not media_matches
            reliable_edit = timestamp(record.data, "edited") or row["edited_timestamp"]
            if not date_matches or not author_matches or (different and not reliable_edit):
                mismatches += 1
            elif different:
                changed += 1
            else:
                matched += 1
        token = secrets.token_urlsafe(24)
        self.previews = {
            key: value
            for key, value in self.previews.items()
            if value["expires"] > time.monotonic()
        }
        if len(self.previews) >= 16:
            self.previews.pop(next(iter(self.previews)))
        self.previews[token] = {
            "peer": peer,
            "account_id": account_id,
            "connection_id": self.manager.row()["id"],
            "chat_id": chat_id,
            "revision": revision,
            "baseline": baseline,
            "since": since,
            "mismatches": mismatches,
            "expires": time.monotonic() + 300,
        }
        return {
            "preview_token": token,
            "chat_id": chat_id,
            "revision": revision,
            "typed_id_match": expected is not None,
            "checked": len(records),
            "matched": matched,
            "edited": changed,
            "mismatches": mismatches,
            "unavailable": len(rows) - len(records),
            "baseline_id": baseline,
            "can_bind": mismatches == 0,
            "account_confirmation_required": True,
        }

    def apply(self, token, confirm_account, expected_revision):
        preview = self.previews.get(token)
        if not preview or preview["expires"] <= time.monotonic():
            raise UserError("Проверка привязки истекла. Повторите её.")
        if not confirm_account:
            raise UserError("Подтвердите, что выбрали нужный аккаунт и диалог Telegram.")
        if preview["mismatches"]:
            raise UserError("Перекрытие истории не совпало. Выберите правильный диалог.")
        peer, now = preview["peer"], int(time.time())
        scope = f"telegram:{preview['account_id']}:{peer.kind}"
        chat_id = preview["chat_id"] or str(
            uuid.uuid5(uuid.NAMESPACE_URL, serialize([scope, str(peer.id)]))
        )
        root_relative = f"data/media/telegram/{preview['connection_id']}"
        root = owned_path(self.store.db.workspace, root_relative)
        root.mkdir(parents=True, exist_ok=True)
        with self.store.lock, self.store.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            connection = conn.execute(
                "SELECT * FROM telegram_connections WHERE id=?", (preview["connection_id"],)
            ).fetchone()
            if (
                not connection
                or connection["account_user_id"] != preview["account_id"]
                or not connection["reconnect"]
            ):
                raise UserError("Аккаунт Telegram изменился. Повторите проверку.")
            if preview["chat_id"]:
                chat = conn.execute("SELECT revision FROM chats WHERE id=?", (chat_id,)).fetchone()
                if not chat or chat[0] != preview["revision"] or expected_revision != chat[0]:
                    raise UserError("Диалог изменился после проверки. Повторите привязку.")
                if conn.execute(
                    "SELECT 1 FROM dialog_sync_bindings WHERE chat_id=?", (chat_id,)
                ).fetchone():
                    raise UserError("Диалог уже связан с Telegram.")
            else:
                types = {
                    "user": "personal_chat",
                    "chat": "private_group",
                    "channel": "private_supergroup",
                }
                conn.execute(
                    "INSERT INTO chats(id,scope,external_id,name,kind,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        chat_id,
                        scope,
                        str(peer.id),
                        peer.name or peer.key,
                        types[peer.kind],
                        now,
                    ),
                )
            if conn.execute(
                "SELECT 1 FROM dialog_sync_bindings WHERE account_user_id=? AND peer_type=? AND "
                "peer_id=?",
                (preview["account_id"], peer.kind, peer.id),
            ).fetchone():
                raise UserError("Этот диалог уже подключён к синхронизации.")
            conn.execute(
                "INSERT OR IGNORE INTO source_roots(chat_id,relative_path,managed) VALUES(?,?,1)",
                (chat_id, root_relative),
            )
            root_id = conn.execute(
                "SELECT id FROM source_roots WHERE chat_id=? AND relative_path=?",
                (chat_id, root_relative),
            ).fetchone()[0]
            binding_id = str(uuid.uuid4())
            settings = self.manager.settings
            conn.execute(
                "INSERT INTO "
                "dialog_sync_bindings(id,chat_id,connection_id,account_user_id,peer_type,"
                "peer_id,access_hash,source_root_id,download_media,deletion_policy,reconcile_days) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    binding_id,
                    chat_id,
                    preview["connection_id"],
                    preview["account_id"],
                    peer.kind,
                    peer.id,
                    str(peer.access_hash) if peer.access_hash is not None else None,
                    root_id,
                    int(settings.download_photos),
                    settings.default_deletion_policy,
                    settings.reconcile_window_days,
                ),
            )
            conn.execute(
                "INSERT INTO "
                "dialog_sync_cursors(binding_id,baseline_id,scanned_through_id,coverage) "
                "VALUES(?,?,?,?)",
                (
                    binding_id,
                    preview["baseline"],
                    preview["baseline"],
                    "partial_export" if preview["chat_id"] else "from_start_date",
                ),
            )
        self.previews.pop(token, None)
        self.store.request(binding_id, "binding")
        return {"chat_id": chat_id, **self.store.status(chat_id)}

    def settings(self, chat_id, expected_revision, changes):
        with self.store.lock, self.store.db.connect() as conn:
            binding = conn.execute(
                "SELECT * FROM dialog_sync_bindings WHERE chat_id=?", (chat_id,)
            ).fetchone()
            if not binding or binding["revision"] != expected_revision:
                raise UserError("Настройки синхронизации изменились. Обновите карточку.")
            assignments = ",".join(f"{key}=?" for key in changes)
            if assignments:
                conn.execute(
                    f"UPDATE dialog_sync_bindings SET {assignments},revision=revision+1,"
                    "generation=generation+1,next_sync_at=0 WHERE id=?",
                    (*changes.values(), binding["id"]),
                )
                if changes.get("enabled") is False:
                    conn.execute(
                        "UPDATE sync_runs SET state='paused',finished_at=? WHERE binding_id=? "
                        "AND state IN ('queued','running','partial','waiting_rate_limit')",
                        (int(time.time()), binding["id"]),
                    )
                    conn.execute(
                        "UPDATE telegram_jobs SET state='cancelled',lease_until=0 WHERE "
                        "binding_id=? AND kind='scan' "
                        "AND state IN ('pending','running')",
                        (binding["id"],),
                    )
        return self.store.status(chat_id)

    def remove(self, chat_id, expected_revision):
        with self.store.lock, self.store.db.connect() as conn:
            row = conn.execute(
                "SELECT revision FROM dialog_sync_bindings WHERE chat_id=?", (chat_id,)
            ).fetchone()
            if not row or row[0] != expected_revision:
                raise UserError("Настройки синхронизации изменились. Обновите карточку.")
            conn.execute("DELETE FROM dialog_sync_bindings WHERE chat_id=?", (chat_id,))
        return {"binding": None}
