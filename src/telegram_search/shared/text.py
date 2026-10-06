import hashlib
import json
import unicodedata
from datetime import UTC, datetime

from telegram_search.shared.errors import UserError


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")


def flatten_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in value
            if isinstance(item, (str, dict))
        )
    return ""


def timestamp(message: dict, prefix: str = "date") -> int | None:
    value = message.get(f"{prefix}_unixtime")
    if value is not None:
        return int(value)
    value = message.get(prefix)
    if not value:
        if prefix == "date":
            raise UserError("У сообщения отсутствует дата.")
        return None
    dt = datetime.fromisoformat(value)
    # Telegram's epoch fields are authoritative. Naive legacy dates use UTC explicitly.
    return int(dt.replace(tzinfo=UTC).timestamp() if dt.tzinfo is None else dt.timestamp())


def serialize(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def meaningful_content(message: dict, media: list[dict]) -> dict:
    meaningful = {
        key: message.get(key)
        for key in (
            "type",
            "date_unixtime",
            "date",
            "from_id",
            "actor_id",
            "text",
            "text_entities",
            "reply_to_message_id",
            "forwarded_from",
            "forwarded_from_id",
            "saved_from",
            "via_bot",
            "grouped_id",
            "album_id",
            "action",
            "actor",
            "members",
            "media_type",
            "mime_type",
            "sticker_emoji",
            "duration_seconds",
        )
        if key in message
    }
    meaningful["media"] = [
        {"kind": item["kind"], "identity": item["sha256"] or item["relative_path"]}
        for item in media
    ]
    return meaningful


def content_hash(message: dict, media: list[dict]) -> str:
    return hashlib.sha256(serialize(meaningful_content(message, media)).encode("utf-8")).hexdigest()
