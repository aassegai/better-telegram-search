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


def canonical_entities(message: dict) -> list[dict]:
    """UTF-16 spans make Desktop text arrays and MTProto entities comparable."""
    entities = message.get("text_entities")
    if not isinstance(entities, list):
        entities = message.get("text") if isinstance(message.get("text"), list) else []
    result, offset = [], 0
    aliases = {"text_url": "text_link", "mention_name": "text_mention", "phone": "phone_number"}
    for entity in entities:
        if isinstance(entity, str):
            offset += len(entity.encode("utf-16-le")) // 2
            continue
        if not isinstance(entity, dict):
            continue
        text = str(entity.get("text", ""))
        length = len(text.encode("utf-16-le")) // 2
        kind = aliases.get(entity.get("type"), entity.get("type", "plain"))
        if kind != "plain":
            span = {
                "type": kind,
                "offset": int(entity.get("offset", offset)),
                "length": int(entity.get("length", length)),
            }
            for key in ("href", "user_id", "language", "document_id"):
                value = entity.get(key)
                if value not in (None, ""):
                    span[key] = str(value)
            result.append(span)
        offset += length
    return sorted(result, key=lambda item: (item["offset"], item["length"], serialize(item)))


def meaningful_content(message: dict, media: list[dict]) -> dict:
    # Rendering metadata (names, filenames, local timezone, receipt time) is not a revision.
    meaningful = {
        "type": message.get("type", "message"),
        "sent_at": timestamp(message),
        "author_id": message.get("from_id") or message.get("actor_id"),
        "text": flatten_text(message.get("text")),
        "entities": canonical_entities(message),
        "reply_to_message_id": message.get("reply_to_message_id"),
        "grouped_id": str(message.get("grouped_id") or message.get("album_id") or ""),
    }
    if meaningful["type"] != "message":
        meaningful["action"] = message.get("action")
        meaningful["members"] = message.get("members", [])
    meaningful["media"] = sorted(
        (
            {"kind": item["kind"], "identity": item["sha256"] or item["relative_path"]}
            for item in media
        ),
        key=serialize,
    )
    return meaningful


def content_hash(message: dict, media: list[dict]) -> str:
    return hashlib.sha256(serialize(meaningful_content(message, media)).encode("utf-8")).hexdigest()
