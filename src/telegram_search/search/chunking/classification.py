"""Conservative whole-message rules, with no lexical stopword removal."""

import re
from dataclasses import dataclass

from telegram_search.shared.text import normalize_text

WORD = re.compile(r"[^\W_]+(?:[-'’][^\W_]+)*", re.UNICODE)
SUPPORT = frozenset({"да", "нет", "ок", "ага", "угу", "неа", "не", "тоже", "почему", "зачем"})


def whole_word(text):
    value = normalize_text(text.strip())
    # Only enclosing punctuation is ignored; never change the original ranges/text.
    value = value.strip(' \t\n\r.,!?…:;"«»“”()[]')
    return value if WORD.fullmatch(value) else None


@dataclass(frozen=True)
class Classification:
    role: str
    counts: bool
    include: bool = True


def classify(message, policy, parent=None):
    if message.kind != "message":
        return Classification("service", False, False)
    text = message.text.strip()
    if not text:
        return Classification("attachment", False, False)
    form = whole_word(text)
    # Captions and structured entities protect their contents from filler filtering.
    if form in policy.filler and not (
        message.has_attachment or message.has_photo or message.protected
    ):
        if not parent or "?" not in parent.text:
            return Classification("filler", False, False)
    if form in SUPPORT:
        return Classification("support", False)
    # Standalone terms, numbers, URLs and emoji are retained but do not spend a
    # meaningful-message slot. Their rendered text ALWAYS spends model tokens.
    one_field = len(text.split()) == 1
    symbolic = all(not char.isalnum() for char in text)
    counts = not (form is not None or one_field or symbolic)
    if message.has_attachment or message.has_photo:
        counts = len(list(WORD.finditer(text))) > 1
    return Classification("core", counts)
