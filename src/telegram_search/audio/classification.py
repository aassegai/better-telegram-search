"""Classify metadata without decoding media or importing Telegram/ML runtimes."""

AUDIO_KINDS = frozenset({"voice", "audio"})


def identity_kind(kind):
    # Refining the old generic attachment type is not an edit to its bytes.
    return "attachment" if kind in AUDIO_KINDS else kind


def audio_mime(value):
    return isinstance(value, str) and value.split(";", 1)[0].strip().casefold().startswith("audio/")


def desktop_file_kind(message):
    kind = message.get("media_type")
    if kind == "voice_message":
        return "voice"
    if kind == "audio_file":
        return "audio"
    if kind in ("video_file", "video_message", "animation", "sticker"):
        return "attachment"
    return "audio" if audio_mime(message.get("mime_type")) else "attachment"


def telegram_audio_kind(document):
    attributes = getattr(document, "attributes", None) or []
    if any(type(attribute).__name__ == "DocumentAttributeVideo" for attribute in attributes):
        return None
    mime = getattr(document, "mime_type", "")
    if isinstance(mime, str) and mime.strip().casefold().startswith("video/"):
        return None
    for attribute in attributes:
        if type(attribute).__name__ == "DocumentAttributeAudio":
            return "voice" if getattr(attribute, "voice", False) is True else "audio"
    return "audio" if audio_mime(mime) else None
