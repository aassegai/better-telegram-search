"""Read-only MTProto adapter. Importing the application never imports Telethon."""

import logging
from datetime import UTC, datetime

from telegram_search import __version__
from telegram_search.telegram_sync.models import NormalizedMessage, Peer, SourceFailure


def safe_failure(exc: Exception) -> SourceFailure:
    name = type(exc).__name__
    if isinstance(exc, SourceFailure):
        return exc
    if name in {"FloodWaitError", "FloodPremiumWaitError", "SlowModeWaitError"}:
        return SourceFailure("waiting_rate_limit", int(getattr(exc, "seconds", 60)))
    if name in {
        "AuthKeyUnregisteredError",
        "SessionRevokedError",
        "UserDeactivatedError",
        "AuthKeyDuplicatedError",
        "SessionExpiredError",
    }:
        return SourceFailure("auth_required")
    if name in {"FileReferenceExpiredError", "FileReferenceInvalidError"}:
        return SourceFailure("network")
    if name in {"PhoneCodeInvalidError", "PhoneCodeExpiredError", "PhoneCodeEmptyError"}:
        return SourceFailure("invalid_code")
    if name == "PasswordHashInvalidError":
        return SourceFailure("invalid_password")
    if name in {"PhoneNumberInvalidError", "PhoneNumberBannedError"}:
        return SourceFailure("phone_invalid")
    if name in {"ChannelPrivateError", "ChatAdminRequiredError", "PeerIdInvalidError"}:
        return SourceFailure("access_denied")
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return SourceFailure("network")
    return SourceFailure("unexpected")


def peer_of(entity) -> Peer:
    kind = type(entity).__name__
    if kind in {"User", "UserEmpty", "PeerUser", "InputPeerUser"}:
        identity = getattr(entity, "user_id", None) or entity.id
        peer_type = "user"
    elif kind in {"Chat", "ChatForbidden", "PeerChat", "InputPeerChat"}:
        identity = getattr(entity, "chat_id", None) or entity.id
        peer_type = "chat"
    else:
        identity = getattr(entity, "channel_id", None) or entity.id
        peer_type = "channel"
    name = getattr(entity, "title", None) or " ".join(
        filter(None, (getattr(entity, "first_name", None), getattr(entity, "last_name", None)))
    )
    return Peer(peer_type, identity, getattr(entity, "access_hash", None), name)


def normalize_message(message) -> NormalizedMessage:
    peer = peer_of(message.peer_id)
    sender = peer_of(message.from_id) if message.from_id else peer
    data = {
        "id": message.id,
        "type": "service" if getattr(message, "action", None) else "message",
        "date_unixtime": str(int(message.date.timestamp())),
        "from_id": f"{sender.kind}{sender.id}",
        "from": "",
        "text": getattr(message, "message", None) or "",
    }
    cached_sender = getattr(message, "sender", None)
    if cached_sender is not None:
        data["from"] = peer_of(cached_sender).name
    if getattr(message, "edit_date", None):
        data["edited_unixtime"] = str(int(message.edit_date.timestamp()))
    if getattr(message, "reply_to_msg_id", None):
        data["reply_to_message_id"] = message.reply_to_msg_id
    if getattr(message, "grouped_id", None):
        data["grouped_id"] = str(message.grouped_id)
    entities = []
    names = {
        "Bold": "bold",
        "Italic": "italic",
        "Underline": "underline",
        "Strike": "strikethrough",
        "Spoiler": "spoiler",
        "Code": "code",
        "Pre": "pre",
        "TextUrl": "text_link",
        "MentionName": "text_mention",
        "Mention": "mention",
        "Hashtag": "hashtag",
        "Cashtag": "cashtag",
        "BotCommand": "bot_command",
        "Url": "link",
        "Email": "email",
        "Phone": "phone_number",
        "CustomEmoji": "custom_emoji",
        "Blockquote": "blockquote",
    }
    for entity in getattr(message, "entities", None) or []:
        kind = names.get(type(entity).__name__.removeprefix("MessageEntity"))
        if not kind:
            continue
        value = {"type": kind, "offset": entity.offset, "length": entity.length}
        for attr, key in (
            ("url", "href"),
            ("user_id", "user_id"),
            ("language", "language"),
            ("document_id", "document_id"),
        ):
            if getattr(entity, attr, None) is not None:
                value[key] = str(getattr(entity, attr))
        entities.append(value)
    data["text_entities"] = entities
    photo, document = getattr(message, "photo", None), getattr(message, "document", None)
    identity, downloadable, size = None, False, None
    if photo:
        identity, downloadable = f"photo:{photo.id}", True
        sizes = getattr(photo, "sizes", [])
        size = max((getattr(item, "size", 0) or 0 for item in sizes), default=0) or None
        data["photo"] = "remote"
    elif document:
        identity, size = f"document:{document.id}", getattr(document, "size", None)
        mime = getattr(document, "mime_type", "")
        downloadable = mime in {"image/jpeg", "image/png", "image/webp", "image/gif"}
        data["mime_type"] = mime
        data["media_type"] = "image_document" if downloadable else "document"
        if downloadable:
            data["photo"] = "remote"
    elif getattr(message, "media", None):
        data["media_type"] = type(message.media).__name__
    if getattr(message, "action", None):
        data["action"] = type(message.action).__name__
    return NormalizedMessage(peer, data, identity, downloadable, size)


class TelethonAdapter:
    def __init__(self, session_path, settings):
        try:
            from telethon import TelegramClient, events
            from telethon.sessions import SQLiteSession
        except ImportError as exc:
            raise SourceFailure("runtime_missing") from exc
        logger = logging.getLogger("telegram_search.private_mtproto")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        logger.setLevel(logging.CRITICAL)
        session = SQLiteSession(str(session_path))
        session.save_entities = False
        self.client = TelegramClient(
            session,
            settings.api_id,
            settings.api_hash,
            sequential_updates=True,
            flood_sleep_threshold=0,
            request_retries=1,
            connection_retries=1,
            retry_delay=1,
            timeout=15,
            entity_cache_limit=100,
            base_logger=logger,
            device_model="Better Telegram Search",
            app_version=__version__,
        )
        self.events = events
        self.handler = None
        self.dialog_offsets = {"": None}

    def set_handler(self, callback):
        self.handler = callback

        async def changed(event):
            await callback("raw_message", event.message, None)

        async def deleted(event):
            peer = Peer.from_marked(event.chat_id) if event.chat_id else None
            await callback("delete", peer, list(event.deleted_ids))

        self.client.add_event_handler(changed, self.events.NewMessage())
        self.client.add_event_handler(changed, self.events.MessageEdited())
        self.client.add_event_handler(deleted, self.events.MessageDeleted())

    async def connect(self):
        await self.client.connect()

    async def disconnect(self):
        try:
            await self.client.disconnect()
        finally:
            self.client.session.close()

    def is_connected(self):
        return self.client.is_connected()

    async def authorized(self):
        return await self.client.is_user_authorized()

    async def account_id(self):
        return (await self.client.get_me()).id

    async def request_code(self, phone):
        result = await self.client.send_code_request(phone)
        return result.phone_code_hash

    async def login_code(self, phone, code, code_hash):
        from telethon.errors import SessionPasswordNeededError

        try:
            await self.client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
        except SessionPasswordNeededError:
            return False
        return True

    async def login_password(self, password):
        await self.client.sign_in(password=password)

    async def logout(self):
        return bool(await self.client.log_out())

    async def catch_up(self):
        await self.client.catch_up()

    def input_peer(self, peer):
        from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser

        if peer.kind == "chat":
            return InputPeerChat(peer.id)
        if peer.access_hash is None:
            raise SourceFailure("access_denied")
        return (InputPeerUser if peer.kind == "user" else InputPeerChannel)(
            peer.id, peer.access_hash
        )

    async def dialogs(self, cursor="", query=""):
        import secrets

        from telethon.tl.types import InputPeerEmpty

        if cursor not in self.dialog_offsets:
            raise SourceFailure("access_denied")
        offset = self.dialog_offsets[cursor]
        kwargs = (
            {}
            if not offset
            else dict(offset_date=offset[0], offset_id=offset[1], offset_peer=offset[2])
        )
        # Pagination is server-order, not an unbounded search through every dialog.
        page = await self.client.get_dialogs(limit=50, ignore_migrated=True, **kwargs)
        peers = [peer_of(dialog.entity) for dialog in page]
        next_cursor = None
        if len(page) == 50:
            last = page[-1]
            next_cursor = secrets.token_urlsafe(16)
            self.dialog_offsets[next_cursor] = (
                last.date,
                last.message.id if last.message else 0,
                last.input_entity or InputPeerEmpty(),
            )
            if len(self.dialog_offsets) > 32:
                self.dialog_offsets.pop(next(key for key in self.dialog_offsets if key != ""))
        return [peer for peer in peers if query.casefold() in peer.name.casefold()], next_cursor

    async def latest(self, peer):
        values = await self.client.get_messages(self.input_peer(peer), limit=1)
        return values[0].id if values else 0

    async def before(self, peer, since):
        values = await self.client.get_messages(
            self.input_peer(peer), limit=1, offset_date=datetime.fromtimestamp(since, UTC)
        )
        return values[0].id if values else 0

    async def page(self, peer, after, upper, limit=100):
        return [
            normalize_message(message)
            async for message in self.client.iter_messages(
                self.input_peer(peer),
                min_id=after,
                max_id=upper + 1,
                reverse=True,
                limit=limit,
            )
            if after < message.id <= upper
        ]

    async def recent(self, peer, before, since, upper, limit=100):
        # Descending bounded tail; persist before between budget-limited runs.
        values = await self.client.get_messages(
            self.input_peer(peer),
            offset_id=before or 0,
            max_id=upper + 1,
            limit=limit,
        )
        return [
            normalize_message(message)
            for message in values
            if message.date and int(message.date.timestamp()) >= since
        ]

    async def get(self, peer, ids):
        values = await self.client.get_messages(self.input_peer(peer), ids=ids)
        return [
            normalize_message(message)
            for message in values
            if message and getattr(message, "date", None)
        ]

    async def download(self, peer, message_id, identity):
        # Refresh the message and its expiring file reference before every attempt.
        values = await self.client.get_messages(self.input_peer(peer), ids=[message_id])
        if not values or not values[0] or not getattr(values[0], "date", None):
            raise SourceFailure("remote_unavailable")
        message = values[0]
        normalized = normalize_message(message)
        if normalized.media_identity != identity or not normalized.downloadable:
            raise SourceFailure("remote_unavailable")
        async for block in self.client.iter_download(message.media, request_size=256 * 1024):
            yield bytes(block)
