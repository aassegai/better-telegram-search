"""Whole short-message identities; no substring or transitive deletion rules."""

from telegram_search.search.chunking.classification import WORD, whole_word
from telegram_search.shared.text import normalize_text

MAX_MESSAGE_FORM = 256
APPROVED_MESSAGES = frozenset({"зоопарк xd", "покормить жабу"})


def message_form(text):
    if not isinstance(text, str) or len(text) > 512:
        return None
    single = whole_word(text)
    if single is not None:
        return single if len(single) <= 64 else None
    value = " ".join(normalize_text(text).split())
    if "://" in value or value.startswith("www."):
        return None
    if not 1 <= len(value) <= MAX_MESSAGE_FORM or any(ord(char) < 32 for char in value):
        return None
    words = [match.group() for match in WORD.finditer(value)]
    if not 1 <= len(words) <= 32 or len(set(words)) >= 5:
        return None
    if len(words) == 1 and not value.startswith("/"):
        return None
    return value


def valid_form(value):
    return isinstance(value, str) and message_form(value) == value


def is_message_form(value):
    return valid_form(value) and whole_word(value) is None
