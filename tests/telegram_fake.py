"""Synthetic MTProto source: no network, phone delivery, or real account access."""

import asyncio
import io
import time

from PIL import Image

from telegram_search.telegram_sync.models import NormalizedMessage, Peer


def remote(mid, text=None, *, peer=None, date=None, edited=None, photo=None, **extra):
    peer = peer or Peer("user", 100, 123, "Synthetic")
    data = {
        "id": mid,
        "type": "message",
        "date_unixtime": str(date or int(time.time())),
        "from_id": "user1",
        "from": "Synthetic author",
        "text": text or f"message {mid}",
        **extra,
    }
    if edited:
        data["edited_unixtime"] = str(edited)
    if photo:
        data["photo"] = "remote"
    return NormalizedMessage(peer, data, f"photo:{photo}" if photo else None, bool(photo))


def image_bytes(color="red"):
    output = io.BytesIO()
    Image.new("RGB", (12, 12), color).save(output, format="PNG")
    return output.getvalue()


class FakeAdapter:
    def __init__(self, path=None, settings=None):
        self.connected = False
        self.authenticated = False
        self.handler = None
        self.need_password = False
        self.account = 999
        self.peers = [Peer("user", 100, 123, "Synthetic")]
        self.messages = {}
        self.page_hook = None
        self.error = None
        self.photo_bytes = image_bytes()
        self.download_error = None
        self.page_calls = []
        self.concurrent_pages = self.max_concurrent_pages = 0
        self.revoked = False

    def set_handler(self, callback):
        self.handler = callback

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def authorized(self):
        return self.authenticated

    async def account_id(self):
        return self.account

    async def request_code(self, phone):
        return "synthetic-code-hash"

    async def login_code(self, phone, code, code_hash):
        self.authenticated = not self.need_password
        return self.authenticated

    async def login_password(self, password):
        self.authenticated = True

    async def logout(self):
        self.revoked = True
        self.authenticated = False
        return True

    async def catch_up(self):
        pass

    async def dialogs(self, cursor="", query=""):
        return [peer for peer in self.peers if query.casefold() in peer.name.casefold()], None

    def for_peer(self, peer):
        return [message for message in self.messages.values() if message.peer.key == peer.key]

    async def latest(self, peer):
        if self.error:
            raise self.error
        return max((message.data["id"] for message in self.for_peer(peer)), default=0)

    async def before(self, peer, since):
        return max(
            (
                message.data["id"]
                for message in self.for_peer(peer)
                if int(message.data["date_unixtime"]) < since
            ),
            default=0,
        )

    async def get(self, peer, ids):
        return [message for message in self.for_peer(peer) if message.data["id"] in ids]

    async def page(self, peer, after, upper, limit=100):
        self.concurrent_pages += 1
        self.max_concurrent_pages = max(self.max_concurrent_pages, self.concurrent_pages)
        try:
            self.page_calls.append((after, upper))
            if self.page_hook:
                hook, self.page_hook = self.page_hook, None
                await hook()
            if self.error:
                raise self.error
            await asyncio.sleep(0)
            return sorted(
                (message for message in self.for_peer(peer) if after < message.data["id"] <= upper),
                key=lambda x: x.data["id"],
            )[:limit]
        finally:
            self.concurrent_pages -= 1

    async def recent(self, peer, before, since, upper, limit=100):
        return sorted(
            (
                message
                for message in self.for_peer(peer)
                if message.data["id"] <= upper
                and (not before or message.data["id"] < before)
                and int(message.data["date_unixtime"]) >= since
            ),
            key=lambda x: x.data["id"],
            reverse=True,
        )[:limit]

    async def download(self, peer, message_id, identity):
        if self.download_error:
            yield self.photo_bytes[:10]
            raise self.download_error
        yield self.photo_bytes
